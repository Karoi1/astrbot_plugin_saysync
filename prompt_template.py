

class MesStatePack:
    def __init__(self, messages: list, user_state: str):
        self.messages = messages
        self.user_state = user_state
def format_queue_prompt(pack: MesStatePack) -> str:
    """将 MesStatePack 格式化为 LLM 能理解的 prompt"""
    state_explanations = {
        "idle": "用户发送完这些消息后，停止了输入，处于空闲状态。",
        "typing": "用户发送完这些消息后，仍在继续输入下一句（被超时或队列上限强制截断），表示用户的思绪可能还未完全表达完。",
        "cleared": "用户在输入框中打了一些字，但最终又全部删除了没有发出来，表现为欲言又止或放弃了某句话。"
    }
    
    state_hint = state_explanations.get(pack.user_state, "未知状态。")
    
    messages_str = "\n".join(pack.messages)
    
    prompt = (
        f"用户刚刚连续发送了以下消息（请注意观察时间戳来感受用户的输入节奏和停顿）：\n"
        f"{messages_str}\n\n"
        f"用户发送完上述内容后的最终状态：{pack.user_state}（{state_hint}）\n\n"
        f"请结合以上时间节奏和最终状态来理解用户当下的情绪和意图，并给出你的回复。"
    )
    return prompt


QUEUE_SYSTEM_PROMPT_SUFFIX = """
【特殊对话模式：多消息输入感知】
你可能会收到包含精确时间戳和“最终输入状态”的用户消息。请遵循以下规则处理：
1. 将时间戳的间隔作为感知用户情绪节奏（如急促、犹豫、停顿）的重要参考。
2. 根据“最终状态”调整你的回应策略：
   - idle：正常回复。
   - typing：用户的话被强制截断了。你可以先回应当前内容，语气上可以带有“你似乎还有话没说完”的察觉，或者给出简短、留白较多的回复以等待对方继续。
   - cleared：用户欲言又止。请敏锐地察觉这种情绪，以你人格的性格和思绪去理解用户的意思
3. 绝对禁止在回复中提及“时间戳”、“状态”、“系统提示”、“打包”等底层机制词汇，保持你原本的人格和沉浸感。
"""