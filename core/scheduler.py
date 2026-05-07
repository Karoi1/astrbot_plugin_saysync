import time
import asyncio
from typing import Optional, Dict

from astrbot.api import logger

from .models import SessionContext, UserStatus,SchedulerResult
from .prompt_template import MesStatePack


class SessionScheduler:
    """后厨，正在思考如何下锅。"""
    _sessions: Dict[str, SessionContext] = {}         # 每个chat_id对应一个session，会话隔离
    _user_status: Dict[str, UserStatus] = {}         # 每个chat_id对应的用户输入状态
    _max_size: int = 3                               # 最大队列长度
    _expire_time: float = 10                         # 超时后立刻下锅
    dead_lock_threshold = 30                         # 30秒没收到LLM回复，锅烧糊了，得关火下新的

    def __init__(self, max_size=3, expire_time=10, dead_lock_threshold=30):
        pass

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

    # ==========================================
    # 对外接口
    # ==========================================    

    def update_input_state(self, chat_id: str, is_typing: bool):
        """插件服务员说客户想自定口味，更新chat_id对应session的输入状态"""
        curr_status = self._get_user_status(chat_id)
        curr_status.set_state(is_typing)
        logger.debug(f"[Scheduler][{chat_id}] 用户状态更新: {curr_status.sm.name}")

    def submit_message(self, chat_id: str, text: str) -> asyncio.Future:
        """插件服务员送来了某桌客户的餐牌，挂起餐牌等待入锅"""
        session = self._get_session(chat_id)
        send_flag = self._get_user_status(chat_id).set_mes_sent()
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
                logger.debug(f"[Scheduler][{chat_id}] 上一轮处理中，当前事件放弃争抢。")
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
            # 注意：这里不需要主动触发下一轮。
            # 如果在处理期间有新消息，它们会乖乖躺在队列里。
            # 等待下一次用户发消息或定时器自然触发时，prepare_release 会发现锁开了且队列有货，自然接管。

    async def terminate(self):
        """插件卸载时的清理工作"""
        for chat_id, session in self._sessions.items():
            if session.timer_task and not session.timer_task.done():
                session.timer_task.cancel()
            if session.active_future and not session.active_future.done():
                session.active_future.set_result(SchedulerResult.KILL)

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
                logger.debug(f"[Scheduler][{chat_id}] 定时器到期，触发唤醒")
                session.active_future.set_result(SchedulerResult.PROCESS)
        except asyncio.CancelledError:
            pass # 被新消息重置了，静默退出
