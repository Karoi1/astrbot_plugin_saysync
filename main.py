import time
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest, LLMResponse

from .core import prompt_template
from .core.scheduler import SessionScheduler
from .core.models import SchedulerResult


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
        logger.info("收到用户推送了")
        chat_id = event.unified_msg_origin
        # --- 分支 B：处理底层输入状态推送 ---
        if not event.message_str:
            if self._parse_aiocqhttp_input_status(event, chat_id):
                # 状态已交接给 Scheduler，直接杀死事件，不进入后续流程
                event.stop_event()
            return
        # --- 分支 A：处理正常文本消息 ---
        # 1. 格式化纯文本数据
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        packed_msg = f"[{timestamp}] {event.get_sender_name()}: {event.message_str}"
        # 2. 将数据交给调度器，获取控制凭证
        future = self.scheduler.submit_message(chat_id, packed_msg)
        # 3. 原地挂起，等待调度器的最终判决
        result = await future
        logger.info(f"future result是{result},类={type(result)}")
        # 4. 醒来后执行判决
        if result == SchedulerResult.KILL:
            # 被新消息顶替了，安静地去死
            event.stop_event()
            return
        if result == SchedulerResult.PROCESS:
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
        # logger.info("这是我们要找的LLM request")
        # 核心魔法：用积攒的队列替换掉原本只有一句话的 prompt
        req.prompt = prompt_template.format_queue_prompt(pack)
        # 追加系统提示词
        req.system_prompt += "\n" + prompt_template.QUEUE_SYSTEM_PROMPT_SUFFIX

        logger.info(f"[{event.unified_msg_origin}] LLM 请求已被劫持，替换为聚合 Prompt。消息数量:{len(pack.messages)},状态:{pack.user_state}")
        
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