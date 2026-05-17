import asyncio
from enum import Enum
import uuid
import time

from astrbot.core.platform.astrbot_message import AstrBotMessage
from astrbot.api import logger
from astrbot.api.platform import PlatformMetadata, MessageMember, Group, MessageType


def _log(enable: bool, level: str, msg: str, *args, **kwargs):
    """内部日志辅助方法，受模块级 enable_log 开关控制。"""
    if not enable:
        return
    log_fn = getattr(logger, level, None)
    if log_fn and callable(log_fn):
        log_fn(msg, *args, **kwargs)


class ProactiveType(Enum):
    """主动说话冲动的底色枚举。
    
    描述 Bot 在对话暂歇后内心可能产生的各类延时冲动，
    用于 LLM 复盘时做类型化输出。
    """
    DIG = "察觉到对方欲言又止、含糊其辞或自相矛盾，内心产生想要轻巧追问、澄清真实想法的冲动。"
    PROMPT = "面对对方长时间沉默或卡壳，感到对话悬停，想要递个话头、抛个选项帮对方继续的冲动。"
    SUPPLEMENT = "话音刚落，脑海中突然闪过与当前话题直接相关的遗漏细节，产生'对了，还有这个'的自然补充冲动。"
    TEASE = "看着对方上钩或犯错，内心产生恶作剧般的戏谑感，想要停顿一下再抛出吐槽或玩笑的冲动。"
    SOFTEN = "意识到自己踩雷、说错话或察觉到对方情绪受挫/抗拒，内心想要立刻软化态度、给出台阶、温柔安抚的冲动。"
    PIVOT = "察觉到当前话题陷入死胡同、尴尬冷场或对方明显不感兴趣，为了挽救对话，内心想要努力切换到新话题的冲动。"
    FAREWELL = "察觉到对方已释放出明确的离场信号（回复变短、主动收尾、再无攀谈意愿），内心升起得体道别、结束对话的冲动。"

    def __str__(self):
        # 注意这里使用的是中文全角冒号“：”
        return self.value
    
    @classmethod
    def formatted(cls):
        """类方法：返回该枚举下所有成员格式化后的完整字符串。"""
        return "\n".join(f"- {item.name}：{item.value}" for item in cls)



class ProactiveLevel(Enum):
    """主动说话冲动的表达势能等级枚举。
    
    1~5 级，从微弱涟漪到决堤爆发，用于 LLM 判断冲动的强度。
    """
    FAINT = (1, "一闪而过的微弱涟漪，念头在脑海中轻轻掠过，稍纵即逝，甚至可以被轻易忽略。")
    SLIGHT = (2, "泛起的涟漪在心底转了个弯，引起了注意与轻微的咀嚼，但情绪仍处于静默观望的边缘。")
    EMERGING = (3, "念头开始明显涌动，表达欲已然成型，思绪在后台开始自觉组织语言，寻找出口。")
    STRONG = (4, "内心的鼓噪变得强烈且带有清晰的情绪色彩，试图压抑会感到明显的心理违和与不适。")
    OVERWHELMING = (5, "冲动如决堤般不可阻挡，到了“不吐不快”的绝对临界点，必须立刻寻找缝隙将其宣泄而出。")

    def __init__(self, level, description):
        self.level = level
        self.description = description

    def str(self):
        """返回该等级的文字描述。"""
        return self.description
    
    @classmethod
    def formated(cls):
        """类方法：返回该枚举下所有成员格式化后的完整字符串。"""
        return '\n'.join(f"- {item.level}：{item.description}" for item in cls)


class ProactiveEventResult(Enum):
    """主动任务事件的执行结果枚举。"""
    KILL = "KILL"
    PROCESS = "PROCESS"


class SchedulerResult(Enum):
    """消息调度器（攒批）的执行结果枚举。"""
    KILL = "KILL"
    PROCESS = "PROCESS"


class ProactiveTask:
    """主动说话任务单。
    
    封装一次主动说话所需的全部信息，由 mindflow 生成后提交给 ProactiveManager 延迟执行。
    
    Attributes:
        chat_id: 目标会话 ID。
        task_type: 冲动底色（ProactiveType）。
        instruction: 由 LLM 生成的具体草稿/指令。
        mindflow: 冲动溯源（极简的内心独白）。
        level: 表达势能等级（ProactiveLevel）。
        delay: 延迟执行的秒数。
        created_at: 任务创建时间戳。
    """
    def __init__(self, chat_id: str, task_type: ProactiveType, instruction: str, mindflow: str, level: ProactiveLevel, delay: float = 20.0):
        self.chat_id = chat_id
        self.task_type = task_type      # 类型
        self.instruction = instruction  # 草稿
        self.mindflow = mindflow        # 当时思绪
        self.level = level
        self.delay = delay
        self.created_at = time.time()


class UserStatus:
    """用户输入状态机。
    
    根据 aiocqhttp 的 input_status 推送，追踪用户从 idle → typing → cleared → cleared_sure 的状态转移，
    用于判断用户是否正在输入、欲言又止或已停止输入。
    """
    class StateMachine(Enum):
        """用户输入状态枚举。"""
        idle = 0               # 没打字 / 发送完消息后停止
        typing = 1             # 正在打字
        cleared = 2            # 打了字又删了，欲言又止
        cleared_sure = 3       # 同上，过滤用

    def __init__(self):
        self._mes_sent: bool = False
        self.sm = self.StateMachine.idle

    def set_mes_sent(self):
        """标记消息已发送，触发状态机重置。"""
        self.reset()
    
    def set_state(self, typing: bool):
        """根据外部输入状态更新状态机。"""
        self.state_transfer(typing)

    def state_transfer(self, _input_status: bool):
        """状态转移核心逻辑。当前在打字吗？会发送还是默默删除呢？"""
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
        """重置状态机到初始状态。"""
        self._mes_sent = False
        self.sm = self.StateMachine.idle

    @property
    def pack_state(self) -> str:
        """将当前状态打包为字符串，用于 Prompt 中的场景提示。
        
        cleared_sure 对外收敛为 cleared，避免过度细分干扰 LLM。
        """
        if self.sm == self.StateMachine.cleared_sure:
            return "cleared"
        return self.sm.name


class SessionContext:
    """单个 chat_id 的会话上下文。
    
    维护消息队列、挂起的 Future、定时器 Task、处理锁及看门狗等运行时状态。
    """
    def __init__(self):
        # 消息包
        self.message_queue: list[str] = []
        # 备战未来
        self.active_future: asyncio.Future | None = None
        self.timer_task: asyncio.Task | None = None
        # 会赢吗
        self.is_processing: bool = False
        self.lock_timestamp: float = -1
        self.watchdog_task: asyncio.Task | None = None


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
        # group 私聊时为 None，群聊时包含群号、群名等完整信息
        self.group: 'Group | None' = None  
        self.sender: 'MessageMember | None' = None
        self.unified_msg_origin: str = ""
        # bot 实例，目前能用 cqhttp
        self.bot = None

    def is_ready(self) -> bool:
        """检查壳是否已经收集完整，可以用来伪造 Event。"""
        return self.platform_meta is not None and self.unified_msg_origin != ""

    def clone_message_obj(self, prompt: str = "") -> 'AstrBotMessage':
        """
        根据保存的壳，伪造一个全新的 AstrBotMessage 对象。
        动态特征（如 message_id, raw_message）会自动生成假数据。
        
        Args:
            prompt: 要主动发送的文本内容。
            
        Returns:
            伪造好的 AstrBotMessage 实例。
        """
        # 动态导入，避免在非运行时环境报错
        from astrbot.core.platform.astrbot_message import AstrBotMessage
        from astrbot.core.message.components import Plain

        msg_obj = AstrBotMessage()
        msg_obj.type = self.msg_type
        msg_obj.self_id = self.self_id
        msg_obj.session_id = self.session_id
        
        # 更新：直接赋值整个 group 对象
        # 赋值后，AstrBotMessage 内部的 @property group_id 会自动返回 msg_obj.group.group_id
        msg_obj.group = self.group 
        
        msg_obj.sender = self.sender
        
        # 填充我们要主动发送的内容
        msg_obj.message = [Plain(prompt)] if prompt else []
        msg_obj.message_str = prompt
        
        # 动态特征：伪造防御性数据，防止底层报错
        msg_obj.raw_message = {} 
        msg_obj.message_id = str(uuid.uuid4()) # 必须生成全新的 ID
        
        return msg_obj
