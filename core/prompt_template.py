class MesStatePack:
    def __init__(self, messages: list, user_state: str):
        self.messages = messages
        self.user_state = user_state

def format_queue_prompt(pack: MesStatePack) -> str:
    """将 MesStatePack 格式化为客观的场景描述，不加任何引导词"""
    
    # 将状态翻译成客观的“舞台提示”
    state_hints = {
        "idle": "[场景提示：用户发完上述内容后，停下了手中的动作。]",
        "typing": "[场景提示：用户发完上述内容后，似乎还在继续打字，思绪可能还未完全表达完。]",
        "cleared": "[场景提示：用户在输入框里编辑了一些内容，但最终默默删除了，没有发送出来。]"
    }
    
    state_hint = state_hints.get(pack.user_state, "")
    
    messages_str = "\n".join(pack.messages)
    
    # 核心改变：去掉了“请结合...给出回复”这种指令式的话术
    # 直接把事实摆在 LLM 面前
    prompt = (
        f"(用户刚刚连续发送了以下内容)\n"
        f"{messages_str}\n\n"
        f"{state_hint}"
    )
    return prompt


QUEUE_SYSTEM_PROMPT_SUFFIX = """
【环境感知层】
你现在处于一个能够感知到用户细微输入行为的对话环境中。这就如同面对面交流时，你能直观地看到对方说话时的停顿、语速，或是欲言又止的神态。
具体而言，你可能会在用户的消息末尾看到类似 [场景提示：...] 的客观描述。这些是真实发生的行为，而非文本内容本身。

请将这些行为感知作为理解当下情境的自然背景。就像你在现实中遇到朋友说话停顿或欲言又止时那样，以你固有的性格和直觉去体会和应对，保持最自然的交流状态。
"""