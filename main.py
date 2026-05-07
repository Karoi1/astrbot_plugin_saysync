import time
import asyncio
from enum import Enum
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from . import prompt_template
from .prompt_template import MesStatePack
from typing import Dict, Optional
from astrbot.api.provider import ProviderRequest, LLMResponse

class UserStatus:
    # 从用户输入状态，判断用户想要什么定制化服务
    class StateMachine(Enum):
        idle = 0               # 没打字/发送完消息后停止
        typing = 1             # 正在打字
        cleared = 2            # 打了字又删了，欲言又止
        cleared_sure = 3       # 同上，过滤用

    def __init__(self):
        self._mes_sent: bool = False
        self.sm: self.StateMachine = self.StateMachine.idle

    def state_transfer(self, _input_status: bool):
        """状态转移。当前在打字吗？会发送还是默默删除呢"""
        # 如果发送了消息，就回到最初状态
        if self._mes_sent:
            self.reset()
            return

        # 正在开始打字
        if self.sm == self.StateMachine.idle:
            if _input_status:
                self.sm = self.StateMachine.typing
            # else stay idle

        # 正在打字 -> 继续打字/欲言又止
        elif self.sm == self.StateMachine.typing:
            if self._input_status:
                self.sm = self.StateMachine.typing
            else:
                self.sm = self.StateMachine.cleared

        # 欲言又止 -> 真·欲言又止（过滤）
        elif self.sm == self.StateMachine.cleared:
            if self._input_status:
                self.sm = self.StateMachine.cleared_sure   # 第一次收到输入，进入过渡态
            # else stay cleared

        # 真·欲言又止 -> 正在打字
        elif self.sm == self.StateMachine.cleared_sure:
            if self._input_status:
                self.sm = self.StateMachine.typing          # 再次收到输入，确认进入 typing
            else:
                self.sm = self.StateMachine.cleared         # 输入消失，退回 cleared

        else:
            # 容错：回归初始
            self.reset()

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
        # 消息包
        self.message_queue: list[str] = []
        # 备战未来
        self.active_future: asyncio.Future | None = None
        self.timer_task: asyncio.Task | None = None
        # 会赢吗
        self.is_processing: bool = False
        self.lock_timestamp: float = -1

class SchedulerResult(Enum):
    PROCESS = "PROCESS"
    KILLED = "KILLED"

class SessionScheduler:
    """后厨，正在思考如何下锅。"""
    _sessions: Dict[str, SessionContext] = {}         # 每个chat_id对应一个session，会话隔离
    _user_status: Dict[str, UserStatus] = {}         # 每个chat_id对应的用户输入状态
    _max_size: int = 3                               # 最大队列长度
    _expire_time: float = 10                         # 超时后立刻下锅
    dead_lock_threshold = 30                         # 30秒没收到LLM回复，锅烧糊了，得关火下新的

    def __init__(self, max_size=3, expire_time=10, dead_lock_threshold=30):
        pass

    def _get_session(self, chat_id: str) -> SessionContext:
        """找chat_id对应session。这桌客户之前点没点过菜来着"""
        if chat_id not in self._sessions:
            self._sessions[chat_id] = SessionContext()
        return self._sessions[chat_id]

    def _get_user_status(self, chat_id: str) -> UserStatus:
        """找chat_id对应输入状态。客户要什么定制服务来着"""
        if chat_id not in self._user_status:
            self._user_status[chat_id] = UserStatus()
        return self._user_status[chat_id]

    # ==========================================
    # 对外接口
    # ==========================================    

    def update_input_state(self, chat_id: str, is_typing: bool):
        """插件服务员说客户想自定口味，更新chat_id对应session的输入状态"""
        curr_status = self._get_user_status(chat_id)
        curr_status.state_transfer(is_typing)
        logger.debug(f"[Scheduler][{chat_id}] 用户状态更新: {curr_status.sm.name}")

    def submit_message(self, chat_id: str, text: str) -> asyncio.Future:
        """插件服务员送来了某桌客户的餐牌，挂起餐牌等待入锅"""
        session = self._get_session(chat_id)

        # 1. 【杀旧】干掉正在傻等的旧协程
        if session.active_future and not session.active_future.done():
            session.active_future.set_result("KILL")
        
        # 2. 【攒货】无论死活，先把数据存下来
        session.message_queue.append(text)
        logger.info(f"[Scheduler][{chat_id}] 消息入队，当前数量: {len(session.message_queue)}") 

        # 3. 创建新的控制凭证
        new_future = asyncio.Future()

        # 4. 【容量检查】队列满了，不挂起，直接给一个“假凭证”让其立刻去打包
        if len(session.message_queue) >= self._max_size:
            logger.info("Scheduler: 队列满，放行")
            session.active_future = new_future
            new_future.set_result("PROCESS") # 立刻唤醒
            return new_future

        # 5. 【正常挂起】重置定时器，挂起新凭证
        logger.info("Scheduler: 挂起")
        self._reset_timer(session, chat_id)
        session.active_future = new_future
        return new_future

    def prepare_release(self, chat_id: str) -> Optional[MesStatePack]:
        """
        要准备下锅了，正在考虑先做哪个菜。每桌在某一时刻最多下一锅，下锅上锁
        争抢放行权，抢夺成功就给Session上锁（最多dead_lock_threshold秒），成功就返回pack，否则返回None
        """
        session = self._sessions.get(chat_id)
        if not session:
            return None
        
        # 1. 【看门狗】检查死锁
        if session.is_processing:
            if time.time() - session.lock_timestamp > self._dead_lock_threshold:
                logger.warning(f"[Scheduler][{chat_id}] 检测到死锁(>{self._dead_lock_threshold}s)，暴力砸锁！")
                session.is_processing = False
            else:
                # 上一轮还在跑，当前事件放弃
                logger.debug(f"[Scheduler][{chat_id}] 上一轮处理中，当前事件放弃争抢。")
                return None

        # 2. 【防御】如果没有货了（可能被极端并发情况清空了）
        if not session.message_queue:
            return None
        
        # 3. 【争抢成功】打包数据
        status = self._get_user_status(chat_id)
        pack = MesStatePack(session.message_queue.copy(), status.pack_state) 

        # 4. 【清理现场】
        session.message_queue.clear()
        status.reset()      

        if session.timer_task and not session.timer_task.done():
            session.timer_task.cancel()
        session.active_future = None

        # 5. 【上锁】
        session.is_processing = True
        session.lock_timestamp = time.time()

        logger.info(f"[Scheduler][{chat_id}] 争抢成功，打包放行。消息数: {len(pack.messages)}")
        return pack

    def unlock_session(self, chat_id: str):
        """
        下锅抄完，把大盘菜端上来
        LLM回复完咯，当前session可解锁
        """
        session = self._sessions.get(chat_id)
        if session and session.is_processing:
            session.is_processing = False
            logger.info(f"[Scheduler][{chat_id}] 会话处理完成，已解锁。")
            # 注意：这里不需要主动触发下一轮。
            # 如果在处理期间有新消息，它们会乖乖躺在队列里。
            # 等待下一次用户发消息或定时器自然触发时，prepare_release 会发现锁开了且队列有货，自然接管。

    async def terminate(self):
        """插件卸载时的清理工作"""
        for chat_id, session in self._sessions.items():
            if session.timer_task and not session.timer_task.done():
                session.timer_task.cancel()
            if session.active_future and not session.active_future.done():
                session.active_future.set_result("KILL")

    # ==========================================
    # 内部方法
    # ==========================================

    def _reset_timer(self, session: SessionContext, chat_id: str):
        """重置超时计时器"""
        if session.timer_task and not session.timer_task.done():
            session.timer_task.cancel()
        session.timer_task = asyncio.create_task(self._timer_expire(chat_id))

    async def _timer_expire(self, chat_id: str):
        """定时器到期回调"""
        try:
            await asyncio.sleep(self._expire_time)
            session = self._sessions.get(chat_id)
            # 只有当有凭证在等待，且没有在处理中时，才唤醒
            if session and session.active_future and not session.active_future.done() and not session.is_processing:
                logger.debug(f"[Scheduler][{chat_id}] 定时器到期，触发唤醒")
                session.active_future.set_result("PROCESS")
        except asyncio.CancelledError:
            pass # 被新消息重置了，静默退出




@register("chatqueue", "YourName", "挂起式队列感知插件", "1.0.0")
class ChatQueuePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 实例化我们纯手工打造的调度大脑
        self.scheduler = SessionScheduler(
            max_size=3, 
            expire_time=10.0,
            dead_lock_threshold=60.0
        )

    # ==========================================
    # 接线员 A & B：事件总入口 (极高优先级截胡)
    # ==========================================
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        logger.info("收到推送了")
        user_name = event.get_sender_name()
        message_str = event.message_str
        logger.info(f"{user_name}: {message_str}")
        chat_id = event.unified_msg_origin
        # --- 分支 B：处理底层输入状态推送 ---
        if not event.message_str:
            if self._parse_aiocqhttp_input_status(event, chat_id):
                # 状态已交接给 Scheduler，直接杀死事件，不进入后续流程
                event.stop_event()
            return
        logger.info("123")
        # --- 分支 A：处理正常文本消息 ---
        # 1. 格式化纯文本数据
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        packed_msg = f"[{timestamp}] {event.get_sender_name()}: {event.message_str}"
        # 2. 将数据交给调度器，获取控制凭证
        future = self.scheduler.submit_message(chat_id, packed_msg)
        logger.info(type(future))
        logger.info("123")
        # 3. 原地挂起，等待调度器的最终判决
        result = await future
        logger.info(f"result: {result}")
        # 4. 醒来后执行判决
        if result == "KILL":
            # 被新消息顶替了，安静地去死
            event.stop_event()
            return
        if result == "PROCESS":
            # 调度器允许放行，尝试获取打包好的货物
            pack = self.scheduler.prepare_release(chat_id)
            
            if pack is None:
                # 拿不到货说明上一轮卡死还没解锁（看门狗拦住了），放弃本次事件
                event.stop_event()
                return
            
            # 拿到货了：把暗号和货物缝在 event 身上，然后正常 return 放行
            event.set_extra("chatqueue_pending", True)
            event.set_extra("chatqueue_pack", pack)
            # 注意：这里没有 stop_event()，事件将继续流向 AstrBot 的原生 LLM 流程
            return

    # ==========================================
    # 接线员 C：瞒天过海 (劫持原生 LLM 请求)
    # ==========================================
    @filter.on_llm_request(priority=10)
    async def hijack_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        # 检查暗号：是不是我们放行的事件？
        logger.info("收到LLM request")
        if not event.get_extra("chatqueue_pending"):
            return # 不是，放行

        pack = event.get_extra("chatqueue_pack")
        if not pack:
            return

        # 核心魔法：用积攒的队列替换掉原本只有一句话的 prompt
        req.prompt = prompt_template.format_queue_prompt(pack)
        # 追加系统提示词
        req.system_prompt += "\n" + prompt_template.QUEUE_SYSTEM_PROMPT_SUFFIX

        logger.info(f"[{event.unified_msg_origin}] LLM 请求已被劫持，替换为聚合 Prompt")
        
        # 擦除暗号，防止干扰其他插件或后续流程
        event.set_extra("chatqueue_pending", False)

    # ==========================================
    # 接线员 D：善后解锁 (最高优先级保底)
    # ==========================================
    @filter.on_llm_response(priority=100)
    async def force_unlock_on_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        logger.info("收到LLM response")
        # 只要 LLM 给出了响应（哪怕报错），立刻通知调度器解锁
        chat_id = event.unified_msg_origin
        self.scheduler.unlock_session(chat_id)

    # ==========================================
    # 辅助方法：解析 QQ 输入状态协议
    # ==========================================
    def _parse_aiocqhttp_input_status(self, event: AstrMessageEvent, chat_id: str) -> bool:
        if event.get_platform_name() != "aiocqhttp":
            return False
            
        try:
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
                # 直接扔给调度器的状态机处理
                self.scheduler.update_input_state(chat_id, new_status)
                logger.debug(f"用户输入状态更新: {'正在输入' if new_status else '停止输入'}")
                return True
        except Exception as e:
            logger.debug(f"解析输入状态异常: {e}")
            
        return False

    # ==========================================
    # 生命周期管理
    # ==========================================
    async def terminate(self):
        await self.scheduler.terminate()