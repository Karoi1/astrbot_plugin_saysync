import time
import re
import asyncio
import uuid

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.core.message.components import Plain

from .core import prompt_template
from .core.scheduler import SessionScheduler
from .core.models import SchedulerResult, ProactiveTask, ProactiveType


@register("知音", "Robin", "安静倾听，告别一问一答的机械感", "1.0.1")
class ChatQueuePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 实例化我们纯手工打造的调度大脑，绑定统一的“接单员”回调
        self.scheduler = SessionScheduler(
            max_size=3, 
            expire_time=10.0,  # 顺手修复了你的小 typo
            dead_lock_threshold=60.0,
            on_proactive_ready=self._handle_proactive_ready 
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
        self.scheduler.update_skin(chat_id, event)

        # --- 分支 B：处理底层输入状态推送 ---
        if not event.message_str:
            if self._parse_aiocqhttp_input_status(event, chat_id):
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

        logger.info(f"[{event.unified_msg_origin}] LLM 请求已被劫持，替换为聚合 Prompt。消息数量:{len(pack.messages)},状态:{pack.user_state}")
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
    @filter.on_llm_response(priority=10)
    async def trigger_proactive_check(self, event: AstrMessageEvent, resp: LLMResponse):
        """消息发送完毕后的钩子：寻找主动说话的机会（目前100%触发测试）"""
        if not event.is_private_chat():
            return
        if event.get_extra("is_implicit_proactive", False):
            return
            
        chat_id = event.unified_msg_origin
        
        # 提交一个“结束补充”类型的主动任务
        task = ProactiveTask(
            chat_id=chat_id,
            task_type=ProactiveType.FAREWELL,
            instruction="[底层系统提示：用户刚刚表达了结束对话的意图（如说了晚安、拜拜、去忙了）。请以极其自然的语气，像突然想起来什么事一样，补充一句极短的话（10字以内）。禁止出现总结性、过度热情或机械的结束语。]",
            delay=5.0  # 测试用，后续可以随机 15~45秒
        )
        self.scheduler._submit_proactive_task(task)

    # ==========================================
    # 统一接单员：处理 Scheduler 推送过来的任务单
    # ==========================================
    async def _handle_proactive_ready(self, task: ProactiveTask):
        """
        统一的主动说话接单员：只负责根据任务单造 Event 并推流。
        不再手动拉取历史，直接把指令塞给 prompt，让流水线自动拼接历史！
        """
        chat_id = task.chat_id
        try:
            skin = self.scheduler.get_skin(chat_id)
            if not skin or not skin.is_ready():
                return

            # 直接把任务单里的指令作为 prompt，让流水线自动拼接历史！
            fake_event = self._build_fake_event(skin, prompt=task.instruction)
            if fake_event:
                self.context._event_queue.put_nowait(fake_event)
                logger.info(f"[主动说话][{chat_id}] {task.task_type.value} 任务已推流。")
        except Exception as e:
            logger.error(f"[主动说话][{chat_id}] 接单员处理失败: {e}", exc_info=True)

    # ==========================================
    # 核心伪造与辅助方法
    # ==========================================
    def _build_fake_event(self, skin, prompt: str):
        try:
            fake_msg_obj = skin.clone_message_obj(prompt=prompt)
            platform_name = skin.platform_meta.name
            fake_event = None
            
            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                
                if not skin.bot:
                    logger.error("[主动说话] 缓存的 Bot 客户端为空，无法伪造 aiocqhttp Event。")
                    return None
                    
                fake_event = AiocqhttpMessageEvent(
                    message_str=prompt,
                    message_obj=fake_msg_obj,
                    platform_meta=skin.platform_meta,
                    session_id=skin.session_id,
                    bot=skin.bot 
                )
            else:
                from astrbot.core.platform.astr_message_event import AstrMessageEvent
                fake_event = AstrMessageEvent(
                    message_str=prompt,
                    message_obj=fake_msg_obj,
                    platform_meta=skin.platform_meta,
                    session_id=skin.session_id,
                )
                logger.warning(f"[主动说话] 平台 {platform_name} 暂不支持主动说话降级处理。")
            
            fake_event.set_extra("is_implicit_proactive", True)
            fake_event.is_at_or_wake_command = True
            fake_event.is_wake = True
            
            return fake_event

        except Exception as e:
            logger.error(f"[主动说话] 伪造 Event 失败: {e}", exc_info=True)
            return None

    def _parse_aiocqhttp_input_status(self, event: AstrMessageEvent, chat_id: str) -> bool:
        if event.get_platform_name() != "aiocqhttp":
            return False
            
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            assert isinstance(event, AiocqqhttpMessageEvent)
            raw = event.message_obj.raw_message
            
            if not isinstance(raw, dict):
                return False
                
            if (raw.get("post_type") == "notice" and
                raw.get("notice_type") == "notify" and
                raw.get("sub_type") == "input_status" and "status_text" in raw):
                
                new_status = bool(raw["status_text"])
                self.scheduler.update_input_state(chat_id, new_status)
                return True
        except Exception as e:
            logger.debug(f"解析输入状态异常: {e}")
            
        return False

    # ==========================================
    # 生命周期管理
    # ==========================================
    async def terminate(self):
        await self.scheduler.terminate()