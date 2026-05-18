"""Bot 可实施的 QQ 平台行为封装（Agent → 平台方向）。

所有动作均通过 go-cqhttp HTTP API 执行（bot.call_action），
不走 AstrBot 框架管线（不触发 Hook、不经过审批）。
"""

from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from astrbot.api import logger
from .models import _log

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

# ====== 日志开关：True 打印所有日志，False 静默 ======
enable_log = True


@dataclass
class ActionResult:
    """go-cqhttp API 调用的统一返回值契约。

    Attributes:
        success: 调用是否成功。
        retcode: 返回码（0=成功，-1=未捕获异常，ActionFailed 时取 e.retcode）。
        data: 响应中的 data 字段，成功时为 bot.call_action 解包后的 data。
        message: 失败时的错误描述，成功时为空字符串。
    """
    success: bool
    retcode: int = -1
    data: dict | None = None
    message: str = ""


@dataclass
class ActionContext:
    """动作执行上下文。

    封装一次 API 调用所需的会话维度信息，由调用方创建后传入 ActionManager 各方法。
    仿照 signals.py 中 SignalContext 的设计模式。

    Attributes:
        bot: CQHttp 实例（唯一必填）。
        user_id: 交互目标用户 QQ 号。
        group_id: 交互目标群号。
        message_id: 操作对象消息 ID。
        chat_id: 会话统一标识（unified_msg_origin）。
    """
    bot: Any
    user_id: str = ""
    group_id: str = ""
    message_id: str = ""
    chat_id: str = ""

    @classmethod
    def from_event(cls, event: "AstrMessageEvent") -> "ActionContext":
        """从 AstrMessageEvent 自动提取上下文信息。

        提取内容：bot、sender_id → user_id、群聊时 group_id、
        message_obj.message_id、unified_msg_origin → chat_id。

        Args:
            event: AstrBot 消息事件。

        Returns:
            填充好的 ActionContext。
        """
        bot = getattr(event, "bot", None)
        user_id = str(event.get_sender_id() or "")
        group_id = str(event.message_obj.group_id or "") if event.message_obj else ""
        message_id = str(event.message_obj.message_id or "") if event.message_obj else ""
        chat_id = event.unified_msg_origin or ""

        return cls(
            bot=bot,
            user_id=user_id,
            group_id=group_id,
            message_id=message_id,
            chat_id=chat_id,
        )


def _bot_guard(bot) -> ActionResult | None:
    """检查 bot 实例是否可用（有 call_action 方法）。

    若不可用则直接返回失败的 ActionResult，避免在业务方法中重复判断。
    """
    if bot is None or not callable(getattr(bot, "call_action", None)):
        return ActionResult(
            success=False,
            retcode=-1,
            data=None,
            message="bot 实例不可用或缺少 call_action 方法"
        )
    return None


class ActionManager:
    """Bot 平台动作集合（无状态封装层）。

    每个方法对应一个 go-cqhttp API 端点，第一参数统一为 ActionContext，
    由调用方提供，不依赖 EventForger 或其他内部状态。

    使用方式：
        from .action import ActionManager, ActionContext
        mgr = ActionManager()
        ctx = ActionContext.from_event(event)
        result = await mgr.recall_message(ctx)
        if result.success: ...

    bot.call_action 的三层返回模型：
    - 成功：返回 BaseResponse.data 字段内容（dict 或 None），不含 retcode
    - 行为内兜底：仍返回 data，无法单纯通过返回值判断成败
    - 框架异常：抛出 ActionFailed(status='failed', retcode=..., message=..., wording=...)
    """

    # ==========================================
    # 内部工具
    # ==========================================

    async def _call_action(self, bot, action: str, **params) -> ActionResult:
        """统一调用 bot.call_action 并包装结果为 ActionResult。

        处理 bot 可用性检查、ActionFailed 异常捕获、通用异常兜底。
        成功时 bot.call_action 已自动解包为 data 字段内容。

        Args:
            bot: CQHttp 实例。
            action: go-cqhttp API 端点名称。
            **params: 端点所需的参数。

        Returns:
            包装后的 ActionResult。
        """
        guard = _bot_guard(bot)
        if guard is not None:
            return guard

        try:
            from aiocqhttp.exceptions import ActionFailed
            result = await bot.call_action(action, **params)
            _log(enable_log, "debug", f"[ActionManager] {action} 提交完毕")
            return ActionResult(success=True, retcode=0, data=result)
        except ActionFailed as e:
            _log(enable_log, "warning", f"[ActionManager] {action} ActionFailed retcode={e.retcode} msg={e.message}")
            return ActionResult(
                success=False,
                retcode=e.retcode,
                message=e.wording or e.message or f"{action} 失败(retcode={e.retcode})"
            )
        except Exception as e:
            _log(enable_log, "error", f"[ActionManager] {action} 调用异常: {e}")
            return ActionResult(
                success=False,
                retcode=-1,
                message=str(e)
            )

    # ==========================================
    # 消息操作
    # ==========================================

    async def recall_message(self, ctx: ActionContext) -> ActionResult:
        """撤回一条消息。

        使用 ctx.message_id 作为撤回目标。

        对应 go-cqhttp API：delete_msg
        """
        return await self._call_action(ctx.bot, "delete_msg", message_id=int(ctx.message_id))

    async def get_message(self, ctx: ActionContext) -> ActionResult:
        """获取一条消息的详情。

        使用 ctx.message_id。

        对应 go-cqhttp API：get_msg
        """
        return await self._call_action(ctx.bot, "get_msg", message_id=int(ctx.message_id))

    async def mark_all_as_read(self, ctx: ActionContext) -> ActionResult:
        """标记所有未读消息为已读。

        对应 NapCat API：_mark_all_as_read
        """
        return await self._call_action(ctx.bot, "_mark_all_as_read")

    async def forward_to_friend(self, ctx: ActionContext) -> ActionResult:
        """转发一条消息到好友。

        使用 ctx.message_id（来源）+ ctx.user_id（目标）。

        对应 NapCat API：forward_friend_single_msg
        """
        return await self._call_action(ctx.bot, "forward_friend_single_msg",
                                       message_id=int(ctx.message_id),
                                       user_id=ctx.user_id)

    async def forward_to_group(self, ctx: ActionContext) -> ActionResult:
        """转发一条消息到群。

        使用 ctx.message_id（来源）+ ctx.group_id（目标）。

        对应 NapCat API：forward_group_single_msg
        """
        return await self._call_action(ctx.bot, "forward_group_single_msg",
                                       message_id=int(ctx.message_id),
                                       group_id=ctx.group_id)

    # ==========================================
    # 表情与输入状态（身体语言）
    # ==========================================

    async def set_emoji_like(self, ctx: ActionContext, emoji_id: str,
                              set_like: bool = True) -> ActionResult:
        """给指定消息点表情赞或取消。

        使用 ctx.message_id。

        Args:
            ctx: 动作上下文。
            emoji_id: 表情 ID（如 "32"）。
            set_like: True 点赞，False 取消。

        对应 NapCat API：set_msg_emoji_like
        """
        return await self._call_action(ctx.bot, "set_msg_emoji_like",
                                       message_id=int(ctx.message_id),
                                       emoji_id=emoji_id,
                                       set=set_like)

    async def set_input_status(self, ctx: ActionContext, event_type: int = 1) -> ActionResult:
        """设置 Bot 输入状态（正在输入 / 停止输入）。

        使用 ctx.user_id 作为目标用户。
        对 SaySync 天然适用：主动说话前调此接口亮输入状态可营造真人感。

        Args:
            ctx: 动作上下文。
            event_type: 1=正在输入，2=停止输入。

        对应 NapCat API：set_input_status
        """
        return await self._call_action(ctx.bot, "set_input_status",
                                       user_id=ctx.user_id,
                                       event_type=event_type)

    # ==========================================
    # 互动操作（私聊 + 群聊通用）
    # ==========================================

    async def send_poke(self, ctx: ActionContext) -> ActionResult:
        """戳一戳指定用户。

        使用 ctx.user_id（目标）+ ctx.group_id（群聊自动走 group_poke）。

        对应 go-cqhttp API：friend_poke / group_poke
        """
        if ctx.group_id:
            return await self._call_action(ctx.bot, "group_poke",
                                           group_id=int(ctx.group_id),
                                           user_id=int(ctx.user_id))
        else:
            return await self._call_action(ctx.bot, "friend_poke",
                                           user_id=int(ctx.user_id))

    async def send_like(self, ctx: ActionContext, times: int = 1) -> ActionResult:
        """给指定用户点赞。

        使用 ctx.user_id。

        Args:
            ctx: 动作上下文。
            times: 点赞次数，默认 1。

        对应 go-cqhttp API：send_like
        """
        return await self._call_action(ctx.bot, "send_like",
                                       user_id=int(ctx.user_id),
                                       times=times)

    # ==========================================
    # 群管理操作
    # ==========================================

    async def set_group_ban(self, ctx: ActionContext, duration: int) -> ActionResult:
        """禁言指定群成员。

        使用 ctx.group_id（群）+ ctx.user_id（目标）。
        duration 为 0 表示取消禁言。

        Args:
            ctx: 动作上下文。
            duration: 禁言时长（秒），0 表示取消禁言。

        对应 go-cqhttp API：set_group_ban
        """
        return await self._call_action(ctx.bot, "set_group_ban",
                                       group_id=int(ctx.group_id),
                                       user_id=int(ctx.user_id),
                                       duration=duration)

    async def set_group_kick(self, ctx: ActionContext,
                              reject_add_request: bool = False) -> ActionResult:
        """踢出指定群成员。

        使用 ctx.group_id（群）+ ctx.user_id（目标）。

        Args:
            ctx: 动作上下文。
            reject_add_request: 是否同时拒绝此人后续的加群请求。

        对应 go-cqhttp API：set_group_kick
        """
        return await self._call_action(ctx.bot, "set_group_kick",
                                       group_id=int(ctx.group_id),
                                       user_id=int(ctx.user_id),
                                       reject_add_request=reject_add_request)

    async def set_group_admin(self, ctx: ActionContext,
                               enable: bool = True) -> ActionResult:
        """设置或取消群管理员。

        使用 ctx.group_id（群）+ ctx.user_id（目标）。

        Args:
            ctx: 动作上下文。
            enable: True 设置管理员，False 取消管理员。

        对应 go-cqhttp API：set_group_admin
        """
        return await self._call_action(ctx.bot, "set_group_admin",
                                       group_id=int(ctx.group_id),
                                       user_id=int(ctx.user_id),
                                       enable=enable)

    async def set_group_card(self, ctx: ActionContext,
                              card: str = "") -> ActionResult:
        """设置群成员名片（群昵称）。

        使用 ctx.group_id（群）+ ctx.user_id（目标）。
        card 为空字符串表示取消群名片。

        Args:
            ctx: 动作上下文。
            card: 新的群名片内容，空字符串表示取消。

        对应 go-cqhttp API：set_group_card
        """
        return await self._call_action(ctx.bot, "set_group_card",
                                       group_id=int(ctx.group_id),
                                       user_id=int(ctx.user_id),
                                       card=card)

    async def send_group_sign(self, ctx: ActionContext) -> ActionResult:
        """群打卡。

        使用 ctx.group_id。

        对应 NapCat API：send_group_sign
        """
        return await self._call_action(ctx.bot, "send_group_sign", group_id=ctx.group_id)

    async def set_essence_msg(self, ctx: ActionContext) -> ActionResult:
        """将一条消息设为群精华。

        使用 ctx.message_id。

        对应 NapCat API：set_essence_msg
        """
        return await self._call_action(ctx.bot, "set_essence_msg",
                                       message_id=int(ctx.message_id))

    # ==========================================
    # 用户信息
    # ==========================================

    async def get_user_status(self, ctx: ActionContext) -> ActionResult:
        """查询指定用户的在线状态。

        使用 ctx.user_id。

        对应 NapCat API：get_user_status
        """
        return await self._call_action(ctx.bot, "get_user_status", user_id=ctx.user_id)

    async def get_recent_contact(self, ctx: ActionContext, count: int = 10) -> ActionResult:
        """获取最近会话列表。

        Args:
            ctx: 动作上下文。
            count: 获取数量，默认 10。

        对应 NapCat API：get_recent_contact
        """
        return await self._call_action(ctx.bot, "get_recent_contact", count=count)

    # ==========================================
    # 文件操作
    # ==========================================

    async def send_online_file(self, ctx: ActionContext, file_path: str,
                                file_name: str = "") -> ActionResult:
        """向指定用户发送在线文件。

        使用 ctx.user_id。

        Args:
            ctx: 动作上下文。
            file_path: 本地文件路径。
            file_name: 文件名（可选，默认取 file_path 中的文件名）。

        对应 NapCat API：send_online_file
        """
        params = {"user_id": ctx.user_id, "file_path": file_path}
        if file_name:
            params["file_name"] = file_name
        return await self._call_action(ctx.bot, "send_online_file", **params)
