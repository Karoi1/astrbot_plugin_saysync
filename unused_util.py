


@filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE,priority=-1)
async def debug_extract_event_skin(self, event: AstrMessageEvent):
    """调试用：扒取真实 Event 的底层属性"""
    logger.info("========== 开始扒皮真实 Event ==========")
    
    # 1. Event 层核心属性
    logger.info(f"unified_msg_origin: {event.unified_msg_origin}")
    logger.info(f"get_platform_name(): {event.get_platform_name()}")
    logger.info(f"get_platform_id(): {event.get_platform_id()}")
    logger.info(f"get_self_id(): {event.get_self_id()}")
    logger.info(f"get_sender_id(): {event.get_sender_id()}")
    logger.info(f"get_session_id(): {event.get_session_id()}")
    
    # 2. 平台元数据 (PlatformMetadata)
    if hasattr(event, 'platform_meta') and event.platform_meta:
        pm = event.platform_meta
        logger.info(f"platform_meta.name: {pm.name}")
        logger.info(f"platform_meta.id: {pm.id}")
        logger.info(f"platform_meta.description: {pm.description}")
        # 打印整个 dataclass 转字典，看全貌
        import dataclasses
        if dataclasses.is_dataclass(pm):
            logger.info(f"platform_meta FULL: {dataclasses.asdict(pm)}")
    
    # 3. 消息对象
    if hasattr(event, 'message_obj') and event.message_obj:
        msg = event.message_obj
        logger.info(f"message_obj.type: {msg.type}")
        logger.info(f"message_obj.self_id: {msg.self_id}")
        logger.info(f"message_obj.session_id: {msg.session_id}")
        logger.info(f"message_obj.message_id: {msg.message_id}")
        logger.info(f"message_obj.group_id: {msg.group_id}")
        
        # 发送者信息 (MessageMember)
        if hasattr(msg, 'sender') and msg.sender:
            import dataclasses
            if dataclasses.is_dataclass(msg.sender):
                logger.info(f"message_obj.sender FULL: {dataclasses.asdict(msg.sender)}")
        
        # 原始消息结构 (看我们伪造时需要塞什么)
        logger.info(f"message_obj.raw_message type: {type(msg.raw_message)}")
        # 注意：不要打印 raw_message 的内容，通常巨大且包含敏感信息
        
    logger.info("========== 扒皮结束 ==========")
    
    # 打印完就杀掉，不影响正常流程
    event.stop_event()





# ========== 强力 DEBUG：对比正常请求与伪造请求的差异 ==========
is_proactive = event.get_extra("is_implicit_proactive", False)
tag = "[主动请求]" if is_proactive else "[正常请求]"

logger.info(f"================ {tag} ProviderRequest 拆解 ================")
logger.info(f"1. prompt 类型与内容: {type(req.prompt)} | {req.prompt}")
logger.info(f"2. session_id: {req.session_id}")
logger.info(f"3. system_prompt (前50字): {(req.system_prompt or '')[:50]}")
logger.info(f"4. model: {req.model}")
logger.info(f"5. func_tool: {req.func_tool}")

# 重点怀疑对象：conversation 对象
if req.conversation:
    logger.info(f"6. conversation.cid: {req.conversation.cid}")
    logger.info(f"   conversation.persona_id: {getattr(req.conversation, 'persona_id', 'N/A')}")
else:
    logger.warning(f"6. conversation: >>> 为 None !!!! <<< (这极有可能是卡死的原因)")
    
# 重点怀疑对象：contexts 历史上下文
if req.contexts:
    logger.info(f"7. contexts 长度: {len(req.contexts)}")
    if len(req.contexts) > 0:
        # 只打印最后一条上下文的 role 和前 50 字
        last_ctx = req.contexts[-1]
        logger.info(f"   最后一条 context role: {last_ctx.get('role')}")
        content = last_ctx.get('content', '')
        content_preview = str(content)[:50] if content else "空"
        logger.info(f"   最后一条 context content(前50字): {content_preview}")
else:
    logger.warning(f"7. contexts: >>> 为空列表 !!!! <<<")

logger.info(f"8. image_urls 数量: {len(req.image_urls or [])}")
logger.info(f"9. audio_urls 数量: {len(req.audio_urls or [])}")
logger.info(f"=======================================================")
# ================================================================