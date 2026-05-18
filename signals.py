from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import Dict, Type, Optional, Any
from pathlib import Path
from datetime import datetime
import json
import os

from astrbot.api.star import Star

from .core import SessionScheduler
from .core import ProactiveManager

from .core.models import _log

# ====== 日志开关：True 打印所有日志，False 静默 ======
enable_log = True

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

    @classmethod
    def from_components(cls, plugin: Star) -> "SignalContext":
        """工厂方法：从组件实例创建 SignalContext。

        Args:
            plugin: 要包装成 SignalContext 的插件Star实例。
        
        Returns:
            SignalContext 实例。
        """
        scheduler = None
        proactive_mgr = None

        if hasattr(plugin, 'scheduler'):
            scheduler = plugin.scheduler
        if hasattr(plugin, 'proactive_mgr'):
            proactive_mgr = plugin.proactive_mgr

        return cls(scheduler=scheduler, proactive_mgr=proactive_mgr)


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
        sender_id: 发起戳一戳的用户 ID。
        user_id: 被戳的用户 ID。
        target_id: 戳一戳目标 ID。
        group_id: 群 ID（私聊时为空）。
    """
    def __init__(self, chat_id: str, sender_id: str, user_id: str = "", target_id: str = "", group_id: str = ""):
        super().__init__(chat_id)
        self.sender_id = sender_id
        self.user_id = user_id
        self.target_id = target_id
        self.group_id = group_id


class RecallSignal(EnvSignal):
    """消息撤回信号（对应 friend_recall / group_recall）。
    
    Attributes:
        recall_type: 撤回类型（"friend_recall" 或 "group_recall"）。
        msg_id: 被撤回的消息 ID。
        user_id: 消息发送者 ID。
        operator_id: 执行撤回操作者 ID。
        group_id: 群 ID（私聊时为空）。
    """
    def __init__(self, chat_id: str, recall_type: str, msg_id: str, user_id: str = "", operator_id: str = "", group_id: str = ""):
        super().__init__(chat_id)
        self.recall_type = recall_type
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
        file_url: 文件下载地址。
    """
    def __init__(self, chat_id: str, user_id: str, file_name: str = "", file_size: int = 0, file_url: str = ""):
        super().__init__(chat_id)
        self.user_id = user_id
        self.file_name = file_name
        self.file_size = file_size
        self.file_url = file_url


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


class EmojiLikeSignal(EnvSignal):
    """群消息表情点赞信号（对应 group_msg_emoji_like 推送，NapCatQQ 扩展）。

    当群中有人给消息添加或取消表情点赞时推送。

    Attributes:
        group_id: 群 ID。
        user_id: 点赞用户 QQ。
        message_id: 被点赞的消息 ID。
        likes: 表情点赞列表 [{"emoji_id": "66", "count": 1}]。
        is_add: True=添加点赞，False=取消点赞。
    """
    def __init__(self, chat_id: str, group_id: str, user_id: str, message_id: str,
                 likes: list = None, is_add: bool = True):
        super().__init__(chat_id)
        self.group_id = group_id
        self.user_id = user_id
        self.message_id = message_id
        self.likes = likes or []
        self.is_add = is_add


# ==========================================
# 处理器抽象层
# ==========================================

class SignalHandler(ABC):
    """信号处理抽象基类。
    
    所有具体信号处理器（如 TypingHandler、PokeHandler）必须继承此类，
    并实现 handle 方法以决定如何影响 Scheduler 或 ProactiveManager。
    """
    @abstractmethod
    async def handle(self, signal: EnvSignal, ctx: SignalContext):
        """处理信号，并决定如何影响 Scheduler 或 ProactiveMgr。
        
        Args:
            signal: 类型化后的环境信号。
            ctx: 信号处理上下文（含 scheduler 和 proactive_mgr）。
        """
        pass


class TypingHandler(SignalHandler):
    """输入状态处理器。
    
    对应 main.py 中 _from_aiocqhttp_update 对 input_status 的处理逻辑。
    此段代码基于 AI 助手自身理解生成，仅供参考。
    """
    async def handle(self, signal: TypingSignal, ctx: SignalContext):
        if hasattr(ctx.scheduler, 'update_input_state'):
            ctx.scheduler.update_input_state(signal.chat_id, signal.is_typing)

        # 如果正在打字，尝试重置定时器（需 scheduler 提供公开接口）
        if signal.is_typing and hasattr(ctx.scheduler, 'reset_timer'):
            ctx.scheduler.reset_timer(signal.chat_id)


class PokeHandler(SignalHandler):
    """戳一戳处理器。
    
    此段代码基于 AI 助手自身理解生成，仅供参考。
    """
    async def handle(self, signal: PokeSignal, ctx: SignalContext):
        # 戳一戳的逻辑：可能要打断当前的等待，立刻强制放行
        if hasattr(ctx.scheduler, 'force_release'):
            ctx.scheduler.force_release(signal.chat_id, env_state="poked")


class RecallHandler(SignalHandler):
    """撤回消息处理器。"""
    async def handle(self, signal: RecallSignal, ctx: SignalContext):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        # 撤回消息可能影响对话历史，但当前业务逻辑不确定，暂不处理
        pass


class GroupBanHandler(SignalHandler):
    """群禁言处理器。"""
    async def handle(self, signal: GroupBanSignal, ctx: SignalContext):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        # 群聊场景尚未深度适配，当前业务逻辑不确定
        pass


class GroupAdminHandler(SignalHandler):
    """群管理员变动处理器。"""
    async def handle(self, signal: GroupAdminSignal, ctx: SignalContext):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class GroupMemberChangeHandler(SignalHandler):
    """群成员变动处理器。"""
    async def handle(self, signal: GroupMemberChangeSignal, ctx: SignalContext):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class FriendAddHandler(SignalHandler):
    """好友添加处理器。"""
    async def handle(self, signal: FriendAddSignal, ctx: SignalContext):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class EssenceHandler(SignalHandler):
    """精华消息处理器。"""
    async def handle(self, signal: EssenceSignal, ctx: SignalContext):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class OfflineFileHandler(SignalHandler):
    """离线文件处理器。"""
    async def handle(self, signal: OfflineFileSignal, ctx: SignalContext):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class ClientStatusHandler(SignalHandler):
    """客户端状态处理器。"""
    async def handle(self, signal: ClientStatusSignal, ctx: SignalContext):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class LuckyKingHandler(SignalHandler):
    """群红包运气王处理器。"""
    async def handle(self, signal: LuckyKingSignal, ctx: SignalContext):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class HonorHandler(SignalHandler):
    """群荣誉变更处理器。"""
    async def handle(self, signal: HonorSignal, ctx: SignalContext):
        # 此段代码基于 AI 助手自身理解生成，仅供参考
        pass


class EmojiLikeHandler(SignalHandler):
    """群消息表情点赞处理器。"""
    async def handle(self, signal: EmojiLikeSignal, ctx: SignalContext):
        # 初始暂不干预攒批/推流，保留为扩展点
        pass


# ==========================================
# 信号路由器
# ==========================================

class SignalRouter:
    """信号路由器：将 aiocqhttp 原始消息解析为对应信号并分发到注册的 Handler。
    
    使用 _parse 方法将原始 dict 转为类型化 EnvSignal，
    再通过 dispatch 方法找到对应的 SignalHandler 并调用其 handle。
    """

    def __init__(self, data_dir: str = ""):
        """初始化空的路由表。

        Args:
            data_dir: 插件数据持久化目录，用于记录未知信号。
        """
        self._handlers: Dict[Type[EnvSignal], SignalHandler] = {}
        self._data_dir = data_dir

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

            #_log(enable_log, "info", f"[SignalRouter] 解析 notice: notice_type={notice_type} sub_type={sub_type} raw={raw}")
            # 输入状态 (input_status)
            if (notice_type == "notify" and
                sub_type == "input_status" and
                "status_text" in raw):
                is_typing = bool(raw.get("status_text"))
                _log(enable_log, "info", f"[SignalRouter] 解析到 TypingSignal: is_typing={is_typing} user_id={raw.get('user_id', '')}")
                return TypingSignal(
                    chat_id=chat_id,
                    is_typing=is_typing,
                    user_id=str(raw.get("user_id", ""))
                )
                
            # 戳一戳 (poke)
            if notice_type == "notify" and sub_type == "poke":
                _log(enable_log, "info", f"[SignalRouter] 解析到 PokeSignal: sender_id={raw.get('sender_id', '')} user_id={raw.get('user_id', '')} target_id={raw.get('target_id', '')} group_id={raw.get('group_id', '')}")
                return PokeSignal(
                    chat_id=chat_id,
                    sender_id=str(raw.get("sender_id", "")),
                    user_id=str(raw.get("user_id", "")),
                    target_id=str(raw.get("target_id", "")),
                    group_id=str(raw.get("group_id", ""))
                )

            # 运气王 (lucky_king)
            if notice_type == "notify" and sub_type == "lucky_king":
                _log(enable_log, "info", f"[SignalRouter] 解析到 LuckyKingSignal: group_id={raw.get('group_id', '')} user_id={raw.get('user_id', '')} target_id={raw.get('target_id', '')}")
                return LuckyKingSignal(
                    chat_id=chat_id,
                    group_id=str(raw.get("group_id", "")),
                    user_id=str(raw.get("user_id", "")),
                    target_id=str(raw.get("target_id", ""))
                )

            # 荣誉 (honor)
            if notice_type == "notify" and sub_type == "honor":
                _log(enable_log, "info", f"[SignalRouter] 解析到 HonorSignal: group_id={raw.get('group_id', '')} user_id={raw.get('user_id', '')} honor_type={raw.get('honor_type', '')}")
                return HonorSignal(
                    chat_id=chat_id,
                    group_id=str(raw.get("group_id", "")),
                    user_id=str(raw.get("user_id", "")),
                    honor_type=str(raw.get("honor_type", ""))
                )

            # 消息撤回 (friend_recall / group_recall)
            if notice_type in ("friend_recall", "group_recall"):
                _log(enable_log, "info", f"[SignalRouter] 解析到 RecallSignal: group_id={raw.get('group_id', '')} user_id={raw.get('user_id', '')} msg_id={raw.get('message_id', '')}")
                return RecallSignal(
                    chat_id=chat_id,
                    recall_type=notice_type,
                    msg_id=str(raw.get("message_id", "")),
                    user_id=str(raw.get("user_id", "")),
                    operator_id=str(raw.get("operator_id", "")),
                    group_id=str(raw.get("group_id", ""))
                )

            # 群禁言 (group_ban)
            if notice_type == "group_ban":
                _log(enable_log, "info", f"[SignalRouter] 解析到 GroupBanSignal: group_id={raw.get('group_id', '')} user_id={raw.get('user_id', '')} duration={raw.get('duration', 0)}")
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
                _log(enable_log, "info", f"[SignalRouter] 解析到 GroupAdminSignal: group_id={raw.get('group_id', '')} user_id={raw.get('user_id', '')} is_set={sub_type == 'set'}")
                return GroupAdminSignal(
                    chat_id=chat_id,
                    group_id=str(raw.get("group_id", "")),
                    user_id=str(raw.get("user_id", "")),
                    is_set=(sub_type == "set")
                )

            # 群成员增加 (group_increase)
            if notice_type == "group_increase":
                _log(enable_log, "info", f"[SignalRouter] 解析到 GroupMemberChangeSignal: group_id={raw.get('group_id', '')} user_id={raw.get('user_id', '')} change_type=increase")
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
                _log(enable_log, "info", f"[SignalRouter] 解析到 GroupMemberChangeSignal: group_id={raw.get('group_id', '')} user_id={raw.get('user_id', '')} change_type=decrease")
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
                _log(enable_log, "info", f"[SignalRouter] 解析到 FriendAddSignal: user_id={raw.get('user_id', '')}")
                return FriendAddSignal(
                    chat_id=chat_id,
                    user_id=str(raw.get("user_id", ""))
                )

            # 精华消息 (essence)
            if notice_type == "essence":
                _log(enable_log, "info", f"[SignalRouter] 解析到 EssenceSignal: group_id={raw.get('group_id', '')} msg_id={raw.get('message_id', '')}")
                return EssenceSignal(
                    chat_id=chat_id,
                    group_id=str(raw.get("group_id", "")),
                    msg_id=str(raw.get("message_id", "")),
                    sender_id=str(raw.get("sender_id", "")),
                    operator_id=str(raw.get("operator_id", ""))
                )

            # 离线文件 (offline_file)
            if notice_type == "offline_file":
                _log(enable_log, "info", f"[SignalRouter] 解析到 OfflineFileSignal: user_id={raw.get('user_id', '')}")
                file_info = raw.get("file", {}) or {}
                return OfflineFileSignal(
                    chat_id=chat_id,
                    user_id=str(raw.get("user_id", "")),
                    file_name=str(file_info.get("name", "")),
                    file_size=int(file_info.get("size", 0)),
                    file_url=str(file_info.get("url", ""))
                )

            # 客户端状态 (client_status)
            if notice_type == "client_status":
                _log(enable_log, "info", f"[SignalRouter] 解析到 ClientStatusSignal: user_id={raw.get('user_id', '')} online={raw.get('online', False)} client={raw.get('client')}")
                return ClientStatusSignal(
                    chat_id=chat_id,
                    online=bool(raw.get("online", False)),
                    client=raw.get("client")
                )

            # 群消息表情点赞 (group_msg_emoji_like，NapCatQQ 扩展)
            if notice_type == "group_msg_emoji_like":
                _log(enable_log, "info", f"[SignalRouter] 解析到 EmojiLikeSignal: group_id={raw.get('group_id', '')} user_id={raw.get('user_id', '')} message_id={raw.get('message_id', '')}, is_add={raw.get('is_add', True)} likes={raw.get('likes', [])}")
                return EmojiLikeSignal(
                    chat_id=chat_id,
                    group_id=str(raw.get("group_id", "")),
                    user_id=str(raw.get("user_id", "")),
                    message_id=str(raw.get("message_id", "")),
                    likes=raw.get("likes", []),
                    is_add=bool(raw.get("is_add", True))
                )

        return None

    async def dispatch(self, raw: dict, chat_id: str, ctx: SignalContext) -> bool:
        """解析并分发信号。
        
        先调用 _parse 将原始消息转为 EnvSignal，再查找对应的 Handler 执行处理。
        
        Args:
            raw: aiocqhttp 推送的原始字典。
            chat_id: 会话唯一标识。
            ctx: 信号处理上下文（含 scheduler 和 proactive_mgr）。
            
        Returns:
            True 表示信号被成功识别并分发；False 表示无法识别或无对应 Handler。
        """
        # _log(enable_log, "info", f"[SignalRouter] 开始 dispatch，raw={raw} chat_id={chat_id}")
        signal = self._parse(raw, chat_id)
        if signal is None:
            await self._record_unknown(raw, chat_id)
            return False

        handler = self._handlers.get(type(signal))
        if handler is None:
            return False

        await handler.handle(signal, ctx)
        return True

    async def _record_unknown(self, raw: dict, chat_id: str):
        """记录无法识别的原始推送，供后续扩展信号类型。

        以 raw 中的 notice_type 为去重键，同一类型只记录第一次出现。
        数据写入 <data_dir>/unknown_signals.json。

        Args:
            raw: aiocqhttp 推送的原始字典。
            chat_id: 会话唯一标识。
        """
        if not self._data_dir:
            return

        key = raw.get("notice_type") or raw.get("post_type", "unknown")
        filepath = os.path.join(self._data_dir, "unknown_signals.json")

        existing = {}
        if filepath.exists():
            try:
                existing = json.loads(filepath.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

        if key in existing:
            return

        existing[key] = {
            "first_seen": datetime.now().isoformat(),
            "chat_id": chat_id,
            "raw": raw,
        }

        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            _log(enable_log, "info", f"[SignalRouter] 已记录未知信号: {key}")
        except OSError as e:
            _log(enable_log, "error", f"[SignalRouter] 写入未知信号文件失败: {e}")


# ==========================================
# 预置默认路由器
# ==========================================

_default_router: Optional[SignalRouter] = None


def get_default_router(data_dir: str = "") -> SignalRouter:
    """获取预置了所有默认 Handler 的信号路由器（单例）。

    Args:
        data_dir: 插件数据持久化目录，用于记录未知信号。

    Returns:
        已注册全部默认处理器的 SignalRouter 实例。
    """
    global _default_router

    if _default_router is None:
        _default_router = SignalRouter(data_dir=data_dir)
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
        _default_router.register(EmojiLikeSignal, EmojiLikeHandler())

    return _default_router
