import time
import asyncio
from typing import Optional, Dict

from astrbot.api import logger

from .models import SessionContext, UserStatus, SchedulerResult, SessionSkin, ProactiveTask, ProactiveType
from .prompt_template import MesStatePack


class SessionScheduler:
    """后厨，正在思考如何下锅。"""
    _sessions: Dict[str, SessionContext] = {}         # 每个chat_id对应一个session，会话隔离
    _user_status: Dict[str, UserStatus] = {}         # 每个chat_id对应的用户输入状态
    _skins: Dict[str, SessionSkin] = {}              # 按 chat_id 存储皮肤外壳
    _max_size: int = 3                               # 最大队列长度
    _expire_time: float = 10                         # 超时后立刻下锅
    _dead_lock_threshold = 30                         # 30秒没收到LLM回复，锅烧糊了，得关火下新的
    
    def __init__(self, max_size=3, expire_time=10, dead_lock_threshold=30, on_proactive_ready=None):
        self._sessions = {}         
        self._user_status = {}  
        self._skins = {}   
        self._max_size = max_size    
        self._expire_time = expire_time     
        self._dead_lock_threshold = dead_lock_threshold         
        
        # ========== 统一的主动说话引擎底层 ==========
        self._proactive_tasks: Dict[str, asyncio.Task] = {} 
        self._on_proactive_ready = on_proactive_ready  # 主逻辑统一的接单回调
        # =======================================================================

    def _get_session(self, chat_id: str) -> SessionContext:
        """找chat_id对应session。这桌客户之前点没点过菜来着"""
        if chat_id not in self._sessions:
            self._sessions[chat_id] = SessionContext()
        return self._sessions[chat_id]

    def _get_user_status(self, chat_id: str) -> UserStatus:
        """找chat_id对应输入状态。客户要什么定制服务来着"""
        if chat_id not in self._user_status:
            self._user_status[chat_id] = UserStatus()
        return self._user_status[chat_id]
    
    def get_skin(self, chat_id: str) -> 'SessionSkin | None':
        """获取指定会话的皮肤"""
        return self._skins.get(chat_id)

    # ==========================================
    # 对外接口
    # ==========================================    

    def update_input_state(self, chat_id: str, is_typing: bool):
        """插件服务员说客户想自定口味，更新chat_id对应session的输入状态"""
        curr_status = self._get_user_status(chat_id)
        curr_status.set_state(is_typing)
        logger.debug(f"[Scheduler][{chat_id}] 用户状态更新: {curr_status.sm.name}")

        # ========== 欲言又止感知逻辑 -> 转化为提交主动任务 ==========
        if curr_status.sm in (UserStatus.StateMachine.cleared, UserStatus.StateMachine.cleared_sure):
            logger.info(f"[ProactiveTask][{chat_id}] 检测到欲言又止，提交延迟主动任务。")
            self._start_cleared_timer(chat_id)
        elif curr_status.sm == UserStatus.StateMachine.typing:
            # 如果用户又开始打字了，立刻杀掉当前可能存在的主动任务
            logger.debug(f"[ProactiveTask][{chat_id}] 用户恢复输入，取消主动任务。")
            self._cancel_proactive_task(chat_id)

    def update_skin(self, chat_id: str, event) -> None:
        """
        从真实的 Event 中提取静态特征，更新或创建会话皮肤。
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
                logger.info(f"[主动说话] 已缓存 {chat_id} 的真实 Bot 客户端。")

    def submit_message(self, chat_id: str, text: str) -> asyncio.Future:
        """插件服务员送来了某桌客户的餐牌，挂起餐牌等待入锅"""
        # ========== 防御：发了真消息，立刻取消任何主动任务等待 ==========
        self._cancel_proactive_task(chat_id)
        # =======================================================================
        
        session = self._get_session(chat_id)
        self._get_user_status(chat_id).set_mes_sent()
        
        # 1. 【杀旧】干掉正在傻等的旧协程
        if session.active_future and not session.active_future.done():
            session.active_future.set_result(SchedulerResult.KILL)
        
        # 2. 【攒货】无论死活，先把数据存下来
        session.message_queue.append(text)
        logger.info(f"[Scheduler][{chat_id}] 消息入队，当前数量: {len(session.message_queue)}") 

        # 3. 创建新的控制凭证
        new_future = asyncio.Future()

        # 4. 【容量检查】队列满了，不挂起，直接给一个“假凭证”让其立刻去打包
        if len(session.message_queue) >= self._max_size:
            logger.info("Scheduler: 队列满，放行")
            session.active_future = new_future
            new_future.set_result(SchedulerResult.PROCESS) # 立刻唤醒
            return new_future

        # 5. 【正常挂起】重置定时器，挂起新凭证
        logger.info("Scheduler: 挂起")
        self._reset_timer(session, chat_id)
        session.active_future = new_future
        return new_future

    def prepare_release(self, chat_id: str) -> Optional[MesStatePack]:
        """
        要准备下锅了，正在考虑先做哪个菜。每桌在某一时刻最多下一锅，下锅上锁
        争抢放行权，抢夺成功就给Session上锁（最多dead_lock_threshold秒），成功就返回pack，否则返回None
        """
        session = self._sessions.get(chat_id)
        if not session:
            return None
        
        # 1. 【看门狗】检查死锁
        if session.is_processing:
            if time.time() - session.lock_timestamp > self._dead_lock_threshold:
                logger.warning(f"[Scheduler][{chat_id}] 检测到死锁(>{self._dead_lock_threshold}s)，暴力砸锁！")
                session.is_processing = False
            else:
                # 上一轮还在跑，当前事件放弃
                logger.info(f"[Scheduler][{chat_id}] 上一轮处理中，当前事件放弃争抢。")
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

        logger.info(f"[Scheduler][{chat_id}] 争抢成功，打包放行。消息数: {len(pack.messages)}")
        return pack

    def unlock_session(self, chat_id: str):
        """
        下锅抄完，把大盘菜端上来
        LLM回复完咯，当前session可解锁
        """
        session = self._sessions.get(chat_id)
        if session and session.is_processing:
            session.is_processing = False
            logger.info(f"[Scheduler][{chat_id}] 会话处理完成，已解锁。")

    async def terminate(self):
        """插件卸载时的清理工作"""
        # 清理排队和挂起
        for chat_id, session in self._sessions.items():
            if session.timer_task and not session.timer_task.done():
                session.timer_task.cancel()
            if session.active_future and not session.active_future.done():
                session.active_future.set_result(SchedulerResult.KILL)
        
        # 清理统一的主动任务计时器（修复了旧版的 _cleared_timers 拼写错误）
        for chat_id, task in self._proactive_tasks.items():
            if not task.done():
                task.cancel()
        self._proactive_tasks.clear()

    # ==========================================
    # 内部方法
    # ==========================================

    def _reset_timer(self, session: SessionContext, chat_id: str):
        """重置超时计时器"""
        if session.timer_task and not session.timer_task.done():
            session.timer_task.cancel()
        session.timer_task = asyncio.create_task(self._timer_expire(chat_id))

    async def _timer_expire(self, chat_id: str):
        """定时器到期回调"""
        try:
            await asyncio.sleep(self._expire_time)
            session = self._sessions.get(chat_id)
            # 只有当有凭证在等待，且没有在处理中时，才唤醒
            if session and session.active_future and not session.active_future.done() and not session.is_processing:
                logger.info(f"[Scheduler][{chat_id}] 定时器到期，触发唤醒")
                session.active_future.set_result(SchedulerResult.PROCESS)
        except asyncio.CancelledError:
            pass # 被新消息重置了，静默退出


    def _start_cleared_timer(self, chat_id: str):
        """启动或重置欲言又止任务（底层走统一提交）"""
        task = ProactiveTask(
            chat_id=chat_id,
            task_type=ProactiveType.CLEARED,
            instruction="[底层系统提示：用户刚才在输入框里编辑了内容，但最终默默删除了。请结合语境温柔试探一句，绝对禁止直接点破，20字以内。]",
            delay=20.0
        )
        self._submit_proactive_task(task)

    def _submit_proactive_task(self, task: ProactiveTask):
        """提交一个主动说话任务（带延迟）。如果该会话已有任务，会被新任务顶掉。"""
        self._cancel_proactive_task(task.chat_id) # 统一取消旧任务
        self._proactive_tasks[task.chat_id] = asyncio.create_task(
            self._proactive_engine(task)
        )
        logger.info(f"[ProactiveTask][{task.chat_id}] 已提交任务: {task.task_type.value}，延迟 {task.delay}s")

    def _cancel_proactive_task(self, chat_id: str):
        """取消当前会话的主动说话任务（发真消息或重新打字时调用）"""
        task = self._proactive_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _proactive_engine(self, task: ProactiveTask):
        """统一的主动说话引擎：等待 -> 唤醒主逻辑 -> 推流"""
        try:
            await asyncio.sleep(task.delay)
            logger.info(f"[ProactiveTask][{task.chat_id}] 任务 {task.task_type.value} 延迟结束，唤醒主逻辑。")
            # 唤醒主逻辑
            if self._on_proactive_ready:
                asyncio.create_task(self._on_proactive_ready(task))
            else:
                logger.warning(f"[ProactiveTask][{task.chat_id}] 引擎未绑定主逻辑回调！")
        except asyncio.CancelledError:
            pass # 被正常取消，静默退出
        finally:
            self._proactive_tasks.pop(task.chat_id, None)