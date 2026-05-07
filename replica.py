import time
import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest
import json
from enum import Enum
from . import prompt_template

class MesStatePack:
    def __init__(self, messages: list, user_state: str):
        self.messages = messages  # 队列中的消息列表
        self.user_state = user_state  # 打包后的用户状态字符串

class UserStatus:
    class StateMachine(Enum):
        idle = 0
        typing = 1
        cleared = 2
        cleared_sure = 3   # 新增：确认重新输入前的过渡状态

    _input_status: bool = False
    _mes_sent: bool = False
    sm: StateMachine = StateMachine.idle

    def state_transfer(self):
        # 消息发送事件最高优先级：任意状态 → idle 并重置所有标志
        if self._mes_sent:
            self.reset()
            return

        # 常规状态转移（基于 _input_status）
        if self.sm == self.StateMachine.idle:
            if self._input_status:
                self.sm = self.StateMachine.typing
            # else stay idle

        elif self.sm == self.StateMachine.typing:
            if self._input_status:
                self.sm = self.StateMachine.typing
            else:
                self.sm = self.StateMachine.cleared

        elif self.sm == self.StateMachine.cleared:
            if self._input_status:
                self.sm = self.StateMachine.cleared_sure   # 第一次收到输入，进入过渡态
            # else stay cleared

        elif self.sm == self.StateMachine.cleared_sure:
            if self._input_status:
                self.sm = self.StateMachine.typing          # 再次收到输入，确认进入 typing
            else:
                self.sm = self.StateMachine.cleared         # 输入消失，退回 cleared

        else:
            # 容错：未知状态重置
            self.reset()

    def reset(self):
        self._input_status = False
        self._mes_sent = False
        self.sm = self.StateMachine.idle
    
    # >>> 新增以下代码 <<<
    @property
    def pack_state(self) -> str:
        """获取用于打包的状态，将 cleared_sure 映射为 cleared"""
        if self.sm == self.StateMachine.cleared_sure:
            return "cleared"
        return self.sm.name





@register("chatqueue", "YourName", "一个带超时清空的队列插件", "1.0.0")
class ChatQueuePlugin(Star):
    def __init__(self, context: Context, expire_time: int = 10):
        super().__init__(context)
        self.max_size = 3
        self.expire_time = expire_time
        self.queue = []
        self._timer_task = None
        self.userstatus = UserStatus()

    # ================= 队列与定时器核心控制 =================

    def _start_timer(self):
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = asyncio.create_task(self._timer_expire())

    def _cancel_timer(self):
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
            self._timer_task = None

    async def _timer_expire(self):
        """定时器唯一职责：等待超时，然后触发结算"""
        await asyncio.sleep(self.expire_time)
        await self._flush_queue("超时")
        self._timer_task = None

    async def _flush_queue(self, reason: str):
        """统一结算出口：负责打包、清理现场、调用LLM"""
        if not self.queue:
            return

        pack = MesStatePack(self.queue.copy(), self.userstatus.pack_state)
        logger.info(f"[{reason}] 触发打包，状态: {pack.user_state}, 消息数: {len(pack.messages)}")
        
        self.queue.clear()
        self._cancel_timer()
        self.userstatus.reset()
        await self._process_pack_to_llm(pack)

    # ================= 指令与事件入口 =================

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def _parse_platform_event(self, event: AstrMessageEvent):
        # 1. 标记消息已发送（供状态机最高优先级使用）
        if event.message_str:
            self.userstatus._mes_sent = True
            logger.info("设置mes sent=1")

        # 2. 解析底层平台输入状态
        if not self._parse_aiocqhttp_input_status(event):
            return

        # 3. 驱动状态机流转
        self.userstatus.state_transfer()
        logger.info(f"用户状态：{self.userstatus.sm}")

        # 4. 根据状态执行对应动作
        self._handle_state_response()

    @filter.command("queue", alias={"我在", "在吗"})
    async def handle_queue(self, event: AstrMessageEvent, message: str):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        packed = f"[{timestamp}] {event.get_sender_name()}: {message}"

        self.queue.append(packed)
        logger.info(f"消息入队，当前队列消息数量: {len(self.queue)}")

        if len(self.queue) >= self.max_size:
            await self._flush_queue("队列已满")
        else:
            self._start_timer()

        event.stop_event()
    # ================= 平台协议解析层 =================

    def _parse_aiocqhttp_input_status(self, event: AstrMessageEvent) -> bool:
        """隔离底层协议解析逻辑，仅更新状态机的 _input_status"""
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
            if new_status != self.userstatus._input_status:
                self.userstatus._input_status = new_status
                logger.info(f"用户输入状态更新: {'正在输入' if new_status else '停止输入'}")
            return True
            
        return False

    # ================= 状态响应层 =================

    def _handle_state_response(self):
        """根据状态机的最新状态，决定对队列/定时器的操作"""
        if self.queue and self.userstatus.sm == UserStatus.StateMachine.typing:
            self._start_timer()
        elif not self.queue and self.userstatus.sm in (UserStatus.StateMachine.cleared, UserStatus.StateMachine.cleared_sure):
            self._proactive_ask()

    # ================= LLM 拦截调试层 =================
    @filter.on_llm_request()
    async def intercept_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        '''截获并打印即将发送给 LLM 的请求对象'''
        logger.info("====== 拦截到 LLM 请求 ======")
        logger.info(f"请求对象类型: {type(req)}")
        
        # 打印对象的所有属性和值，方便查看格式
        if hasattr(req, '__dict__'):
            for key, value in req.__dict__.items():
                logger.info(f"req.{key} = {value}")
        else:
            logger.info(f"请求对象内容: {req}")
            
        logger.info("====== 拦截结束 ======")
        
        # 注意：这里暂时不要加 event.stop_event()，避免影响正常的聊天流程
    # ================= 业务处理层 =================

    async def _process_pack_to_llm(self, pack: MesStatePack):
        """后续负责排版并发给 LLM 的新函数"""
        pass

    def _proactive_ask(self):
        """主动询问函数"""
        pass

    async def terminate(self):
        self._cancel_timer()