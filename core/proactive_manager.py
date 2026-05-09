import asyncio
from typing import Dict

from .prompt_template import PROMPT4MENTALPULL
from .models import SessionSkin, ProactiveEventResult, ProactiveTask
from .models import _log

# ====== 日志开关：True 打印所有日志，False 静默 ======
enable_log = True

class Pack:
    def __init__(self, delay_task: asyncio.Task, timer: asyncio.Task, future: asyncio.Future):
        self.task = delay_task
        self.timer = timer
        self.future = future

    def _clean(self):
        if self.task:
            self.task.cancel()
        if self.timer:
            self.timer.cancel()
        future = self.future
        if future and not future.done():
            future.set_result(ProactiveEventResult.KILL)

class ProactiveManager:
    """
    ProactiveMgr的全权大总管。
    负责管理所有的主动任务（定时器、凭证、推流执行）
    """
    def __init__(self, context):
        """
        初始化ProactiveMgr大总管。
        
        Args:
            context: AstrBot 的主上下文，用于推流。
        """
        self._context = context
        self._skins: Dict[str, SessionSkin] = {}              # 按 chat_id 存储外壳
        
        # 核心字典：chat_id -> 任务元数据
        # task: 真正的 asyncio.Task
        # future: 控制它是否执行的“执行凭证”
        self._tasks: Dict[str, Pack] = {}

    # ==========================================
    # 对外接口
    # ==========================================    

    def submit_delay_task(self, task: ProactiveTask) -> None:
        """
        提交一个延迟执行的ProactiveMgr任务。
        
        这是一个极简接口，外部（如 main.py 的钩子）调用它后不管
        大总管内部会自动处理计时、劫持和推流。
        
        Args:
            task: ProactiveTask任务
            
        """
        # 1. 如果有旧任务，先静默杀掉（新任务覆盖旧任务，解决时序竞态问题）
        chat_id = task.chat_id
        self.cancel_task(chat_id)
        
        # 2. 创建执行凭证（默认阻塞，等待大总管的裁决）
        future = asyncio.Future()
        
        # 3. 创建worker，用future绑定worker
        delay_task = asyncio.create_task(self._task_worker(task, future))
        timer = asyncio.create_task(self._timer_handler(task.delay, future))
        self._tasks[chat_id] = Pack(delay_task, timer, future)
        
        _log(enable_log, "info", f"[ProactiveMgr] {chat_id} 已提交主动任务，延迟 {task.delay}秒。")

    def update_skin(self, chat_id: str, event) -> None:
        """
        从真实的 Event 中提取静态特征，更新或创建会话壳。
        由 Plugin 层调用。
        """
        if chat_id not in self._skins:
            self._skins[chat_id] = SessionSkin()
            
        skin = self._skins[chat_id]
        skin.platform_meta = event.platform_meta
        skin.msg_type = event.get_message_type()
        skin.self_id = event.get_self_id()
        skin.session_id = event.get_session_id()
        skin.group_id = event.get_group_id()
        skin.sender = event.message_obj.sender
        skin.unified_msg_origin = event.unified_msg_origin

        if skin.bot is None:
            skin.bot = getattr(event, 'bot', None)
            if skin.bot:
                _log(enable_log, "info", f"[ProactiveMgr] 已缓存 {chat_id} 的Bot对象。")

    def cancel_task(self, chat_id: str) -> None:
        """
        主动取消某个会话的主动任务。
        通常在用户发了真消息，或者用户重新开始打字时调用。
        
        Args:
            chat_id: 会话 ID。
        """
        pack = self._tasks.pop(chat_id, None)
        if pack:
            pack._clean()
            _log(enable_log, "info", f"[ProactiveMgr] {chat_id} 主动任务已被外部 Kill。")

    # ==========================================
    # 内部方法
    # ==========================================

    async def _timer_handler(self, delay: float, future: asyncio.Future):
        """
        底层计时器。时间一到，唤醒 Worker 去拿凭证。
        """
        if not future:
            return
        try:
            await asyncio.sleep(delay)
            if not future or future.done():
                return
            
            # 唤醒 Worker 去拿凭证
            future.set_result(ProactiveEventResult.PROCESS)
            _log(enable_log, "info", f"[ProactiveMgr] {delay}s 计时器到期，已发送 PROCESS 唤醒 Worker。")
            
        except asyncio.CancelledError:
            pass # Timer 被 cancel_task 强制 cancel，静默退出

    async def _task_worker(self, 
                           task: ProactiveTask,   # 任务
                           future: asyncio.Future # 凭证
                           ):
        """
        底层执行器：等待 -> 唤醒 -> 检查凭证 -> 推流。
        """
        if not future:
            _log(enable_log, "info", f"[ProactiveMgr] {task.chat_id} 凭证已消费或异常，跳过推流。")
            self._tasks.pop(task.chat_id, None)
            return
        
        # 看看future
        result = await future  # 等待result

        # 醒来了
        # Future给进，事件触发
        if result == ProactiveEventResult.PROCESS:
            _log(enable_log, "info", f"[ProactiveMgr] {task.chat_id} 凭证有效，准备推流。")
            await self._do_send(task)
        # 被Kill了，就不发送了
        elif result == ProactiveEventResult.KILL:
            _log(enable_log, "info", f"[ProactiveMgr] {task.chat_id} 凭证被 Kill，任务静默抛弃。")

        # 3. 无论如何，最后都要清理字典
        self._tasks.pop(task.chat_id, None)

    async def _do_send(self, task: ProactiveTask):
        """
        真正执行伪造 Event 并推入流水线。
        """
        try:
            # 1. 获取会话壳
            skin = self._skins[task.chat_id]
            if not skin or not skin.is_ready():
                _log(enable_log, "warning", f"[ProactiveMgr] {task.chat_id} 会话壳未就绪，取消推流。")
                return

            # 2. 伪造 Event
            decorated_prompt = PROMPT4MENTALPULL(task)
            fake_event = self._build_fake_event(skin, decorated_prompt)
            if not fake_event:
                return

            # 3. 推入 AstrBot 主事件队列
            # Care: 要是更新了这个方法说不定不行了，要关注
            self._context._event_queue.put_nowait(fake_event)
            _log(enable_log, "info", f"[ProactiveMgr] {task.chat_id} 已成功推入流水线。")

        except Exception as e:
            _log(enable_log, "error", f"[ProactiveMgr] {task.chat_id} 伪造 Event 或推流失败: {e}", exc_info=True)

    def _build_fake_event(self, skin, prompt: str):
        try:
            fake_msg_obj = skin.clone_message_obj(prompt=prompt)
            platform_name = skin.platform_meta.name
            fake_event = None
            
            if platform_name == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                
                if not skin.bot:
                    _log(enable_log, "error", "[ProactiveMgr] 缓存的 Bot 客户端为空，无法伪造 aiocqhttp Event。")
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
                _log(enable_log, "warning", f"[ProactiveMgr] 平台 {platform_name} 暂不支持ProactiveMgr降级处理。")
            
            fake_event.set_extra("is_implicit_proactive", True)
            fake_event.is_at_or_wake_command = True
            fake_event.is_wake = True
            
            return fake_event

        except Exception as e:
            _log(enable_log, "error", f"[ProactiveMgr] 伪造 Event 失败: {e}", exc_info=True)
            return None



    def get_skin(self, chat_id: str) -> 'SessionSkin | None':
        """获取指定会话的壳"""
        return self._skins.get(chat_id)

    async def terminate(self):
        """插件卸载时清理所有等待中的主动任务"""
        for _, pack in self._tasks.items():
            pack._clean()
        self._tasks.clear()
        _log(enable_log, "info", "[ProactiveMgr] 已清理所有等待中的主动任务。")