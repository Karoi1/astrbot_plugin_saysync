from typing import Dict

from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import Platform, MessageType, Group, MessageMember
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from .models import SessionSkin
from .models import _log

# ====== 日志开关：True 打印所有日志，False 静默 ======
enable_log = True


class EventForger:
    """
    事件伪造器：负责管理会话特征壳（SessionSkin），并据此凭空捏造可投入 AstrBot 流水线的 Event。
    
    主要用于 Bot 主动开口的场景——当没有真实用户消息触发时，用缓存的 Skin 伪造一个 Event，
    使其能正常走完 AstrBot 的 Handler、LLM、发送流程。
    """
    def __init__(self):
        # 为每个 umo (unified_msg_origin, 即 chat_id) 保留一个 session skin 外壳
        self._skins: Dict[str, SessionSkin] = {}

    # ========================
    # 建档区：管理 SessionSkin
    # ========================

    def update_skin(self, chat_id: str, event: 'AstrMessageEvent') -> None:
        """
        从真实的 Event 中拓印特征，更新或创建会话壳。
        由被动接收消息的 Handler 调用。
        
        Args:
            chat_id: 会话唯一标识（unified_msg_origin）。
            event: 真实的 AstrMessageEvent 实例。
        """
        if chat_id not in self._skins:
            self._skins[chat_id] = SessionSkin()
            
        skin = self._skins[chat_id]
        skin.platform_meta = event.platform_meta
        skin.msg_type = event.get_message_type()
        skin.self_id = event.get_self_id()
        skin.session_id = event.get_session_id()
        
        # 直接复用 event 里的 group 对象 (私聊时为 None)
        skin.group = getattr(event.message_obj, 'group', None)
        
        skin.sender = event.message_obj.sender
        skin.unified_msg_origin = event.unified_msg_origin
        
        # 尝试提取底层 bot 实例
        if skin.bot is None:
            skin.bot = getattr(event, 'bot', None)
        _log(True, "info", f"[Forger]: 已经创建{chat_id}的skin")

    def create_skin(self, chat_id: str, platform: 'Platform', session_id: str, 
                    msg_type: 'MessageType', self_id: str, 
                    group: 'Group' = None, sender: 'MessageMember' = None) -> SessionSkin:
        """
        无中生有创建会话壳。
        用于主动给【从未发过消息的群/人】发消息的场景。
        
        Args:
            chat_id: 会话唯一标识。
            platform: 平台实例。
            session_id: 会话 ID。
            msg_type: 消息类型。
            self_id: Bot 自身 ID。
            group: 群信息（私聊时为 None）。
            sender: 发送者信息。
            
        Returns:
            创建好的 SessionSkin 实例。
        """
        skin = SessionSkin()
        skin.platform_meta = platform.meta()
        skin.msg_type = msg_type
        skin.self_id = self_id
        skin.session_id = session_id
        skin.group = group
        skin.sender = sender
        
        # 根据 platform 和 msg_type 拼接出标准的 umo
        platform_id = platform.meta().id if platform.meta() else "unknown"
        skin.unified_msg_origin = f"{platform_id}:{msg_type.value}:{session_id}"
        
        # 从平台实例中提取 bot
        if hasattr(platform, 'get_client'):
            skin.bot = platform.get_client()
            
        self._skins[chat_id] = skin
        return skin

    # ========================
    # 造假区：输出 Event
    # ========================

    def forge_event(self, chat_id: str, prompt: str) -> 'AiocqhttpMessageEvent':
        """
        给定 chat_id (umo) 和想说的字符串，用对应的缓存壳创造并返回 Event。
        这是最常用的主动发消息入口。
        
        Args:
            chat_id: 会话唯一标识。
            prompt: 要发送的文本内容。
            
        Returns:
            伪造好的 AiocqhttpMessageEvent（或降级为 AstrMessageEvent）。
            
        Raises:
            ValueError: 如果该 chat_id 的 Skin 未准备好或不存在。
        """
        skin = self._skins.get(chat_id)
        if not skin or not skin.is_ready():
            raise ValueError(f"会话 {chat_id} 的 Skin 未准备好或不存在，请先调用 update_skin 或 create_skin。")
            
        return self._build_event(skin, prompt)

    def _build_event(self, skin: SessionSkin, prompt: str) -> 'AstrMessageEvent':
        """
        根据 SessionSkin 和 prompt，构造一个伪造的 Event。
        
        平台特化逻辑：
        - aiocqhttp 平台使用 AiocqhttpMessageEvent。
        - 其他平台降级到 AstrMessageEvent 父类。
        
        Args:
            skin: 会话壳。
            prompt: 要发送的文本内容。
            
        Returns:
            伪造好的 Event 实例。
        """
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        from astrbot.api.event import AstrMessageEvent # 引入父类兜底

        msg_obj = skin.clone_message_obj(prompt)
        fake_event = None

        # 基础设施层：根据平台类型选择合适的 Class 实例化
        if skin.platform_meta and skin.platform_meta.name == "aiocqhttp":
            if not skin.bot:
                raise ValueError("aiocqhttp 平台缺少 bot 实例，无法伪造。")
            fake_event = AiocqhttpMessageEvent(
                message_str=msg_obj.message_str,
                message_obj=msg_obj,
                platform_meta=skin.platform_meta,
                session_id=skin.session_id,
                bot=skin.bot
            )
        else:
            # 降级方案：对于不支持或未知的平台，使用纯父类
            fake_event = AstrMessageEvent(
                message_str=msg_obj.message_str,
                message_obj=msg_obj,
                platform_meta=skin.platform_meta,
                session_id=skin.session_id,
            )
            
        return fake_event

    # ========================
    # 辅助方法
    # ========================

    def get_skin(self, chat_id: str) -> SessionSkin | None:
        """获取指定会话的壳（只读观察用）。"""
        return self._skins.get(chat_id)

    def remove_skin(self, chat_id: str) -> bool:
        """移除指定会话的壳。"""
        if chat_id in self._skins:
            del self._skins[chat_id]
            return True
        return False
