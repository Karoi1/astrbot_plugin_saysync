from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import Dict, Type, Optional, Any

# 避免循环导入
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .core.scheduler import SessionScheduler
    from .core.proactive_manager import ProactiveManager

@dataclass
class SignalContext:
    """信号处理上下文。
    
    将 Scheduler 和 ProactiveManager 打包，方便信号处理器访问。
    
    Attributes:
        scheduler: 消息攒批调度器实例。
        proactive_mgr: 主动任务管理器实例。
    """
    scheduler: SessionScheduler
    proactive_mgr: ProactiveManager


# ==========================================
# 信号模型层
# ==========================================

class EnvSignal:
    """环境信号基类。
    
    所有平台推送信号（如输入状态、戳一戳、撤回等）的抽象基类。
    
    Attributes:
        chat_id: 会话唯一标识。
    """
    def __init__(self, chat_id: str):
        self.chat_id = chat_id


class TypingSignal(EnvSignal):
    """输入状态信号（对应 aiocqhttp input_status 推送）。
    
    Attributes:
        is_typing: 是否正在输入。
        user_id: 用户 ID。
    """
    def __init__(self, chat_id: str, is_typing: bool, user_id: str = ""):
        super().__init__(chat_id)
        self.is_typing = is_typing
        self.user_id = user_id


class PokeSignal(EnvSignal):
    """戳一戳 / 窗口抖动信号（对应 aiocqhttp poke 推送）。
    
    Attributes:
        user_id: 发送戳一戳的用户 ID。
        target_id: 被戳的用户 ID。
        group_id: 群 ID（私聊时为空）。
    """
    def __init__(self, chat_id: str, user_id: str, target_id: str = "", group_id: str = ""):
        super().__init__(chat_id)
        self.user_id = user_id
        self.target_id = target_id
        self.group_id = group_id


class RecallSignal(EnvSignal):
    """消息撤回信号（对应 friend_recall / group_recall）。
    
    Attributes:
        msg_id: 被撤回的消息 ID。
        user_id: 消息发送者 ID。
        operator_id: 执行撤回操作者 ID。
        group_id: 群 ID（私聊时为空）。
    """
    def __init__(self, chat_id: str, msg_id: str, user_id: str = "", operator_id: str = "", group_id: str = ""):
        super().__init__(chat_id)
        self.msg_id = msg_id
        self.user_id = user_id
        self.operator_id = operator_id
        self.group_id = group_id


class GroupBanSignal(EnvSignal):
    """群禁言信号（对应 group_ban）。
    
    Attributes:
        group_id: 群 ID。
        user_id: 被禁言用户 ID。
        duration: 禁言时长（秒）。
        operator_id: 操作者 ID。
        sub_type: 子类型（"ban" 或 "lift_ban"）。
    """
    def __init__(self, chat_id: str, group_id: str, user_id: str, duration: int, operator_id: str = "", sub_type: str = ""):
        super().__init__(chat_id)
        self.group_id = group_id
        self.user_id = user_id
        self.duration = duration
        self.operator_id = operator_id
        self.sub_type = sub_type  # "ban" or "lift_ban"


class GroupAdminSignal(EnvSignal):
    """群管理员变动信号（对应 group_admin）。
    
    Attributes:
        group_id: 群 ID。
        user_id: 被变动的用户 ID。
        is_set: 是否被设为管理员（True=设置，False=取消）。
    """
    def __init__(self, chat_id: str, group_id: str, user_id: str, is_set: bool):
        super().__init__(chat_id)
        self.group_id = group_id
        self.user_id = user_id
        self.is_set = is_set


class GroupMemberChangeSignal(EnvSignal):
    """群成员变动信号（对应 group_increase / group_decrease）。
    
    Attributes:
        group_id: 群 ID。
        user_id: 变动的用户 ID。
        change_type: 变动类型（"increase" 或 "decrease"）。
        sub_type: 子类型（"approve", "invite", "leave", "kick", "kick_me"）。
        operator_id: 操作者 ID。
    """
    def __init__(self, chat_id: str, group_id: str, user_id: str, change_type: str, sub_type: str = "", operator_id: str = ""):
        super().__init__(chat_id)
        self.group_id = group_id
        self.user_id = user_id
        self.change_type = change_type  # "increase" or "decrease"
        self.sub_type = sub_type        # "approve", "invite", "leave", "kick", "kick_me"
        self.operator_id = operator_id


class FriendAddSignal(EnvSignal):
    """好友添加信号（对应 friend_add）。
    
    Attributes:
        user_id: 新添加的好友 ID。
    """
    def __init__(self, chat_id: str, user_id: str):
        super().__init__(chat_id)
        self.user_id = user_id


class EssenceSignal(EnvSignal):
    """精华消息信号（对应 essence）。
    
    Attributes:
        group_id: 群 ID。
        msg_id: 被设精华的消息 ID。
        sender_id: 消息发送者 ID。
        operator_id: 操作者 ID。
    """
    def __init__(self, chat_id: str, group_id: str, msg_id: str, sender_id: str = "", operator_id: str = ""):
        super().__init__(chat_id)
        self.group_id = group_id
        self.msg_id = msg_id
        self.sender_id = sender_id
        self.operator_id = operator_id


class OfflineFileSignal(EnvSignal):
    """离线文件信号（对应 offline_file）。
    
    Attributes:
        user_id: 发送文件的用户 ID。
        file_name: 文件名。
        file_size: 文件大小（字节）。
    """
    def __init__(self, chat_id: str, user_id: str, file_name: str = "", file_size: int = 0):
        super().__init__(chat_id)
        self.user_id = user_id
        self.file_name = file_name
        self.file_size = file_size


class ClientStatusSignal(EnvSignal):
    """客户端状态信号（对应 client_status）。
    
    Attributes:
        online: 是否在线。
        client: 客户端信息（平台相关）。
    """
    def __init__(self, chat_id: str, online: bool, client: Any = None):
        super().__init__(chat_id)
        self.online = online
        self.client = client


class LuckyKingSignal(EnvSignal):
    """群红包运气王信号（对应 lucky_king）。
    
    Attributes:
        group_id: 群 ID。
        user_id: 运气王用户 ID。
        target_id: 红包发送者 ID。
    """
    def __init__(self, chat_id: str, group_id: str, user_id: str, target_id: str = ""):
        super().__init__(chat_id)
        self.group_id = group_id
        self.user_id = user_id
        self.target_id = target_id


class HonorSignal(EnvSignal):
    """群荣誉变更信号（对应 honor）。
    
    Attributes:
        group_id: 群 ID。
        user_id: 获得荣誉的用户 ID。
        honor_type: 荣誉类型。
    """
    def __init__(self, chat_id: str, group_id: str, user_id: str, honor_type: str = ""):
        super().__init__(chat_id)
        self.group_id = group_id
        self.user_id = user_id
        self.honor_type = honor_type


# ==========================================
# 处理器抽象层
# ==========================================

class SignalHandler(ABC):
    """信号处理抽象基类。
    
    所有具体信号处理器（如 TypingHandler、PokeHandler）必须继承此类，
    并实现 handle 方法以决定如何影响 Scheduler 或 ProactiveManager。
    """
    @abstractmethod
    async def handle(self, signal: EnvSignal, scheduler: 'SessionScheduler', proactive_mgr: 'ProactiveManager'):
        """处理信号，并决定如何影响 Scheduler 或 ProactiveMgr。
        
        Args:
            signal: 类型化后的环境信号。
            scheduler: 消息攒批调度器实例。
            proactive_mgr: 主动任务管理器实例。
        """
        pass


class TypingHandler(SignalHandler):
    """输入状态处理器。
    
    对应 main.py 中 _from_aiocqhttp_update 对 input_status 的处理逻辑。
    此段代码基于 AI 助手自身理解生成，仅供参考。
    """
    async def handle(self, signal: TypingSignal, scheduler, proactive_mgr):
        if hasattr(scheduler, 'update_input_state'):
            scheduler.update_input_state(signal.chat_id, signal.is_typing)

        # 如果正在打字，尝试重置定时器（需 scheduler 提供公开接口）
        if signal.is_typing and hasattr(scheduler, 'reset_timer'):
            scheduler.reset_timer(signal.chat_id)


class PokeHandler(SignalHandler):
    """戳一戳处理器。
    
    此段代码基于 AI 助手自身理解生成，仅供参考。
    """
    async def handle(self, signal: PokeSignal, scheduler, proactive_mgr):
        # 戳一戳的逻辑：可能要打断当前的等待，立刻强制放行
        if hasattr(scheduler, 'force_release'):
            scheduler.force_release(signal.chat_id, env_state="poked")


class RecallHandler(SignalHandler):
    """撤回消息处理器。"""
    async def handle(self, signal: RecallSignal, scheduler, proactive_mgr):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        # 撤回消息可能影响对话历史，但当前业务逻辑不确定，暂不处理
        pass


class GroupBanHandler(SignalHandler):
    """群禁言处理器。"""
    async def handle(self, signal: GroupBanSignal, scheduler, proactive_mgr):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        # 群聊场景尚未深度适配，当前业务逻辑不确定
        pass


class GroupAdminHandler(SignalHandler):
    """群管理员变动处理器。"""
    async def handle(self, signal: GroupAdminSignal, scheduler, proactive_mgr):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class GroupMemberChangeHandler(SignalHandler):
    """群成员变动处理器。"""
    async def handle(self, signal: GroupMemberChangeSignal, scheduler, proactive_mgr):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class FriendAddHandler(SignalHandler):
    """好友添加处理器。"""
    async def handle(self, signal: FriendAddSignal, scheduler, proactive_mgr):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class EssenceHandler(SignalHandler):
    """精华消息处理器。"""
    async def handle(self, signal: EssenceSignal, scheduler, proactive_mgr):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class OfflineFileHandler(SignalHandler):
    """离线文件处理器。"""
    async def handle(self, signal: OfflineFileSignal, scheduler, proactive_mgr):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class ClientStatusHandler(SignalHandler):
    """客户端状态处理器。"""
    async def handle(self, signal: ClientStatusSignal, scheduler, proactive_mgr):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class LuckyKingHandler(SignalHandler):
    """群红包运气王处理器。"""
    async def handle(self, signal: LuckyKingSignal, scheduler, proactive_mgr):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class HonorHandler(SignalHandler):
    """群荣誉变更处理器。"""
    async def handle(self, signal: HonorSignal, scheduler, proactive_mgr):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


# ==========================================
# 信号路由器
# ==========================================

class SignalRouter:
    """信号路由器：将 aiocqhttp 原始消息解析为对应信号并分发到注册的 Handler。
    
    使用 _parse 方法将原始 dict 转为类型化 EnvSignal，
    再通过 dispatch 方法找到对应的 SignalHandler 并调用其 handle。
    """

    def __init__(self):
        """初始化空的路由表。"""
        self._handlers: Dict[Type[EnvSignal], SignalHandler] = {}

    def register(self, signal_type: Type[EnvSignal], handler: SignalHandler):
        """注册信号处理器。
        
        Args:
            signal_type: 信号类型（EnvSignal 的子类）。
            handler: 处理该信号的具体处理器实例。
        """
        self._handlers[signal_type] = handler

    def _parse(self, raw: dict, chat_id: str) -> Optional[EnvSignal]:
        """将 aiocqhttp 原始消息解析为类型化的 EnvSignal。
        
        Args:
            raw: aiocqhttp 推送的原始字典。
            chat_id: 会话唯一标识。
            
        Returns:
            解析成功返回对应的 EnvSignal 子类实例；无法识别返回 None。
        """
        post_type = raw.get("post_type")

        if post_type == "notice":
            notice_type = raw.get("notice_type")
            sub_type = raw.get("sub_type", "")

            # 输入状态 (input_status)
            if (notice_type == "notify" and
                sub_type == "input_status" and
                "status_text" in raw):
                is_typing = bool(raw.get("status_text"))
                return TypingSignal(
                    chat_id=chat_id,
                    is_typing=is_typing,
                    user_id=str(raw.get("user_id", ""))
                )

            # 戳一戳 (poke)
            if notice_type == "notify" and sub_type == "poke":
                return PokeSignal(
                    chat_id=chat_id,
                    user_id=str(raw.get("user_id", "")),
                    target_id=str(raw.get("target_id", "")),
                    group_id=str(raw.get("group_id", ""))
                )

            # 运气王 (lucky_king)
            if notice_type == "notify" and sub_type == "lucky_king":
                return LuckyKingSignal(
                    chat_id=chat_id,
                    group_id=str(raw.get("group_id", "")),
                    user_id=str(raw.get("user_id", "")),
                    target_id=str(raw.get("target_id", ""))
                )

            # 荣誉 (honor)
            if notice_type == "notify" and sub_type == "honor":
                return HonorSignal(
                    chat_id=chat_id,
                    group_id=str(raw.get("group_id", "")),
                    user_id=str(raw.get("user_id", "")),
                    honor_type=str(raw.get("honor_type", ""))
                )

            # 消息撤回 (friend_recall / group_recall)
            if notice_type in ("friend_recall", "group_recall"):
                return RecallSignal(
                    chat_id=chat_id,
                    msg_id=str(raw.get("message_id", "")),
                    user_id=str(raw.get("user_id", "")),
                    operator_id=str(raw.get("operator_id", "")),
                    group_id=str(raw.get("group_id", ""))
                )

            # 群禁言 (group_ban)
            if notice_type == "group_ban":
                return GroupBanSignal(
                    chat_id=chat_id,
                    group_id=str(raw.get("group_id", "")),
                    user_id=str(raw.get("user_id", "")),
                    duration=int(raw.get("duration", 0)),
                    operator_id=str(raw.get("operator_id", "")),
                    sub_type=sub_type
                )

            # 群管理员变动 (group_admin)
            if notice_type == "group_admin":
                return GroupAdminSignal(
                    chat_id=chat_id,
                    group_id=str(raw.get("group_id", "")),
                    user_id=str(raw.get("user_id", "")),
                    is_set=(sub_type == "set")
                )

            # 群成员增加 (group_increase)
            if notice_type == "group_increase":
                return GroupMemberChangeSignal(
                    chat_id=chat_id,
                    group_id=str(raw.get("group_id", "")),
                    user_id=str(raw.get("user_id", "")),
                    change_type="increase",
                    sub_type=sub_type,
                    operator_id=str(raw.get("operator_id", ""))
                )

            # 群成员减少 (group_decrease)
            if notice_type == "group_decrease":
                return GroupMemberChangeSignal(
                    chat_id=chat_id,
                    group_id=str(raw.get("group_id", "")),
                    user_id=str(raw.get("user_id", "")),
                    change_type="decrease",
                    sub_type=sub_type,
                    operator_id=str(raw.get("operator_id", ""))
                )

            # 好友添加 (friend_add)
            if notice_type == "friend_add":
                return FriendAddSignal(
                    chat_id=chat_id,
                    user_id=str(raw.get("user_id", ""))
                )

            # 精华消息 (essence)
            if notice_type == "essence":
                return EssenceSignal(
                    chat_id=chat_id,
                    group_id=str(raw.get("group_id", "")),
                    msg_id=str(raw.get("message_id", "")),
                    sender_id=str(raw.get("sender_id", "")),
                    operator_id=str(raw.get("operator_id", ""))
                )

            # 离线文件 (offline_file)
            if notice_type == "offline_file":
                file_info = raw.get("file", {}) or {}
                return OfflineFileSignal(
                    chat_id=chat_id,
                    user_id=str(raw.get("user_id", "")),
                    file_name=str(file_info.get("name", "")),
                    file_size=int(file_info.get("size", 0))
                )

            # 客户端状态 (client_status)
            if notice_type == "client_status":
                return ClientStatusSignal(
                    chat_id=chat_id,
                    online=bool(raw.get("online", False)),
                    client=raw.get("client")
                )

        return None

    async def dispatch(self, raw: dict, chat_id: str, scheduler: 'SessionScheduler', proactive_mgr: 'ProactiveManager'):
        """解析并分发信号。
        
        先调用 _parse 将原始消息转为 EnvSignal，再查找对应的 Handler 执行处理。
        
        Args:
            raw: aiocqhttp 推送的原始字典。
            chat_id: 会话唯一标识。
            scheduler: 消息攒批调度器实例。
            proactive_mgr: 主动任务管理器实例。
        """
        signal = self._parse(raw, chat_id)
        if signal is None:
            return

        handler = self._handlers.get(type(signal))
        if handler is None:
            return

        await handler.handle(signal, scheduler, proactive_mgr)


# ==========================================
# 预置默认路由器
# ==========================================

_default_router: Optional[SignalRouter] = None


def get_default_router() -> SignalRouter:
    """获取预置了所有默认 Handler 的信号路由器（单例）。
    
    Returns:
        已注册全部默认处理器的 SignalRouter 实例。
    """
    global _default_router

    if _default_router is None:
        _default_router = SignalRouter()
        _default_router.register(TypingSignal, TypingHandler())
        _default_router.register(PokeSignal, PokeHandler())
        _default_router.register(RecallSignal, RecallHandler())
        _default_router.register(GroupBanSignal, GroupBanHandler())
        _default_router.register(GroupAdminSignal, GroupAdminHandler())
        _default_router.register(GroupMemberChangeSignal, GroupMemberChangeHandler())
        _default_router.register(FriendAddSignal, FriendAddHandler())
        _default_router.register(EssenceSignal, EssenceHandler())
        _default_router.register(OfflineFileSignal, OfflineFileHandler())
        _default_router.register(ClientStatusSignal, ClientStatusHandler())
        _default_router.register(LuckyKingSignal, LuckyKingHandler())
        _default_router.register(HonorSignal, HonorHandler())

    return _default_router
