import time
import re

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain, ResultContentType

from .core import ProactiveManager
from .core.prompt_template import MesPack2prompt, PROMPT4ENV, PROMPT4MENTALPUSH
from .core import SessionScheduler
from .core.models import *
from .core.models import _log

# ====== 日志开关：True 打印所有日志，False 静默 ======
enable_log = False


@register("知音", "Robin", "安静倾听，告别一问一答的机械感", "1.0.1")
class SaySync(Star):
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
        # ========== 主动开口时，不进入等待队列 ==========
        if event.get_extra("is_implicit_proactive"):
            return 
        
        chat_id = event.unified_msg_origin
        self.proactive_mgr.update_skin(chat_id, event)

        # --- 分支 B：处理aiocqhttp推送 ---
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

        req.prompt = MesPack2prompt(pack)
        req.system_prompt += "\n" + PROMPT4ENV

        _log(enable_log, "info", f"[{event.unified_msg_origin}] LLM 请求已被劫持，替换为聚合 Prompt。消息数量:{len(pack.messages)},状态:{pack.user_state}")
        event.set_extra("chatqueue_pending", False)
        # 将当前system prompt人设标签贴上包裹
        event.set_extra("curr_sys_prompt", req.system_prompt)

    # ==========================================
    # 接线员 D：善后解锁Session
    # ==========================================
    @filter.on_llm_response(priority=100)
    async def force_unlock_on_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """解锁上锁的会话"""
        chat_id = event.unified_msg_origin
        self.scheduler.unlock_session(chat_id)

    # ==========================================
    # 接线员 E：主动说话引擎调度器
    # ==========================================
    @filter.after_message_sent(priority=999)
    async def trigger_proactive_check(self, event: AstrMessageEvent):
        """消息发送完毕后的钩子：寻找主动说话的机会（目前100%触发测试）"""
        if not event.is_private_chat():
            return
        # 不重复触发主动事件(?)
        if event.get_extra("is_implicit_proactive", False):
            """"""
            # return
        system_prompt = event.get_extra("curr_sys_prompt")
        # 发完消息后，事后复盘思绪
        logger.info(f"进入trigger")
        mindflow_all = await self.mindflow(chat_id=event.unified_msg_origin, context=self.context, system_prompt=system_prompt)
        logger.info(f"===============思绪\n{mindflow_all}")
    # ==========================================
    # 核心辅助方法
    # ==========================================

    async def mindflow(self, chat_id: str, context: Context, system_prompt: str, len_hist: int = -1):
        history = []

        provider_id = await context.get_current_chat_provider_id(chat_id)
        conv_mgr = context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(chat_id)

        if curr_cid:
            conversation = await conv_mgr.get_conversation(chat_id, curr_cid)
            if conversation and conversation.history:
                import json
                raw_history = json.loads(conversation.history)
                if len_hist < 0:
                    # 加入所有历史会话
                    history = raw_history
                else:
                    history = raw_history[-(len_hist)*2:]
        
        llm_result = await context.llm_generate(
            chat_provider_id = provider_id,
            prompt = PROMPT4MENTALPUSH,
            system_prompt = system_prompt,
            contexts = history
        )

        return llm_result.completion_text

    
    async def Mes2PTask(self, chat_id:str, text: str) -> tuple[str, ProactiveTask]:
        """
        从字符串中提取ProactiveTask  
        从长字符串中提取所有 &&PRO&&...&&END&& 模式，
        返回清理后的文本和提取出的参数列表。

        Return: (str, ProactiveTask)
        """
    
        # 容错正则：
        # - \s* 允许字段前后有空格/换行
        # - (?P<name>...) 命名捕获组，方便取值
        # - delay/type 用 [^,]+ 匹配（值本身不含逗号）
        # - mind 用 .*? 非贪婪匹配到 &&END&&（允许值内有逗号）
        pattern = re.compile(
            r'&&PRO&&\s*'
            r'delay\s*=\s*(?P<delay>[^,]*?)\s*,\s*'
            r'type\s*=\s*(?P<type>[^,]*?)\s*,\s*'
            r'mind\s*=\s*(?P<mind>.*?)'
            r'&&END&&',
            re.DOTALL  # 允许 . 匹配换行符，支持多行 mind
        )
        
        _tasks = []
        type_map = {pt.name.lower(): pt for pt in ProactiveType}
        def replacer(match):
        # 提取并清理字段
            delay_str = match.group('delay').strip()
            type_str = match.group('type').strip()
            prompt_str = match.group('mind').strip()
            
            # 解析 delay，容错处理
            try:
                delay = float(delay_str) if delay_str else 20.0
            except ValueError:
                delay = 20.0  # 默认值
            
            # 解析 type，容错处理, 找不到默认SUPPLEMENT
            task_type = type_map.get(type_str.lower())

            if task_type is None:
                _log(enable_log, "warning", f"收到非法的 ProactiveType 值 '{type_str}'，已回退到默认值 '{ProactiveType.SUPPLEMENT.name}'")
                task_type = ProactiveType.SUPPLEMENT
            
            # 创建实例
            task = ProactiveTask(
                chat_id=chat_id,
                task_type=task_type,
                instruction=prompt_str,
                delay=delay
            )
            _tasks.append(task)
            return ''  # 从原字符串中删除该 pattern
    
        cleaned_text = pattern.sub(replacer, text)
        return cleaned_text, _tasks

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