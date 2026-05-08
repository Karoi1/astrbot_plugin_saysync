import asyncio
from enum import Enum
import uuid
import time

from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.message_type import MessageType
from astrbot.api import logger



def _log(enable: bool, level: str, msg: str, *args, **kwargs):
    """内部日志方法，受 enable_logger 控制"""
    if not enable:
        return
    log_fn = getattr(logger, level, None)
    if log_fn and callable(log_fn):
        log_fn(msg, *args, **kwargs)

class ProactiveType(Enum):
    FAREWELL = "FAREWELL"     # 结束后的补充
    CLEARED = "CLEARED"       # 欲言又止的试探

class ProactiveEventResult(Enum):
    KILL = "KILL"
    PROCESS = "PROCESS"

class SchedulerResult(Enum):
    KILL = "KILL"
    PROCESS = "PROCESS"

class ProactiveTask:
    """主动说话任务单"""
    def __init__(self, chat_id: str, task_type: ProactiveType, instruction: str, delay: float = 20.0):
        self.chat_id = chat_id
        self.task_type = task_type
        self.instruction = instruction  # 纯粹的指令 prompt
        self.delay = delay
        self.created_at = time.time()

class UserStatus:
    # 从用户输入状态，判断用户想要什么定制化服务
    class StateMachine(Enum):
        idle = 0               # 没打字/发送完消息后停止
        typing = 1             # 正在打字
        cleared = 2            # 打了字又删了，欲言又止
        cleared_sure = 3       # 同上，过滤用

    def __init__(self):
        self._mes_sent: bool = False
        self.sm = self.StateMachine.idle

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
            if _input_status:
                self.sm = self.StateMachine.typing
            else:
                self.sm = self.StateMachine.cleared

        # 欲言又止 -> 真·欲言又止（过滤）
        elif self.sm == self.StateMachine.cleared:
            if _input_status:
                self.sm = self.StateMachine.cleared_sure   # 第一次收到输入，进入过渡态
            # else stay cleared

        # 真·欲言又止 -> 正在打字
        elif self.sm == self.StateMachine.cleared_sure:
            if _input_status:
                self.sm = self.StateMachine.typing          # 再次收到输入，确认进入 typing
            else:
                self.sm = self.StateMachine.cleared         # 输入消失，退回 cleared

        else:
            # 容错：回归初始
            self.reset()

    def reset(self):
        self._mes_sent = False
        self.sm = self.StateMachine.idle

    @property
    def pack_state(self) -> str:
        if self.sm == self.StateMachine.cleared_sure:
            return "cleared"
        return self.sm.name

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


class SessionSkin:
    """
    会话壳：保存真实 Event 的静态特征，用于后续伪造主动说话的 Event。
    它是纯数据的克隆体，不包含任何控制流逻辑。
    """
    def __init__(self):
        self.platform_meta: 'PlatformMetadata | None' = None
        self.msg_type: 'MessageType | None' = None
        self.self_id: str = ""
        self.session_id: str = ""
        self.group_id: str = ""
        self.sender: 'MessageMember | None' = None
        self.unified_msg_origin: str = ""
        # bot实例，目前能用cqhttp
        self.bot = None

    def is_ready(self) -> bool:
        """检查壳是否已经收集完整，可以用来伪造 Event"""
        return self.platform_meta is not None and self.unified_msg_origin != ""

    def clone_message_obj(self, prompt: str = "") -> 'AstrBotMessage':
        """
        根据保存的壳，伪造一个全新的 AstrBotMessage 对象。
        动态特征（如 message_id, raw_message）会自动生成假数据。
        """
        # 动态导入，避免在非运行时环境报错
        from astrbot.core.platform.astrbot_message import AstrBotMessage
        from astrbot.core.message.components import Plain

        msg_obj = AstrBotMessage()
        msg_obj.type = self.msg_type
        msg_obj.self_id = self.self_id
        msg_obj.session_id = self.session_id
        msg_obj.group_id = self.group_id
        msg_obj.sender = self.sender
        
        # 填充我们要主动发送的内容
        msg_obj.message = [Plain(prompt)] if prompt else []
        msg_obj.message_str = prompt
        
        # 动态特征：伪造防御性数据，防止底层报错
        msg_obj.raw_message = {} 
        msg_obj.message_id = str(uuid.uuid4()) # 必须生成全新的 ID
        
        return msg_obj