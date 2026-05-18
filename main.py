import time
import re
import json
import ast
import os

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core import EventForger, ProactiveManager
from .signals import SignalContext, get_default_router
from .core.prompt_template import MesPack2prompt, PROMPT4ENV, PROMPT4MENTALPUSH, PROMPT4IMPULSE
from .core import SessionScheduler
from .core.models import *
from .core.models import _log

# ====== 日志开关：True 打印所有日志，False 静默 ======
enable_log = False


@register("知音", "Robin", "安静倾听，告别一问一答的机械感", "1.0.1")
class SaySync(Star):
    """知音 / SaySync 插件主类。
    
    通过 5 个 AstrBot Hook（接线员 A~E）串联整个主动倾听与主动开口流程：
    - A & B（on_message）：事件总入口，攒批消息并挂起协程。
    - C（hijack_llm_request）：劫持 LLM 请求，替换为聚合 Prompt。
    - D（force_unlock_on_resp）：LLM 响应后解锁会话。
    - E（trigger_proactive_check）：消息发送完毕后触发主动说话复盘。
    
    Attributes:
        scheduler: 消息攒批调度器实例。
        forger: 事件伪造器实例。
        proactive_mgr: 主动任务管理器实例。
    """
    def __init__(self, context: Context):
        """初始化 SaySync 插件及内部核心组件。"""
        super().__init__(context)

        self.scheduler = SessionScheduler(
            max_size=3, 
            expire_time=10.0, 
            dead_lock_threshold=60.0,
        )
        self.forger = EventForger()
        self.proactive_mgr = ProactiveManager(
            context=self.context,
            forger=self.forger
        )
        self.signal_router = get_default_router(
            data_dir=os.path.join(get_astrbot_data_path(), "plugin_data", "astrbot_plugin_saysync")
        )

    # ==========================================
    # 接线员 A & B：事件总入口
    # ==========================================
    @filter.event_message_type(filter.EventMessageType.ALL, priority=-10)
    async def on_message(self, event: AstrMessageEvent):
        """私聊消息总入口：挂起最后一个收到的消息，实现攒批与状态感知。
        
        分支 B：处理 aiocqhttp 空消息（input_status 等 notice/notify 推送），更新用户输入状态。
        分支 A：处理正常文本消息，打包后提交给 SessionScheduler 挂起等待。
        
        Args:
            event: AstrBot 消息事件。
        """
        # ========== 主动开口时，不进入等待队列 ==========
        if event.get_extra("is_implicit_proactive"):
            return 
        
        chat_id = event.unified_msg_origin
        if not self.forger.get_skin(chat_id):
            self.forger.update_skin(chat_id, event)

        # --- 分支 B：处理aiocqhttp推送 ---
        if not event.message_str:
            result = await self._handle_platform_signal(event, chat_id)
            if result:
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
        """劫持 LLM 请求：将单条 prompt 替换为攒批聚合 Prompt，并追加环境感知 Prompt。
        
        仅对带有 chatqueue_pending 暗号的事件生效。
        
        Args:
            event: AstrBot 消息事件。
            req: ProviderRequest 实例。
        """
        if not event.get_extra("chatqueue_pending"):
            return 

        pack = event.get_extra("chatqueue_pack")
        if not pack:
            return

        req.prompt = MesPack2prompt(pack)
        req.system_prompt += "\n" + PROMPT4ENV

        _log(enable_log, "info", f"[{event.unified_msg_origin}] LLM 请求已被劫持，替换为聚合 Prompt。消息数量:{len(pack.messages)},状态:{pack.user_state}")
        event.set_extra("chatqueue_pending", False)
        # 将当前 system prompt 人设标签贴上包裹
        event.set_extra("curr_sys_prompt", req.system_prompt)

    # ==========================================
    # 接线员 D：善后解锁 Session
    # ==========================================
    @filter.on_llm_response(priority=100)
    async def force_unlock_on_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """LLM 响应后解锁上锁的会话，允许下一轮消息进入攒批。
        
        Args:
            event: AstrBot 消息事件。
            resp: LLM 响应实例。
        """
        chat_id = event.unified_msg_origin
        self.scheduler.unlock_session(chat_id)

    # ==========================================
    # 接线员 E：主动说话引擎调度器
    # ==========================================
    @filter.on_llm_response(priority=999)
    async def trigger_proactive_check(self, event: AstrMessageEvent, resp: LLMResponse):
        """消息发送完毕后的钩子：寻找主动说话的机会（事后复盘）。
        
        读取当前会话历史，用 PROMPT4IMPULSE 让 LLM 判断是否有延时冲动，
        若存在则将冲动转化为 ProactiveTask 提交给 ProactiveManager 延迟执行。
        
        Args:
            event: AstrBot 消息事件。
            resp: LLM 响应实例。
        """
        if not event.is_private_chat():
            return
        # 不重复触发主动事件
        if event.get_extra("is_implicit_proactive"):
            return
        
        # 从包裹上取下人设标签
        system_prompt = event.get_extra("curr_sys_prompt")
        # 发完消息后，事后复盘思绪
        logger.info(f"进入trigger")
        mindflow_Ptask = await self.mindflow(chat_id=event.unified_msg_origin, context=self.context, system_prompt=system_prompt)
        
        if mindflow_Ptask:
            self.proactive_mgr.submit_delay_task(mindflow_Ptask)
        else:
            logger.info(f"没有mindflow")

    # ==========================================
    # 核心辅助方法
    # ==========================================

    async def mindflow(self, chat_id: str, context: Context, system_prompt: str, len_hist: int = -1) -> ProactiveTask | None:
        """主动说话复盘核心：读取历史 -> 判断冲动 -> 生成草稿。
        
        流程：
        1. 读取当前会话历史（通过 conversation_manager）。
        2. 用 PROMPT4IMPULSE 让 LLM 判断是否有延时冲动。
        3. 若 has_impulse=true，用 PROMPT4MENTALPUSH 将冲动转化为具体草稿（instruction）。
        4. 封装为 ProactiveTask 返回。
        
        Args:
            chat_id: 会话唯一标识。
            context: AstrBot 主上下文。
            system_prompt: 当前生效的系统提示词（人设）。
            len_hist: 读取历史轮数（-1 表示全部）。
            
        Returns:
            若 LLM 产生有效冲动则返回 ProactiveTask，否则返回 None。
        """
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
                    # 加入最后 n 对会话（一对=一问一答）
                    history = raw_history[-(len_hist)*2:]
        
        mind = await context.llm_generate(
            chat_provider_id = provider_id,
            prompt = PROMPT4IMPULSE,
            system_prompt = system_prompt,
            contexts = history
        )
        new_task = await self.Mes2PTask(chat_id, mind.completion_text)
        if not new_task:
            return None
        
        instruction = await context.llm_generate(
            chat_provider_id = provider_id,
            prompt = PROMPT4MENTALPUSH(new_task),
            system_prompt = system_prompt,
            contexts = history
        )
        new_task.instruction = instruction.completion_text

        return new_task
        

    
    async def Mes2PTask(self, chat_id: str, text: str) -> ProactiveTask | None:
        """将 LLM 输出的冲动判断 JSON 解析为 ProactiveTask。
        
        具备三级容错：
        1. 标准 json.loads；
        2. 去除尾随逗号后重试；
        3. 降级为 ast.literal_eval 处理 Python 风格字面量。
        
        Args:
            chat_id: 会话唯一标识。
            text: LLM 返回的原始文本（可能含 Markdown 代码块）。
            
        Returns:
            解析成功且校验通过返回 ProactiveTask，否则返回 None。
        """
        # 1. 预处理：移除 LLM 常见的 Markdown 代码块包裹
        clean_text = re.sub(r'```(?:json)?\s*', '', text)
        clean_text = re.sub(r'```', '', clean_text)
        
        # 2. 正则表达式提取目标 JSON 块
        # 容错设计：允许单/双引号、true/false大小写、匹配最内层{}
        pattern = r"""[\{][^{}]*['"]has_impulse['"]\s*:\s*(true|false)[^{}]*[\}]"""
        match = re.search(pattern, clean_text, re.IGNORECASE | re.DOTALL)
        
        if not match:
            return None
            
        json_str = match.group(0)
        
        # 3. LLM JSON 容错修复：去除尾随逗号
        json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
        
        # 4. 健壮的 JSON 解析
        data = None
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            # 降级处理：应对单引号或 Python 风格的 True/False
            try:
                py_str = json_str.replace('true', 'True').replace('false', 'False').replace('null', 'None')
                data = ast.literal_eval(py_str)
            except (ValueError, SyntaxError):
                return None
                
        if not data or not isinstance(data, dict):
            return None
            
        # 5. 判断第一种 pattern：无冲动
        if not data.get("has_impulse", False):
            return None
            
        # 6. 判断第二种 pattern：有冲动，进行严格字段校验与映射
        try:
            # 提取并校验 delay (10~30整数)，找不到就=20
            raw_delay = data.get("delay", 20)
            delay_val = int(float(str(raw_delay).strip()))
                
            # 提取并校验 level (1~5整数，需映射为枚举)
            raw_level = data.get("level")
            if raw_level is None:
                return None
            level_val = int(float(str(raw_level).strip()))
            if not (1 <= level_val <= 5):
                return None
                
            # 匹配 ProactiveLevel 枚举
            task_level = None
            for member in ProactiveLevel:
                if member.level == level_val:
                    task_level = member
                    break
            if task_level is None:
                return None
                
            # 提取并校验 type (精准匹配大写枚举名)
            raw_type = str(data.get("type", "")).strip().upper()
            if not raw_type:
                return None
            task_type = ProactiveType[raw_type]  # 抛出 KeyError 说明不在枚举库中
                
            # 提取并校验 mind (必须有值)
            mindflow_str = str(data.get("mind", "")).strip()
            if not mindflow_str:
                return None
                
            # 原始 JSON 中无 instruction，按业务逻辑默认给空字符串
            
            return ProactiveTask(
                chat_id=chat_id,
                task_type=task_type,
                instruction="",
                mindflow=mindflow_str,
                level=task_level,
                delay=float(delay_val)
            )
            
        except (KeyError, ValueError, TypeError):
            # 字段缺失、类型转换失败、或枚举不匹配，均视为无完全匹配
            return None

    async def _handle_platform_signal(self, event: AstrMessageEvent, chat_id: str) -> bool:
        """将 aiocqhttp notice/notify 推送委托给 SignalRouter 分发。
        
        提取 raw_message 中的 go-cqhttp 原始数据，交由已注册的全部 Handler 处理。
        当前生效的 Handler：TypingHandler（input_status）、PokeHandler 等共 12 种。
        
        Args:
            event: AstrBot 消息事件。
            chat_id: 会话唯一标识。
            
        Returns:
            True 表示信号被 SignalRouter 识别并分发到了对应 Handler。
        """
        if event.get_platform_name() != "aiocqhttp":
            return False
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            assert isinstance(event, AiocqhttpMessageEvent)
            raw = event.message_obj.raw_message
        except Exception:
            return False

        if not isinstance(raw, dict):
            #logger.warning(f"aiocqhttp 推送的 raw_message 不是 dict，无法解析: {type(raw)}")
            return False
        #logger.info(f"收到 aiocqhttp 推送，尝试解析信号: {raw}")
        ctx = SignalContext.from_components(self)
        result = await self.signal_router.dispatch(raw, chat_id, ctx)
        return result

    # ==========================================
    # 生命周期管理
    # ==========================================
    
    async def terminate(self):
        """插件卸载时的清理工作：取消定时器、杀死挂起的 Future、清理主动任务。"""
        await self.scheduler.terminate()
        await self.proactive_mgr.terminate()
