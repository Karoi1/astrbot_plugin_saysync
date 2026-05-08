import time

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest, LLMResponse

from .core import ProactiveManager
from .core import prompt_template
from .core import SessionScheduler
from .core.models import *
from .core.models import _log

# ====== 日志开关：True 打印所有日志，False 静默 ======
enable_log = False


@register("知音", "Robin", "安静倾听，告别一问一答的机械感", "1.0.1")
class ChatQueuePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

        self.scheduler = SessionScheduler(
            max_size=3, 
            expire_time=10.0, 
            dead_lock_threshold=60.0,
        )
        self.proactive_mgr = ProactiveManager(
            context=self.context
        )

    # ==========================================
    # 接线员 A & B：事件总入口
    # ==========================================
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=-10)
    async def on_message(self, event: AstrMessageEvent):
        """挂起最后一个收到的消息"""
        # ========== 绝对防御：拦截主动说话的假事件 ==========
        if event.get_extra("is_implicit_proactive"):
            return 
        
        chat_id = event.unified_msg_origin
        self.proactive_mgr.update_skin(chat_id, event)

        # --- 分支 B：处理底层输入状态推送 ---
        if not event.message_str:
            if self._from_aiocqhttp_update(event, chat_id):
                event.stop_event()
            return
        
        # --- 分支 A：处理正常文本消息 ---
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        packed_msg = f"[{timestamp}] {event.get_sender_name()}: {event.message_str}"
        
        future = self.scheduler.submit_message(chat_id, packed_msg)
        result = await future

        if result == SchedulerResult.KILL:
            event.stop_event()
            return
        if result == SchedulerResult.PROCESS:
            pack = self.scheduler.prepare_release(chat_id)
            
            if pack is None:
                event.stop_event()
                return
            
            event.set_extra("chatqueue_pending", True)
            event.set_extra("chatqueue_pack", pack)
            return

    # ==========================================
    # 接线员 C：瞒天过海 (劫持原生 LLM 请求)
    # ==========================================
    @filter.on_llm_request(priority=10)
    async def hijack_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """修改prompt和system prompt"""
        if not event.get_extra("chatqueue_pending"):
            return 

        pack = event.get_extra("chatqueue_pack")
        if not pack:
            return

        req.prompt = prompt_template.format_queue_prompt(pack)
        req.system_prompt += "\n" + prompt_template.QUEUE_SYSTEM_PROMPT_SUFFIX

        _log(enable_log, "info", f"[{event.unified_msg_origin}] LLM 请求已被劫持，替换为聚合 Prompt。消息数量:{len(pack.messages)},状态:{pack.user_state}")
        event.set_extra("chatqueue_pending", False)

    # ==========================================
    # 接线员 D：善后解锁 (最高优先级保底)
    # ==========================================
    @filter.on_llm_response(priority=100)
    async def force_unlock_on_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """解锁上锁的会话"""
        chat_id = event.unified_msg_origin
        self.scheduler.unlock_session(chat_id)

    # ==========================================
    # 接线员 E：主动说话引擎调度器
    # ==========================================
    @filter.on_llm_response()
    async def trigger_proactive_check(self, event: AstrMessageEvent, resp: LLMResponse):
        """消息发送完毕后的钩子：寻找主动说话的机会（目前100%触发测试）"""
        if not event.is_private_chat():
            return
        if event.get_extra("is_implicit_proactive", False):
            _log(enable_log, "info", "[LLM response Trigger]: 不重复触发Proactive")
            return
            
        # 创建任务
        _log(enable_log, "info", "[LLM response Trigger]: 创建一条新任务给Proactive Mgr")
        instruction = "[这是测试的一部分。除非有特别说明，请你说一句关于\"绿色\"的事物]"
        task = ProactiveTask(
            chat_id=event.unified_msg_origin,
            task_type = ProactiveType.FAREWELL,
            instruction = instruction,
            delay = 10
        )
        self.proactive_mgr.submit_delay_task(task)

    # ==========================================
    # 核心辅助方法
    # ==========================================

    def _from_aiocqhttp_update(self, event: AstrMessageEvent, chat_id: str):
        """从cqhttp消息中提取信息"""
        if event.get_platform_name() != "aiocqhttp":
            return
            
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            assert isinstance(event, AiocqhttpMessageEvent)
            raw = event.message_obj.raw_message
            
            if not isinstance(raw, dict):
                return
            

            # ========== 提取输入状态 ==========
            if (raw.get("post_type") == "notice" and
                raw.get("notice_type") == "notify" and
                raw.get("sub_type") == "input_status" and "status_text" in raw):
                
                new_status = bool(raw["status_text"])
                self.scheduler.update_input_state(chat_id, new_status)

        except Exception as e:
            _log(enable_log, "info", f"Error in _from_aiocqhttp_update(): {e}")

    # ==========================================
    # 生命周期管理
    # ==========================================
    async def terminate(self):
        await self.scheduler.terminate()
        await self.proactive_mgr.terminate()