"""上下文压缩器 — 两种模式，回复后检测余量触发。

模式 1（truncation）：滑动窗口裁剪，保留 head + tail，丢弃 middle
模式 2（compress）：三段式 LLM 摘要，保留 head + tail + middle 摘要

触发时机：模型回复后，检测消息总字符数 > 80% MAX_QUERY_CHARS
尾部保留：按 token budget（~20K tokens ≈ 80K 字符）
"""

import os
from types import SimpleNamespace

MAX_QUERY_CHARS = int(os.getenv("MIMO_MAX_QUERY_CHARS", "1048576"))
COMPRESS_THRESHOLD = 0.80  # 80% 触发
TAIL_TOKEN_BUDGET = 20000
TAIL_CHAR_BUDGET = TAIL_TOKEN_BUDGET * 4  # ~80K 字符


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
    """判断是否需要压缩（80% 阈值）"""
    return estimate_chars(messages) > MAX_QUERY_CHARS * COMPRESS_THRESHOLD


def split_messages(messages):
    """
    将消息拆分为 head、middle、tail 三段。
    head: system + 第一条 user-assistant 交换
    tail: 从尾部往前，按 token budget 保留
    middle: 中间部分（将被裁剪或压缩）

    Returns:
        (head, middle, tail) — 三个消息列表
    """
    if not messages:
        return [], [], []

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

    # tail: 从尾部往前，按 token budget 保留
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


def truncate_messages(messages):
    """
    模式 1：滑动窗口裁剪。
    保留 head + tail，丢弃 middle。
    """
    head, middle, tail = split_messages(messages)
    if not middle:
        return messages

    result = head + tail
    print(
        f"[ContextCompressor:truncation] {len(messages)} 条 → {len(result)} 条 "
        f"(丢弃 {len(middle)} 条 middle)"
    )
    return result


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
    模式 2：三段式 LLM 摘要。
    调 LLM 将 middle 压缩为结构化摘要。

    Returns:
        (summary_text, compressed_messages) — 压缩后的摘要和消息列表。
        如果不需要压缩或失败，返回 (None, 原消息列表)。
    """
    head, middle, tail = split_messages(messages)

    if not middle:
        return None, messages

    prompt = build_summary_prompt(middle)

    try:
        content, _, _, _ = await client.call_api(prompt, False, model)
    except Exception as e:
        print(f"[ContextCompressor:compress] 摘要调用失败，回退到截断: {e}")
        return None, truncate_messages(messages)

    if not content:
        return None, truncate_messages(messages)

    summary_msg = SimpleNamespace(
        role="system",
        content=f"[对话摘要] 原对话已压缩，请基于以下摘要继续对话：\n{content}",
    )

    compressed = head + [summary_msg] + tail
    print(
        f"[ContextCompressor:compress] {len(messages)} 条 → {len(compressed)} 条 "
        f"(middle {len(middle)} 条 → 摘要)"
    )
    return content, compressed
