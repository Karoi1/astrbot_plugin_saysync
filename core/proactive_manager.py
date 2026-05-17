import asyncio
from typing import Dict

from .prompt_template import PROMPT4MENTALPULL
from .models import ProactiveEventResult, ProactiveTask
from .models import _log
from .Event_Forger import EventForger

# ====== 日志开关：True 打印所有日志，False 静默 ======
enable_log = True


class Pack:
    """主动任务元数据包。
    
    封装一个主动说话任务所需的三个协程对象：
    task（执行 Worker）、timer（计时器）、future（执行凭证）。
    """
    def __init__(self, delay_task: asyncio.Task, timer: asyncio.Task, future: asyncio.Future):
        self.task = delay_task
        self.timer = timer
        self.future = future

    def _clean(self):
        """清理本任务的所有协程资源：取消 task 和 timer，若 future 未完成则设为 KILL。"""
        if self.task:
            self.task.cancel()
        if self.timer:
            self.timer.cancel()
        future = self.future
        if future and not future.done():
            future.set_result(ProactiveEventResult.KILL)


class ProactiveManager:
    """主动说话任务管理器（大总管）。
    
    负责管理所有主动说话任务的延迟下发、计时、凭证控制与最终推流执行。
    每个 chat_id 同一时刻最多只有一个待执行的主动任务（新任务会覆盖旧任务）。
    """
    def __init__(self, context, forger: EventForger):
        """
        初始化主动任务管理器。
        
        Args:
            context: AstrBot 的主上下文，用于推流。
            forger: EventForger 实例，用于伪造主动说话的 Event。
        """
        self._context = context
        self.forger = forger
        # 核心字典：chat_id -> Pack
        # task: 真正的 asyncio.Task
        # future: 控制它是否执行的“执行凭证”
        self._tasks: Dict[str, Pack] = {}

    # ==========================================
    # 对外接口
    # ==========================================    

    def submit_delay_task(self, task: ProactiveTask) -> None:
        """
        提交一个延迟执行的主动说话任务。
        
        这是一个极简接口，外部（如 main.py 的钩子）调用后无需再管。
        内部会自动处理计时、凭证裁决和推流。
        
        Args:
            task: ProactiveTask 任务实例。
        """
        # 1. 如果有旧任务，先静默杀掉（新任务覆盖旧任务，解决时序竞态问题）
        chat_id = task.chat_id
        self.cancel_task(chat_id)
        
        # 2. 创建执行凭证（默认阻塞，等待大总管的裁决）
        future = asyncio.Future()
        
        # 3. 创建 worker，用 future 绑定 worker
        delay_task = asyncio.create_task(self._task_worker(task, future))
        timer = asyncio.create_task(self._timer_handler(task.delay, future))
        self._tasks[chat_id] = Pack(delay_task, timer, future)
        
        _log(enable_log, "info", f"[ProactiveMgr] {chat_id} 已提交主动任务，延迟 {task.delay}秒。")

    def cancel_task(self, chat_id: str) -> None:
        """
        主动取消某个会话的待执行主动任务。
        
        通常在用户发了新消息、或用户重新开始打字时调用，避免旧任务干扰当前对话。
        
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
        底层计时器：sleep 指定秒数后，将 future 设为 PROCESS 以唤醒 Worker。
        
        Args:
            delay: 延迟秒数。
            future: 与 Worker 绑定的凭证 Future。
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
        底层执行器：等待凭证 -> 被唤醒 -> 检查凭证结果 -> 决定是否推流。
        
        Args:
            task: 主动说话任务。
            future: 控制是否执行的凭证 Future。
        """
        if not future:
            _log(enable_log, "info", f"[ProactiveMgr] {task.chat_id} 凭证已消费或异常，跳过推流。")
            self._tasks.pop(task.chat_id, None)
            return
        
        # 看看 future
        result = await future  # 等待 result

        # 醒来了
        # Future 给进，事件触发
        if result == ProactiveEventResult.PROCESS:
            _log(enable_log, "info", f"[ProactiveMgr] {task.chat_id} 凭证有效，准备推流。")
            await self._do_send(task)
        # 被 Kill 了，就不发送了
        elif result == ProactiveEventResult.KILL:
            _log(enable_log, "info", f"[ProactiveMgr] {task.chat_id} 凭证被 Kill，任务静默抛弃。")

        # 3. 无论如何，最后都要清理字典
        self._tasks.pop(task.chat_id, None)

    async def _do_send(self, task: ProactiveTask):
        """
        真正执行伪造 Event 并推入 AstrBot 主事件队列。
        
        流程：
        1. 检查会话壳是否就绪。
        2. 用 EventForger 伪造 Event，并打上主动推流标签。
        3. 推入 AstrBot 主事件队列。
        
        Args:
            task: 主动说话任务。
        """
        try:
            # 1. 获取会话壳
            skin = self.forger.get_skin(task.chat_id)
            if not skin or not skin.is_ready():
                _log(enable_log, "warning", f"[ProactiveMgr] {task.chat_id} 会话壳未就绪，取消推流。")
                return

            # 2. 伪造 Event
            decorated_prompt = PROMPT4MENTALPULL(task)
            fake_event = self.forger.forge_event(task.chat_id, decorated_prompt)
            if not fake_event:
                return
            

            fake_event.set_extra("is_implicit_proactive", True) # 主动推流标签
            fake_event.is_at_or_wake_command = True
            fake_event.is_wake = True
            # 3. 推入 AstrBot 主事件队列
            # Care: 关注更新，此处使用了 context.get_event_queue   #7修改
            await self._context.get_event_queue().put(fake_event)
            _log(enable_log, "info", f"[ProactiveMgr] {task.chat_id} 已成功推入流水线。")

        except Exception as e:
            _log(enable_log, "error", f"[ProactiveMgr] {task.chat_id} 伪造 Event 或推流失败: {e}", exc_info=True)

    async def terminate(self):
        """插件卸载时清理所有等待中的主动任务。"""
        for _, pack in self._tasks.items():
            pack._clean()
        self._tasks.clear()
        _log(enable_log, "info", "[ProactiveMgr] 已清理所有等待中的主动任务。")
