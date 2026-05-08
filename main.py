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
from .core.models import SchedulerResult


@register("知音", "Robin", "安静倾听，告别一问一答的机械感", "1.0.1")
class ChatQueuePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 实例化我们纯手工打造的调度大脑
        self.scheduler = SessionScheduler(
            max_size=3, 
            expire_time=10.0,
            dead_lock_threshold=60.0,
            on_cleared_timeout=self._handle_cleared_timeout
        )

    # ==========================================
    # 接线员 A & B：事件总入口
    # ==========================================
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=-10)
    async def on_message(self, event: AstrMessageEvent):
        """挂起最后一个收到的消息"""
        # logger.info("收到用户推送了")
        # ========== 绝对防御：拦截主动说话的假事件 ==========
        if event.get_extra("is_implicit_proactive"):
            return # 看到免检标签，直接放行，绝不入队！
        

        chat_id = event.unified_msg_origin
        # 把当前会话壳存入skin中
        self.scheduler.update_skin(chat_id, event)

        # --- 分支 B：处理底层输入状态推送 ---
        if not event.message_str:
            if self._parse_aiocqhttp_input_status(event, chat_id):
                # 状态已交接给 Scheduler，直接杀死事件，不进入后续流程
                event.stop_event()
            return
        
        # ========== 防线：发了真消息，告诉 Scheduler 取消等待 ==========
        self.scheduler._cancel_cleared_timer(chat_id)
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
        """修改prompt和system prompt"""


        # 检查暗号：是不是我们放行的事件？
        logger.info("收到LLM request")
        if not event.get_extra("chatqueue_pending"):
            logger.info(f"放行prompt:{req.prompt}")
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
        """解锁上锁的会话"""
        logger.info("收到LLM response")
        # 只要 LLM 给出了响应（哪怕报错），立刻通知调度器解锁
        chat_id = event.unified_msg_origin
        self.scheduler.unlock_session(chat_id)

    # ==========================================
    # 接线员 E：主动说话引擎雏形
    # ==========================================
    @filter.on_llm_response(priority=10)
    async def trigger_proactive_check(self, event: AstrMessageEvent, resp:LLMResponse):
        """消息发送完毕后的钩子：寻找主动说话的机会"""
        # 目前只在私聊生效
        logger.info("after mes sent触发")
        proactive_flag = event.get_extra("is_implicit_proactive", False)
        if not event.is_private_chat():
            return
        if proactive_flag:
            logger.info("这是之前的主动聊天，不会再触发主动")
            return
        logger.info("要create咯")
        chat_id = event.unified_msg_origin
        # 雏形阶段：100% 概率触发，延迟 5 秒（方便你观察日志）
        asyncio.create_task(self._proactive_delay_task(chat_id))

    async def _proactive_delay_task(self, chat_id: str):
        """延迟任务：等待一段时间后，伪造 Event 推入流水线"""
        try:
            # 延迟 5 秒 (测试用，后续可以改成 30 或动态计算)
            await asyncio.sleep(5)
            logger.info(f"[主动说话] {chat_id} 延迟结束，准备伪造事件推入流水线。")

            # 1. 获取该会话的皮肤
            skin = self.scheduler.get_skin(chat_id)
            if not skin or not skin.is_ready():
                logger.warning(f"[主动说话] {chat_id} 皮肤未就绪，取消主动说话。")
                return

            # 2. 伪造假事件
            fake_event = self._build_fake_event(skin, prompt="主动打个招呼")
            if not fake_event:
                return

            # 3. 推入 AstrBot 的主事件队列！
            self.context._event_queue.put_nowait(fake_event)
            logger.info(f"[主动说话] {chat_id} 假事件已成功推入流水线！")

        except Exception as e:
            logger.error(f"[主动说话] {chat_id} 延迟任务发生异常: {e}", exc_info=True)

    def _build_fake_event(self, skin, prompt: str):
        try:
            fake_msg_obj = skin.clone_message_obj(prompt=prompt)
            
            # ========== 核心：使用真实的平台子类并注入客户端 ==========
            platform_name = skin.platform_meta.name
            fake_event = None
            
            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                
                # 防御性检查：如果没有偷到真实的 bot 客户端，直接放弃，避免炸掉整个流水线
                if not skin.bot:
                    logger.error("[主动说话] 缓存的 Bot 客户端为空，无法伪造 aiocqhttp Event。")
                    return None
                    
                # 传入所有必需参数，特别是那个偷来的 bot！
                fake_event = AiocqhttpMessageEvent(
                    message_str=prompt,
                    message_obj=fake_msg_obj,
                    platform_meta=skin.platform_meta,
                    session_id=skin.session_id,
                    bot=skin.bot  # 注入灵魂！
                )
            else:
                # 降级为基础 Event（走完全流程，但最后发不出消息）
                from astrbot.core.platform.astr_message_event import AstrMessageEvent
                fake_event = AstrMessageEvent(
                    message_str=prompt,
                    message_obj=fake_msg_obj,
                    platform_meta=skin.platform_meta,
                    session_id=skin.session_id,
                )
                logger.warning(f"[主动说话] 平台 {platform_name} 暂不支持主动说话降级处理。")
            # =======================================================

            # 贴标签和唤醒钢印
            fake_event.set_extra("is_implicit_proactive", True)
            fake_event.is_at_or_wake_command = True
            fake_event.is_wake = True
            
            return fake_event

        except Exception as e:
            logger.error(f"[主动说话] 伪造 Event 失败: {e}", exc_info=True)
            return None




    # ==========================================
    # 辅助方法
    # ==========================================
    # @filter.on_llm_response()
    def delete_space(self, event: AstrMessageEvent, resp: LLMResponse):
        """删除换行符，本来想提高聊天沉浸感，但对流式传输无效"""
        if resp.completion_text:
            result, count = re.subn(r'[\r\n]+', '', resp.completion_text)
            resp.completion_text = result
            # logger.info(f"删除了 {count} 个换行符")



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
                # logger.info(f"用户当前状态: {'正在输入' if new_status else '停止输入'}")
                return True
        except Exception as e:
            logger.info(f"解析输入状态异常: {e}")
            
        return False


    async def _handle_cleared_timeout(self, chat_id: str):
        """
        欲言又止回调：用户打了字又删了，准备温柔试探。
        注意：这是一个由 Scheduler 异步调用的普通方法，不是 AstrBot 的钩子。
        """
        logger.info(f"[主动说话-欲言又止] {chat_id} 准备处理...")
        
        try:
            # 1. 获取皮肤
            skin = self.scheduler.get_skin(chat_id)
            if not skin or not skin.is_ready():
                logger.warning(f"[主动说话-欲言又止] {chat_id} 皮肤未就绪。")
                return

            # 2. 偷偷拉取当前会话的真实历史记录（只要最后几条即可，不需要全量）
            history_context = []
            try:
                conv_mgr = self.context.conversation_manager
                curr_cid = await conv_mgr.get_curr_conversation_id(chat_id)
                if curr_cid:
                    conversation = await conv_mgr.get_conversation(chat_id, curr_cid)
                    if conversation and conversation.history:
                        import json
                        raw_history = json.loads(conversation.history)
                        # 只取最后 4 条，保留语境即可
                        history_context = raw_history[-4:]
            except Exception as e:
                logger.info(f"[主动说话-欲言又止] 拉取历史记录失败: {e}")

            # 3. 构建“绝杀” Prompt
            # 将历史塞给 LLM，让它理解语境，但严格限制它的回复方式
            history_str = "\n".join([f"{m.get('role', '?')}: {m.get('content', '')}" for m in history_context])
            
            proactive_prompt = (
                f"[底层系统提示：用户刚刚在输入框里编辑了一些内容，但最终默默删除了，什么都没发出来。]\n"
                "[执行指令：请结合上面的对话语境，以极其温柔、间接的方式试探一句。"
                "绝对禁止直接说‘你删了字’或‘你刚才打了什么’。"
                "可以用省略号开头，表现出你察觉到了什么，但又不想给对方压力的感觉。"
                "字数严格控制在20字以内，不要长篇大论。]"
            )

            # 4. 伪造 Event 并推入流水线
            fake_event = self._build_fake_event(skin, prompt=proactive_prompt)
            if not fake_event:
                return

            self.context._event_queue.put_nowait(fake_event)
            logger.info(f"[主动说话-欲言又止] {chat_id} 探测任务已推入流水线。")

        except Exception as e:
            logger.error(f"[主动说话-欲言又止] {chat_id} 处理失败: {e}", exc_info=True)

    # ==========================================
    # 生命周期管理
    # ==========================================
    async def terminate(self):
        await self.scheduler.terminate()
