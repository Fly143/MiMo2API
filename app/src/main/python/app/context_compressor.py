"""上下文压缩器 — 对话超长时压缩中间历史为摘要，保留 head + tail。

算法（参考 Hermes 三段式）：
1. 拆分消息为 head（system + 首轮）、middle（中间历史）、tail（最近 ~20K tokens）
2. 调 LLM 将 middle 压缩为结构化摘要
3. 摘要作为 system 消息注入 head 与 tail 之间
4. 压缩后的消息列表送回 build_query_from_messages 构建最终 query
"""

import os
from types import SimpleNamespace

MAX_QUERY_CHARS = int(os.getenv("MIMO_MAX_QUERY_CHARS", "1048576"))
TAIL_TOKEN_BUDGET = 20000
TAIL_CHAR_BUDGET = TAIL_TOKEN_BUDGET * 4  # 保守估算：1 token ≈ 4 字符（英文/混合）


def estimate_chars(messages):
    """估算消息列表总字符数"""
    total = 0
    for msg in messages:
        content = msg.content or ""
        if isinstance(content, list):
            text_parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            content = " ".join(text_parts)
        total += len(content) + len(msg.role) + 2
    return total


def should_compress(messages):
    """判断是否需要压缩"""
    return estimate_chars(messages) > MAX_QUERY_CHARS


def split_messages(messages):
    """
    将消息拆分为 head、middle、 tail 三段。
    
    Returns:
        (head, middle, tail) — 三个消息列表
    """
    if not messages:
        return [], [], []

    # 分离 system 和其他消息
    system_msgs = [m for m in messages if m.role == "system"]
    other_msgs = [m for m in messages if m.role != "system"]

    # head: system + 第一条 user-assistant 交换
    head = list(system_msgs)
    head_turns = []
    saw_user = False
    for msg in other_msgs:
        head_turns.append(msg)
        if msg.role == "user":
            saw_user = True
        if saw_user and msg.role == "assistant":
            break
    head.extend(head_turns)

    # tail: 从尾部往前，按字符预算保留
    tail = []
    tail_chars = 0
    for msg in reversed(other_msgs):
        content = msg.content or ""
        if isinstance(content, list):
            text_parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            content = " ".join(text_parts)
        msg_chars = len(content) + len(msg.role) + 2
        if tail_chars + msg_chars > TAIL_CHAR_BUDGET and tail:
            break
        tail.insert(0, msg)
        tail_chars += msg_chars

    # middle: 去掉 head 和 tail 后的中间部分
    head_ids = {id(m) for m in head}
    tail_ids = {id(m) for m in tail}
    middle = [m for m in messages if id(m) not in head_ids and id(m) not in tail_ids]

    return head, middle, tail


def build_summary_prompt(messages):
    """构建摘要 prompt"""
    lines = []
    for msg in messages:
        role = msg.role
        content = msg.content or ""
        if isinstance(content, list):
            text_parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            content = " ".join(text_parts)
        lines.append(f"{role}: {content}")

    conversation_text = "\n".join(lines)

    return (
        "请将以下对话历史压缩为一段简洁的中文摘要，保留关键信息：\n"
        "- 已完成的任务和决策\n"
        "- 待解决的问题\n"
        "- 用户偏好和重要上下文\n"
        "- 关键数据和结论\n\n"
        f"对话历史：\n{conversation_text}\n\n"
        "摘要（中文）："
    )


async def compress_messages(messages, model, client):
    """
    压缩中间消息为摘要。
    
    Args:
        messages: 消息列表
        model: 模型名
        client: MimoClient 实例
    
    Returns:
        (summary_text, compressed_messages) — 压缩后的摘要和消息列表。
        如果不需要压缩或失败，返回 (None, 原消息列表)。
    """
    head, middle, tail = split_messages(messages)

    if not middle:
        return None, messages

    # 调 LLM 生成摘要
    prompt = build_summary_prompt(middle)

    try:
        content, _, _ = await client.call_api(prompt, False, model)
    except Exception as e:
        print(f"[ContextCompressor] 摘要调用失败，回退到截断模式: {e}")
        return None, messages

    if not content:
        return None, messages

    # 构建摘要消息（作为 system 消息，会被 build_query_from_messages 合并到最前面）
    summary_msg = SimpleNamespace(
        role="system",
        content=f"[对话摘要] 原对话已压缩，请基于以下摘要继续对话：\n{content}",
    )

    compressed = head + [summary_msg] + tail
    print(
        f"[ContextCompressor] 压缩完成：{len(messages)} 条 → {len(compressed)} 条 "
        f"(middle {len(middle)} 条 → 摘要)"
    )
    return content, compressed
