"""会话管理 — 消息指纹续接 MiMo conversationId

通过 SHA256 指纹检测消息连续性，复用 MiMo 后端会话，
避免每次都生成新 conversationId 导致上下文丢失。

参考实现：
  GoblinHonest/mimo2api_mimoapi — session.ts / session-marker.ts
  (https://github.com/GoblinHonest/mimo2api_mimoapi)
"""

import json
import hashlib
import time
import uuid
from pathlib import Path

SESSION_FILE = Path(__file__).parent.parent / "sessions.json"

# token 超限后强制清屏（MiMo 上下文 ~128K，留余量）
TOKEN_THRESHOLD = 150000
# 会话 7 天过期
SESSION_TTL = 7 * 86400
# 每个账号最多保留的会话数
MAX_SESSIONS_PER_ACCOUNT = 20


def _load() -> dict:
    if not SESSION_FILE.exists():
        return {}
    try:
        return json.loads(SESSION_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _fingerprint(messages: list) -> str:
    """计算消息列表的 SHA256 指纹。

    只取最后 5 条非 system 消息，每条消息截断到前 200 字符。
    与 GoblinHonest 的 calculateMessageFingerprint 策略一致。
    """
    non_sys = [m for m in messages if getattr(m, 'role', '') != 'system']
    recent = non_sys[-5:]
    if not recent:
        return ''
    content = json.dumps([
        {
            "role": getattr(m, 'role', ''),
            "content": str(getattr(m, 'content', ''))[:200],
        }
        for m in recent
    ], sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()


def _is_continuation(messages: list, stored_fingerprint: str) -> bool:
    """检查当前消息列表是否是存储在 fingerprint 中的会话的延续。

    策略（与 GoblinHonest 的 isMessageContinuation 一致）：
    从最长 slice 开始往前查找，看是否有一段消息的指纹匹配存储的指纹。
    """
    if not stored_fingerprint:
        return False

    non_sys = [m for m in messages if getattr(m, 'role', '') != 'system']
    # 只有 1 条非 system 消息 → 新对话
    if len(non_sys) < 2:
        return False

    for i in range(len(non_sys), 0, -1):
        slice_msgs = (
            [m for m in messages if getattr(m, 'role', '') == 'system']
            + non_sys[:i]
        )
        slice_fp = _fingerprint(slice_msgs)
        if slice_fp == stored_fingerprint:
            return True

    return False


def get_or_create_session(
    account_id: str,
    messages: list,
    model: str = "mimo-v2-pro",
) -> tuple:
    """获取或创建会话。

    Args:
        account_id: 账号标识（如 user_id）
        messages: 当前请求的消息列表
        model: 模型名

    Returns:
        (conversation_id: str, is_new: bool)
        - conversation_id: MiMo 会话 ID（复用或新建）
        - is_new: True=新建会话, False=复用了现有会话
    """
    db = _load()
    key = f"account_{account_id}"
    sessions = db.get(key, [])
    current_fp = _fingerprint(messages)

    if not current_fp:
        # 空消息 → 全新的会话
        return _create_new(key, model, sessions, db)

    now = time.time()

    # 清理过期会话
    sessions = [s for s in sessions if now - s.get('last_used', 0) < SESSION_TTL]

    # 尝试匹配现有会话
    for s in sessions:
        if s.get('model') != model:
            continue
        if not s.get('fingerprint'):
            continue

        if _is_continuation(messages, s['fingerprint']):
            # token 超限检查
            if s.get('prompt_tokens', 0) > TOKEN_THRESHOLD:
                # 超限 → 清屏，跳出匹配创建新会话
                continue

            # 复用会话：更新指纹和最后使用时间
            s['fingerprint'] = current_fp
            s['last_used'] = now
            _save({**db, key: sessions})
            return s['conversation_id'], False

    # 没有匹配的会话 → 创建新会话
    return _create_new(key, model, sessions, db)


def _create_new(key: str, model: str, sessions: list, db: dict) -> tuple:
    """创建新会话并持久化。"""
    now = time.time()
    conv_id = uuid.uuid4().hex[:32]
    current_fp = ''  # 空指纹，首次使用时由 update_tokens 更新

    sessions.append({
        'conversation_id': conv_id,
        'fingerprint': current_fp,
        'prompt_tokens': 0,
        'model': model,
        'created': now,
        'last_used': now,
    })

    # 限制每个账号的会话数
    if len(sessions) > MAX_SESSIONS_PER_ACCOUNT:
        sessions = sessions[-MAX_SESSIONS_PER_ACCOUNT:]

    _save({**db, key: sessions})
    return conv_id, True


def update_fingerprint(account_id: str, conversation_id: str, messages: list) -> None:
    """首次响应后用真正的消息指纹更新会话。"""
    db = _load()
    key = f"account_{account_id}"
    sessions = db.get(key, [])
    fp = _fingerprint(messages)
    if not fp:
        return
    for s in sessions:
        if s['conversation_id'] == conversation_id:
            if not s.get('fingerprint'):
                s['fingerprint'] = fp
            break
    _save({**db, key: sessions})


def update_tokens(account_id: str, conversation_id: str, prompt_tokens: int) -> None:
    """更新会话的累积 token 计数。"""
    if not prompt_tokens:
        return
    db = _load()
    key = f"account_{account_id}"
    sessions = db.get(key, [])
    for s in sessions:
        if s['conversation_id'] == conversation_id:
            s['prompt_tokens'] = s.get('prompt_tokens', 0) + prompt_tokens
            s['last_used'] = time.time()
            break
    _save({**db, key: sessions})
