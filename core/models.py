import asyncio
from enum import Enum
from typing import Dict

class UserStatus:
    # 从用户输入状态，判断用户想要什么定制化服务
    class StateMachine(Enum):
        idle = 0               # 没打字/发送完消息后停止
        typing = 1             # 正在打字
        cleared = 2            # 打了字又删了，欲言又止
        cleared_sure = 3       # 同上，过滤用

    def __init__(self):
        self._mes_sent: bool = False
        self.sm: self.StateMachine = self.StateMachine.idle

    def set_mes_sent(self):
        self.reset()
    
    def set_state(self, typing: bool):
        self.state_transfer(typing)

    def state_transfer(self, _input_status: bool):
        """状态转移。当前在打字吗？会发送还是默默删除呢"""
        # 如果发送了消息，就回到最初状态
        if self._mes_sent:
            self.reset()
            return

        # 正在开始打字
        if self.sm == self.StateMachine.idle:
            if _input_status:
                self.sm = self.StateMachine.typing
            # else stay idle

        # 正在打字 -> 继续打字/欲言又止
        elif self.sm == self.StateMachine.typing:
            if self._input_status:
                self.sm = self.StateMachine.typing
            else:
                self.sm = self.StateMachine.cleared

        # 欲言又止 -> 真·欲言又止（过滤）
        elif self.sm == self.StateMachine.cleared:
            if self._input_status:
                self.sm = self.StateMachine.cleared_sure   # 第一次收到输入，进入过渡态
            # else stay cleared

        # 真·欲言又止 -> 正在打字
        elif self.sm == self.StateMachine.cleared_sure:
            if self._input_status:
                self.sm = self.StateMachine.typing          # 再次收到输入，确认进入 typing
            else:
                self.sm = self.StateMachine.cleared         # 输入消失，退回 cleared

        else:
            # 容错：回归初始
            self.reset()

    def reset(self):
        self._input_status = False
        self._mes_sent = False
        self.sm = self.StateMachine.idle

    @property
    def pack_state(self) -> str:
        if self.sm == self.StateMachine.cleared_sure:
            return "cleared"
        return self.sm.name

class SchedulerResult(Enum):
    KILL = "KILL"
    PROCESS = "PROCESS"

class SessionContext:
    def __init__(self):
        # 消息包
        self.message_queue: list[str] = []
        # 备战未来
        self.active_future: asyncio.Future | None = None
        self.timer_task: asyncio.Task | None = None
        # 会赢吗
        self.is_processing: bool = False
        self.lock_timestamp: float = -1