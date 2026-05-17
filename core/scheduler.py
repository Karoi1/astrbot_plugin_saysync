import time
import asyncio
from typing import Optional, Dict

from .models import SessionContext, UserStatus, SchedulerResult
from .prompt_template import MesStatePack
from .models import _log

# ====== 日志开关：True 打印所有日志，False 静默 ======
enable_log = False


class SessionScheduler:
    """消息攒批调度器（后厨）。
    
    负责将同一 chat_id 的连续消息进行攒批（缓冲），通过超时定时器或队列满触发放行，
    并配合处理锁防止并发冲突。包含死锁检测与会话超时清理机制。
    """
    
    def __init__(self, max_size=3, expire_time=10, dead_lock_threshold=30, session_timeout=1800):    
        """
        初始化调度器。
        
        Args:
            max_size: 消息队列最大长度，达到后立刻放行。
            expire_time: 超时等待秒数，无新消息到达时触发放行。
            dead_lock_threshold: 死锁检测阈值（秒），超过则强制解锁。
            session_timeout: 会话闲置超时（秒），超时后清理会话资源。
        """
        self._sessions: Dict[str, SessionContext] = {}               # 每个 chat_id 对应一个 session，会话隔离
        self._user_status: Dict[str, UserStatus] = {}                # 每个 chat_id 对应的用户输入状态
        self._max_size = max_size                                    # 最大队列长度
        self._hold_time = expire_time                                # 超时后立刻下锅
        self._dead_lock_threshold = dead_lock_threshold              # 死锁检测阈值
        self._session_timeout = session_timeout                      # 会话闲置超时

    def _get_session(self, chat_id: str) -> SessionContext:
        """获取指定 chat_id 的 SessionContext，不存在则自动创建。"""
        if chat_id not in self._sessions:
            self._sessions[chat_id] = SessionContext()
        return self._sessions[chat_id]

    def _get_user_status(self, chat_id: str) -> UserStatus:
        """获取指定 chat_id 的 UserStatus，不存在则自动创建。"""
        if chat_id not in self._user_status:
            self._user_status[chat_id] = UserStatus()
        return self._user_status[chat_id]
    


    # ==========================================
    # 对外接口
    # ==========================================    

    def update_input_state(self, chat_id: str, is_typing: bool):
        """更新用户输入状态（由 aiocqhttp input_status 推送触发）。"""
        curr_status = self._get_user_status(chat_id)
        curr_status.set_state(is_typing)
        _log(enable_log, "info", f"[Scheduler][{chat_id}] 用户状态更新: {curr_status.sm.name}")

    def submit_message(self, chat_id: str, text: str) -> asyncio.Future:
        """提交一条消息进入攒批队列，返回一个 Future 供调用方挂起等待。
        
        逻辑：
        1. 杀死旧协程（KILL）。
        2. 将消息入队。
        3. 若队列已满，直接返回已设为 PROCESS 的 Future。
        4. 否则重置定时器，返回新的 Future 供挂起。
        
        Args:
            chat_id: 会话唯一标识。
            text: 已格式化的消息文本。
            
        Returns:
            asyncio.Future，其结果为 SchedulerResult.KILL 或 SchedulerResult.PROCESS。
        """
        
        session = self._get_session(chat_id)
        self._get_user_status(chat_id).set_mes_sent()
        
        # 1. 【杀旧】干掉正在傻等的旧协程
        if session.active_future and not session.active_future.done():
            session.active_future.set_result(SchedulerResult.KILL)
        
        # 2. 【攒货】无论死活，先把数据存下来
        session.message_queue.append(text)
        _log(enable_log, "info", f"[Scheduler][{chat_id}] 消息入队，当前数量: {len(session.message_queue)}") 

        # 3. 创建新的控制凭证
        new_future = asyncio.Future()

        # 4. 【容量检查】队列满了，不挂起，直接给一个“假凭证”让其立刻去打包
        if len(session.message_queue) >= self._max_size:
            _log(enable_log, "info", "Scheduler: 队列满，放行")
            session.active_future = new_future
            new_future.set_result(SchedulerResult.PROCESS) # 立刻唤醒
            return new_future

        # 5. 【正常挂起】重置定时器，挂起新凭证
        _log(enable_log, "info", "Scheduler: 挂起")
        self._reset_timer(session, chat_id)
        session.active_future = new_future
        return new_future

    def prepare_release(self, chat_id: str) -> Optional[MesStatePack]:
        """
        争抢放行权：检查死锁、打包消息、清理现场并上锁。
        
        这是多协程竞争的关键隘口，同一时间最多只有一个协程能成功打包并上锁。
        
        Args:
            chat_id: 会话唯一标识。
            
        Returns:
            争抢成功返回 MesStatePack（含消息列表与用户状态）；
            争抢失败（如死锁未解除、队列为空）返回 None。
        """
        session = self._sessions.get(chat_id)
        if not session:
            return None
        
        # 1. 【看门狗】检查死锁
        if session.is_processing:
            if time.time() - session.lock_timestamp > self._dead_lock_threshold:
                _log(enable_log, "warning", f"[Scheduler][{chat_id}] 检测到死锁(>{self._dead_lock_threshold}s)，暴力砸锁！")
                session.is_processing = False
            else:
                # 上一轮还在跑，当前事件放弃
                _log(enable_log, "info", f"[Scheduler][{chat_id}] 上一轮处理中，当前事件放弃争抢。")
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

        _log(enable_log, "info", f"[Scheduler][{chat_id}] 争抢成功，打包放行。消息数: {len(pack.messages)}")
        return pack

    def unlock_session(self, chat_id: str):
        """
        解锁指定会话，允许接收下一轮消息攒批。
        
        通常在 LLM 响应完成、消息发送完毕后调用。
        """
        session = self._sessions.get(chat_id)
        if session and session.is_processing:
            session.is_processing = False
            _log(enable_log, "info", f"[Scheduler][{chat_id}] 会话处理完成，已解锁。")


    # ==========================================
    # 内部方法
    # ==========================================

    def _reset_timer(self, session: SessionContext, chat_id: str):
        """重置超时计时器（取消旧的，创建新的）。"""
        if session.timer_task and not session.timer_task.done():
            session.timer_task.cancel()
        session.timer_task = asyncio.create_task(self._timer_expire(chat_id))
    
    async def _timer_expire(self, chat_id: str):
        """超时定时器到期回调：唤醒挂起的 Future 以触发 PROCESS。"""
        try:
            await asyncio.sleep(self._hold_time)
            session = self._sessions.get(chat_id)
            # 只有当有凭证在等待，且没有在处理中时，才唤醒
            if session and session.active_future and not session.active_future.done() and not session.is_processing:
                _log(enable_log, "info", f"[Scheduler][{chat_id}] 定时器到期，触发唤醒")
                session.active_future.set_result(SchedulerResult.PROCESS)
        except asyncio.CancelledError:
            pass # 被新消息重置了，静默退出
    

        
    # ==========================================
    # 看门狗内部方法
    # ==========================================

    def _feed_watchdog(self, chat_id: str):
        """喂狗：重置会话闲置计时器。"""
        session = self._sessions.get(chat_id)
        if not session:
            return
            
        if session.watchdog_task and not session.watchdog_task.done():
            session.watchdog_task.cancel()
            
        session.watchdog_task = asyncio.create_task(self._watchdog_expire(chat_id))
    
    async def _watchdog_expire(self, chat_id: str):
        """看门狗到期回调：清理长期闲置的会话资源。"""
        try:
            await asyncio.sleep(self._session_timeout)
            session = self._sessions.get(chat_id)
            
            # 安全检查：如果正在处理中（LLM正在思考），不能清理，放弃本次超时
            if session and session.is_processing:
                _log(enable_log, "warning", f"[Scheduler][{chat_id}] 看门狗超时，但会话正在处理中，跳过清理。")
                return
                
            # 安全检查：如果正在挂起等待（攒批），也不建议直接清理，虽然概率极小
            if session and session.active_future and not session.active_future.done():
                _log(enable_log, "warning", f"[Scheduler][{chat_id}] 看门狗超时，但存在挂起的Future，跳过清理。")
                return

            # 真正超时且处于空闲状态，执行清理
            if session:
                _log(enable_log, "info", f"[Scheduler][{chat_id}] 会话闲置超时(>{self._session_timeout}s)，执行清理回收。")
                self._cleanup_session(chat_id)
                
        except asyncio.CancelledError:
            pass # 被新消息重置（喂狗）了，静默退出

    def _cleanup_session(self, chat_id: str):
        """彻底清理一个会话的所有痕迹（消息队列、Future、Timer、Watchdog 等）。"""
        session = self._sessions.pop(chat_id, None)
        self._user_status.pop(chat_id, None)
        
        if session:
            if session.timer_task and not session.timer_task.done():
                session.timer_task.cancel()
            if session.watchdog_task and not session.watchdog_task.done():
                session.watchdog_task.cancel()
            if session.active_future and not session.active_future.done():
                session.active_future.set_result(SchedulerResult.KILL)

    async def terminate(self):
        """插件卸载时的清理工作：强制清理所有会话。"""
        # 清理排队和挂起
        for chat_id in list(self._sessions.keys()):
            self._cleanup_session(chat_id)
