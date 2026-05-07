import time
import asyncio
from enum import Enum
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from .core import prompt_template
from .core.prompt_template import MesStatePack

class UserStatus:
    # ... 保持你原有的状态机代码不变 ...
    class StateMachine(Enum):
        idle = 0
        typing = 1
        cleared = 2
        cleared_sure = 3

    def __init__(self):
        self._input_status: bool = False
        self._mes_sent: bool = False
        self.sm: self.StateMachine = self.StateMachine.idle

    def state_transfer(self):
        # ... 保持原有逻辑 ...
        pass

    def reset(self):
        self._input_status = False
        self._mes_sent = False
        self.sm = self.StateMachine.idle

    @property
    def pack_state(self) -> str:
        if self.sm == self.StateMachine.cleared_sure:
            return "cleared"
        return self.sm.name


class SessionContext:
    def __init__(self):
        self.message_queue: list[dict] = [{}]
        self.user_status = UserStatus()
        self.active_future: asyncio.Future | None = None
        self.timer_task: asyncio.Task | None = None
        self.is_processing: bool = False
        # 新增：协程争抢锁，防止多个等待事件同时醒来时重复打包
        self.processing_lock = asyncio.Lock() 


@register("chatqueue", "YourName", "挂起式队列插件", "1.0.0")
class ChatQueuePlugin(Star):
    def __init__(self, context: Context, expire_time: int = 10):
        super().__init__(context)
        self.max_size = 3
        self.expire_time = expire_time
        # 核心改变：使用字典按 chat_id 隔离状态
        self.sessions: dict[str, SessionContext] = {}

    def _get_session(self, chat_id: str) -> SessionContext:
        if chat_id not in self.sessions:
            self.sessions[chat_id] = SessionContext()
        return self.sessions[chat_id]

    # ================= 定时器控制 =================
    def _start_timer(self, session: SessionContext, chat_id: str):
        if session.timer_task and not session.timer_task.done():
            session.timer_task.cancel()
        # 注意：这里将 chat_id 传给定时器，方便后续释放
        session.timer_task = asyncio.create_task(self._timer_expire(session, chat_id))

    async def _timer_expire(self, session: SessionContext, chat_id: str):
        """定时器到期：唤醒挂起的协程"""
        try:
            await asyncio.sleep(self.expire_time)
            # 检查是否正在处理中，防止重入
            if not session.is_processing and session.active_future and not session.active_future.done():
                logger.info(f"[{chat_id}] 超时触发，唤醒挂起事件")
                session.active_future.set_result("PROCESS")
        except asyncio.CancelledError:
            logger.info(f"[{chat_id}] timer被重置了")
            pass # 被新消息重置了，静默退出

    # ================= 核心事件入口 =================
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        chat_id = event.unified_msg_origin
        session = self._get_session(chat_id)

        # 0. 拦截底层状态上报 (如QQ输入状态)
        if not event.message_str:
            if self._parse_aiocqhttp_input_status(event, session):
                self._handle_state_response(session, chat_id)
            # 状态上报不进入挂起流程，直接拦截掉
            event.stop_event()
            return

        # 1. 文本消息预处理
        session.user_status._mes_sent = True

        # 2. 【先存钱】无论发生什么，先把消息入队
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        packed_msg = f"[{timestamp}] {event.get_sender_name()}: {event.message_str}"
        session.message_queue.append(packed_msg)
        logger.info(f"[{chat_id}] 消息入队，当前数量: {len(session.message_queue)}")

        # 3. 容量检查：满了就不需要挂起等待了，直接触发结算
        
        if len(session.message_queue) >= self.max_size:
            await self._execute_release(session, chat_id, event)
            return

        # 4. 【杀旧迎新】排他性挂起
        if session.active_future and not session.active_future.done():
            logger.debug(f"[{chat_id}] 新消息到来，KILL 旧事件")
            session.active_future.set_result("KILL")

        # 创建新的等候牌
        session.active_future = asyncio.Future()

        # 5. 重置定时器
        self._start_timer(session, chat_id)

        # 6. 【原地挂起】当前协程在这里睡死，AstrBot的主流程卡在这里
        result = await session.active_future

        # --- 醒来后的世界 ---
        if result == "KILL":
            # 被新消息顶掉了，没有打包数据，必须杀死事件，防止产生幽灵回复
            event.stop_event()
            return

        if result == "PROCESS":
            # 被定时器唤醒，或者因为队列满直接走到这里
            await self._execute_release(session, chat_id, event)
            # 注意：这里直接 return，不调用 stop_event()，这就是"放行"！
            return

    async def _execute_release(self, session: SessionContext, chat_id: str, event: AstrMessageEvent):
        # ==================== 防线一：等待室 ====================
        # 如果上一轮 LLM 还在转，不要直接 return，原地睡死等它结束
        wait_count = 0
        while session.is_processing:
            await asyncio.sleep(0.1)
            wait_count += 1
            if wait_count > 300:  # 防止真的死锁，最多等 30 秒强制接管
                logger.warning(f"[{chat_id}] 等待上一轮处理超时，强制接管")
                session.is_processing = False
                break

        # ==================== 防线二：争抢锁 ====================
        # 保证哪怕有 10 个协程同时醒来，也只有 1 个能进入下面的逻辑
        async with session.processing_lock:
            # 1. 杀死可能还在傻等的旧事件（解决协程泄露）
            if session.active_future and not session.active_future.done():
                session.active_future.set_result("KILL")

            # 2. 检查队列是否还有货（可能被刚才抢到锁的协程清空了）
            if not session.message_queue:
                logger.debug(f"[{chat_id}] 队列已被其他协程清空，当前事件放弃放行")
                event.stop_event()
                return

            # 3. 打包数据
            pack = MesStatePack(session.message_queue.copy(), session.user_status.pack_state)
            logger.info(f"[{chat_id}] 抢到执行权，准备放行。状态: {pack.user_state}, 消息数: {len(pack.messages)}")

            # 4. 上锁
            session.is_processing = True

            # 5. 清理现场（一定要在打包之后清空！）
            session.message_queue.clear()
            session.user_status.reset()
            if session.timer_task and not session.timer_task.done():
                session.timer_task.cancel()
            session.active_future = None

            # 6. 打暗号放行
            event.set_extra("chatqueue_pending", True)
            event.set_extra("chatqueue_pack", pack)

    # ================= 瞒天过海 (LLM 劫持钩子) =================
    @filter.on_llm_request()
    async def hijack_llm_request(self, event: AstrMessageEvent, req):
        """拦截原生 LLM 请求，偷梁换柱"""
        # 检查暗号
        if not event.get_extra("chatqueue_pending"):
            return # 不是我们放行的事件，放行

        pack = event.get_extra("chatqueue_pack")
        if not pack:
            return

        # 核心魔法：用积攒的队列替换掉原本只有一句话的 prompt
        req.prompt = prompt_template.format_queue_prompt(pack)
        # 追加你需要的系统提示词
        req.system_prompt += "\n" + prompt_template.QUEUE_SYSTEM_PROMPT_SUFFIX

        logger.info(f"[{event.unified_msg_origin}] LLM 请求已被劫持，替换为聚合 Prompt")
        
        # 擦除暗号，防止干扰其他插件
        event.set_extra("chatqueue_pending", False)

    # ================= 收尾解锁 =================
    @filter.after_message_sent()
    async def unlock_session(self, event: AstrMessageEvent):
        """消息发送完毕后，解锁会话，允许接收下一轮积压"""
        chat_id = event.unified_msg_origin
        session = self.sessions.get(chat_id)
        if session and session.is_processing:
            session.is_processing = False
            logger.info(f"[{chat_id}] 消息发送完毕，会话已解锁")

    # ================= 以下保持你原有的辅助方法 =================
    def _parse_aiocqhttp_input_status(self, event: AstrMessageEvent, session: SessionContext) -> bool:
        if event.get_platform_name() != "aiocqhttp":
            return False
            
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        assert isinstance(event, AiocqhttpMessageEvent)
        raw = event.message_obj.raw_message
        
        if not isinstance(raw, dict):
            return False
            
        if (raw.get("post_type") == "notice" and
            raw.get("notice_type") == "notify" and
            raw.get("sub_type") == "input_status" and
            "status_text" in raw):
            
            new_status = bool(raw["status_text"])
            if new_status != session.user_status._input_status:
                session.user_status._input_status = new_status
                logger.info(f"用户输入状态更新: {'正在输入' if new_status else '停止输入'}")
            return True
        return False

    def _handle_state_response(self, session: SessionContext, chat_id: str):
        """状态响应层：你可以在这里根据状态机提前触发结算"""
        # 例如：如果检测到用户停止输入(cleared)，可以主动触发唤醒，不用等超时
        if session.message_queue and session.user_status.sm == UserStatus.StateMachine.typing:
            self._start_timer(session, chat_id)
        elif not session.message_queue and session.user_status.sm in (UserStatus.StateMachine.cleared, UserStatus.StateMachine.cleared_sure):
            pass # self._proactive_ask()

    async def terminate(self):
        for session in self.sessions.values():
            if session.timer_task and not session.timer_task.done():
                session.timer_task.cancel()
            if session.active_future and not session.active_future.done():
                session.active_future.set_result("KILL")