"""工具函数 — MiMo2API

凭证解析、媒体提取/上传、消息构建。
"""

import re
import hashlib
import json as _json
import httpx
from typing import Optional, List, Tuple, Dict, Any
from .config import MimoAccount


def parse_curl(curl_command: str) -> Optional[MimoAccount]:
    """解析cURL命令提取Mimo账号凭证。"""
    account = {
        'service_token': '',
        'user_id': '',
        'xiaomichatbot_ph': ''
    }

    cookie_match = re.search(r"(?:-b|--cookie)\s+'([^']+)'", curl_command)
    if not cookie_match:
        cookie_match = re.search(r'(?:-b|--cookie)\s+"([^"]+)"', curl_command)
    if not cookie_match:
        cookie_match = re.search(r"-H\s+'[Cc]ookie:\s*([^']+)'", curl_command)
    if not cookie_match:
        cookie_match = re.search(r'-H\s+"[Cc]ookie:\s*([^"]+)"', curl_command)
    if not cookie_match:
        return None

    cookies = cookie_match.group(1)

    service_token_match = re.search(r'serviceToken="([^"]+)"', cookies)
    if service_token_match:
        account['service_token'] = service_token_match.group(1)

    user_id_match = re.search(r'userId=(\d+)', cookies)
    if user_id_match:
        account['user_id'] = user_id_match.group(1)

    ph_match = re.search(r'xiaomichatbot_ph="([^"]+)"', cookies)
    if ph_match:
        account['xiaomichatbot_ph'] = ph_match.group(1)

    if not account['service_token']:
        return None

    return MimoAccount(**account)


def extract_medias_from_messages(messages: list) -> Tuple[str, list, list, list]:
    """从消息列表中提取图片/视频/音频媒体和文本文件。

    Returns:
        (query_text, base64_medias, text_files, processed_messages)
        text_files: [{"base64": ..., "filename": ..., "mimeType": ...}, ...]
    """
    base64_medias = []
    text_files = []
    seen_base64 = set()
    processed_messages = []

    for msg in messages:
        text = ""
        content = msg.content or ""

        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text += item.get("text", "")
                elif item.get("type") == "image_url":
                    img_url = item.get("image_url", {})
                    url = img_url.get("url", "") if isinstance(img_url, dict) else str(img_url)
                    if url and url.startswith("data:"):
                        base64 = url.split(",", 1)[1] if "," in url else url
                        if base64 and base64 not in seen_base64:
                            mime = url.split(";")[0].split(":")[1] if ";" in url else "image/jpeg"
                            base64_medias.append({
                                "base64": base64,
                                "mimeType": mime,
                                "type": "image"
                            })
                            seen_base64.add(base64)
                elif item.get("type") == "file":
                    # 文本文件：收集 base64 用于 MiMo 上传（mediaType="file"）
                    file_obj = item.get("file", {})
                    if isinstance(file_obj, dict):
                        filename = file_obj.get("filename", "file.txt")
                        file_data = file_obj.get("file_data", "") or file_obj.get("data", "")
                        if file_data and file_data not in seen_base64:
                            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
                            text_files.append({
                                "base64": file_data,
                                "filename": filename,
                                "mimeType": "text/plain"
                            })
                            seen_base64.add(file_data)
        else:
            text = str(content) if content else ""

        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            text = _serialize_tool_calls(msg.tool_calls)

        if msg.role == "tool":
            tool_call_id = getattr(msg, 'tool_call_id', '') or ''
            clean = re.sub(r'\[TOOL_RESULT\]\s*', '', text, flags=re.IGNORECASE)
            text = f"[tool_result id={tool_call_id[:8]}] {clean}"

        processed_messages.append({"role": msg.role, "text": text})

    query_text = processed_messages[-1]["text"] if processed_messages else ""
    return query_text, base64_medias, text_files, processed_messages


def _serialize_tool_calls(tool_calls: list) -> str:
    """统一定义工具调用序列化 — 兼容 dict 和 pydantic model。"""
    tc_lines = []
    for tc in tool_calls:
        fn = _safe_nested_get(tc, "function")
        if not fn:
            continue
        fname = _safe_nested_get(fn, "name", "")
        args_str = _safe_nested_get(fn, "arguments", "{}")

        try:
            args = _json.loads(args_str) if isinstance(args_str, str) else args_str
            if isinstance(args, dict):
                kv = ", ".join(f"{k}={v!r}" for k, v in args.items())
            else:
                kv = str(args)
        except Exception:
            kv = str(args_str)

        tc_lines.append(f"TOOL_CALL: {fname}({kv})")

    return "\n".join(tc_lines)


def _safe_nested_get(obj, *keys, default=None):
    """安全嵌套取值 — 兼容 dict 和 pydantic model。"""
    for key in keys:
        if obj is None:
            return default
        if isinstance(obj, dict):
            obj = obj.get(key, default)
        else:
            obj = getattr(obj, key, default)
    return obj


async def upload_text_file_to_mimo(
    base64_data: str,
    filename: str,
    mime_type: str,
    account: MimoAccount,
    model: str = "mimo-v2.5-pro"
) -> Optional[Dict[str, Any]]:
    """上传文本文件到小米Mimo服务器。

    三步流程：genUploadInfo -> PUT 上传 -> resource/parse
    返回 multiMedias 格式的 dict，可直接传给 MiMo chat API。
    """
    if "," in base64_data:
        base64_data = base64_data.split(",", 1)[1]

    import base64 as b64
    binary_data = b64.b64decode(base64_data)

    md5 = hashlib.md5(binary_data).hexdigest()

    cookie = f"serviceToken={account.service_token}; userId={account.user_id}; xiaomichatbot_ph={account.xiaomichatbot_ph}"
    headers = {
        "Cookie": cookie,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://aistudio.xiaomimimo.com/",
        "Origin": "https://aistudio.xiaomimimo.com"
    }

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            ph = account.xiaomichatbot_ph
            info_res = await client.post(
                f"https://aistudio.xiaomimimo.com/open-apis/resource/genUploadInfo?xiaomichatbot_ph={ph}",
                json={"fileName": filename, "fileContentMd5": md5},
                headers=headers
            )
            info_data = info_res.json()
            if info_data.get("code") != 0 or not info_data.get("data"):
                print(f"[uploadTextFile] genUploadInfo failed: {info_data}")
                return None

            upload_url = info_data["data"]["uploadUrl"]
            resource_url = info_data["data"]["resourceUrl"]
            object_name = info_data["data"]["objectName"]

            put_headers = {"Content-Type": "application/octet-stream"}
            put_res = await client.put(upload_url, content=binary_data, headers=put_headers)
            if put_res.status_code != 200:
                print(f"[uploadTextFile] PUT failed: {put_res.status_code}")
                return None

            from urllib.parse import quote

            parse_params = {
                "fileUrl": resource_url,
                "objectName": object_name,
                "model": model,
                "xiaomichatbot_ph": ph,
            }

            parse_res = None
            for attempt in range(5):
                try:
                    resp = await client.post(
                        "https://aistudio.xiaomimimo.com/open-apis/resource/parse",
                        params=parse_params,
                        json={},
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                            "Referer": "https://aistudio.xiaomimimo.com/",
                            "Origin": "https://aistudio.xiaomimimo.com"
                        },
                        cookies=cookies
                    )
                    data = resp.json()
                    if data.get("code") == 0 and data.get("data", {}).get("id"):
                        parse_res = data
                        import asyncio
                        await asyncio.sleep(3)
                        break
                except Exception:
                    pass
                import asyncio
                await asyncio.sleep(2)

            if not parse_res:
                print("[uploadTextFile] Parse failed after retries")
                return None

            resource_id = parse_res["data"]["id"]
            return {
                "mediaType": "file",
                "fileUrl": resource_url,
                "compressedVideoUrl": "",
                "audioTrackUrl": "",
                "name": filename,
                "size": len(binary_data),
                "status": "completed",
                "objectName": object_name,
                "tokenUsage": parse_res["data"].get("tokenUsage", 0),
                "url": resource_id
            }

        except Exception as e:
            print(f"[uploadTextFile] Error: {e}")
            return None


async def upload_media_to_mimo(
    base64_data: str,
    mime_type: str,
    account: MimoAccount,
    model: str = "mimo-v2.5"
) -> Optional[Dict[str, Any]]:
    """上传媒体文件到小米Mimo服务器。

    三步流程：genUploadInfo -> PUT 上传 -> resource/parse
    """
    if "," in base64_data:
        base64_data = base64_data.split(",", 1)[1]

    import base64 as b64
    binary_data = b64.b64decode(base64_data)

    md5 = hashlib.md5(binary_data).hexdigest()
    import uuid
    ext = mime_type.split("/")[-1] if "/" in mime_type else "jpg"
    if ext == "jpeg":
        ext = "jpg"
    file_name = f"{uuid.uuid4().hex}.{ext}"

    ph = account.xiaomichatbot_ph
    cookies = {"serviceToken": account.service_token, "userId": account.user_id, "xiaomichatbot_ph": ph}
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://aistudio.xiaomimimo.com/",
        "Origin": "https://aistudio.xiaomimimo.com"
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            info_res = await client.post(
                "https://aistudio.xiaomimimo.com/open-apis/resource/genUploadInfo",
                params={"xiaomichatbot_ph": ph},
                json={"fileName": file_name},
                headers=headers,
                cookies=cookies
            )
            info_data = info_res.json()
            if info_data.get("code") != 0 or not info_data.get("data"):
                print(f"[uploadMedia] genUploadInfo failed: {info_data}")
                return None

            upload_url = info_data["data"]["uploadUrl"]
            resource_url = info_data["data"]["resourceUrl"]
            object_name = info_data["data"]["objectName"]

            put_headers = {"Content-Type": "application/octet-stream"}
            put_res = await client.put(upload_url, content=binary_data, headers=put_headers)
            if put_res.status_code != 200:
                print(f"[uploadMedia] PUT failed: {put_res.status_code}")
                return None

            from urllib.parse import quote

            parse_params = {
                "fileUrl": resource_url,
                "objectName": object_name,
                "model": model,
                "xiaomichatbot_ph": ph,
            }

            parse_res = None
            for attempt in range(5):
                try:
                    resp = await client.post(
                        "https://aistudio.xiaomimimo.com/open-apis/resource/parse",
                        params=parse_params,
                        json={},
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                            "Referer": "https://aistudio.xiaomimimo.com/",
                            "Origin": "https://aistudio.xiaomimimo.com"
                        },
                        cookies=cookies
                    )
                    data = resp.json()
                    if data.get("code") == 0 and data.get("data", {}).get("id"):
                        parse_res = data
                        import asyncio
                        await asyncio.sleep(3)
                        break
                except Exception:
                    pass
                import asyncio
                await asyncio.sleep(2)

            if not parse_res:
                print("[uploadMedia] Parse failed after retries")
                return None

            resource_id = parse_res["data"]["id"]
            is_video = mime_type.startswith("video/")
            is_audio = mime_type.startswith("audio/")
            media_type = "video" if is_video else ("audio" if is_audio else "image")

            return {
                "mediaType": media_type,
                "fileUrl": resource_url,
                "compressedVideoUrl": "",
                "audioTrackUrl": resource_url if is_audio else "",
                "name": file_name,
                "size": len(binary_data),
                "status": "completed",
                "objectName": object_name,
                "tokenUsage": parse_res["data"].get("tokenUsage", 106),
                "url": resource_id
            }

        except Exception as e:
            print(f"[uploadMedia] Error: {e}")
            return None


def build_query_from_messages(
    messages: list,
    tools: list = None,
    passthrough: bool = False,
    continuation: bool = False,
    no_truncate: bool = False,
) -> str:
    """从消息列表构建查询字符串。

    格式：系统消息（含工具提示词）→ 对话历史。
    MiMo API 没有 system/user 角色分离，query 是纯文本拼接。
    工具提示词嵌入 system 消息一次，不再每轮重复注入。
    无 system 消息但有 tools 时自动补 system。
    passthrough=True 时跳过 MiMoML 格式说明书，直接嵌入原始工具定义。

    continuation=True 时只发 system + tools + 最后一条 user 消息，
    跳过历史对话（MiMo 服务端通过 conversationId 已有上下文）。
    """
    from .tool_call import build_tool_prompt

    query_parts = []
    system_text = ""

    # 分离 system 消息和其他消息
    non_system_msgs = []
    for msg in messages:
        role = msg.role
        content = msg.content or ""

        if role == "system":
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                content = " ".join(text_parts)
            system_text = str(content).strip()
            continue

        non_system_msgs.append(msg)

    # continuation 模式：只取最后一条 user 消息
    if continuation and non_system_msgs:
        last_user = None
        for msg in reversed(non_system_msgs):
            if msg.role == "user":
                last_user = msg
                break
        if last_user:
            non_system_msgs = [last_user]

    for msg in non_system_msgs:
        role = msg.role
        content = msg.content or ""

        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
            content = " ".join(text_parts)

        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            content = _serialize_tool_calls(msg.tool_calls)

        if role == "tool":
            tool_call_id = getattr(msg, 'tool_call_id', '') or ''
            clean = re.sub(r'\[TOOL_RESULT\]\s*', '', content, flags=re.IGNORECASE)
            clean = clean.strip()
            content = f"[tool_result id={tool_call_id[:8]}] {clean}"

        query_parts.append(f"{role}: {content}")

    # 工具提示词嵌入 system 消息（一次，不再每轮重复追加末尾）
    if tools:
        tool_prompt = build_tool_prompt(tools, passthrough=passthrough)
        if tool_prompt:
            if system_text:
                system_text = system_text + "\n\n" + tool_prompt
            else:
                system_text = tool_prompt

    # system 消息插入最前面
    if system_text:
        query_parts.insert(0, f"system: {system_text}")

    full_query = "\n".join(query_parts)

    # === 长度保护：MiMo bot/chat 对单条 query 有 ~100KB 字符上限，
    # 超出会被服务端直接拒绝（"text you sent is too long"）。
    # continuation 模式下只发增量，通常远低于限制。
    # 非 continuation 模式下采用滑动窗口：保留 system，从尾部裁剪历史。
    MAX_QUERY_CHARS = int(__import__("os").getenv("MIMO_MAX_QUERY_CHARS", "95000"))
    if not no_truncate and len(full_query) > MAX_QUERY_CHARS:
        system_prefix = ""
        history_parts = query_parts
        if system_text:
            system_prefix = f"system: {system_text}\n"
            history_parts = query_parts[1:]  # 去掉 system 行

        kept = []
        used = len(system_prefix)
        for part in reversed(history_parts):
            part_len = len(part) + 1  # +1 是换行符
            if used + part_len > MAX_QUERY_CHARS and kept:
                break
            kept.insert(0, part)
            used += part_len

        full_query = system_prefix + "\n".join(kept)
        print(
            f"[QueryGuard] MiMo query exceeded {MAX_QUERY_CHARS} chars, "
            f"kept system + last {len(kept)} history parts "
            f"(final {len(full_query)} chars). Older history was truncated."
        )

    return full_query


def build_chunked_queries(
    messages: list,
    tools: list = None,
    passthrough: bool = False,
) -> list:
    """当全量 query 超限时，按消息边界拆分成多个 chunk。

    每个 chunk 包含 system + tools + 一部分历史消息，均在限制内。
    调用方按顺序发送 chunk，MiMo 通过 conversationId 累积上下文。
    最后一个 chunk 包含最新的 user 消息（触发模型回复）。

    Returns:
        [query_str, ...] — 如果不需要拆分则返回 [full_query]（单元素列表）
    """
    MAX_QUERY_CHARS = int(__import__("os").getenv("MIMO_MAX_QUERY_CHARS", "95000"))

    from .tool_call import build_tool_prompt

    # 提取 system 和 tool prompt
    system_text = ""
    non_system_msgs = []
    for msg in messages:
        if msg.role == "system":
            content = msg.content or ""
            if isinstance(content, list):
                text_parts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
                content = " ".join(text_parts)
            system_text = str(content).strip()
        else:
            non_system_msgs.append(msg)

    if tools:
        tool_prompt = build_tool_prompt(tools, passthrough=passthrough)
        if tool_prompt:
            system_text = (system_text + "\n\n" + tool_prompt).strip() if system_text else tool_prompt

    system_prefix = f"system: {system_text}\n" if system_text else ""
    sys_len = len(system_prefix)

    # 先试整体构建（不做截断，让 build_chunked_queries 自己拆分）
    full_query = build_query_from_messages(messages, tools=tools, passthrough=passthrough, no_truncate=True)
    if len(full_query) <= MAX_QUERY_CHARS:
        return [full_query]

    # 需要拆分：把 non_system_msgs 转成 "role: content" 字符串列表
    msg_strs = []
    for msg in non_system_msgs:
        content = msg.content or ""
        if isinstance(content, list):
            text_parts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
            content = " ".join(text_parts)
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            content = _serialize_tool_calls(msg.tool_calls)
        if msg.role == "tool":
            tool_call_id = getattr(msg, 'tool_call_id', '') or ''
            clean = __import__('re').sub(r'\[TOOL_RESULT\]\s*', '', content, flags=__import__('re').IGNORECASE).strip()
            content = f"[tool_result id={tool_call_id[:8]}] {clean}"
        msg_strs.append(f"{msg.role}: {content}")

    # 按消息边界拆分
    available = MAX_QUERY_CHARS - sys_len - 1  # -1 for newline after system
    chunks = []
    current_parts = []
    current_len = 0

    for ms in msg_strs:
        part_len = len(ms) + 1  # +1 for \n
        if part_len > available:
            # 单条消息本身就超限 → 按字符拆成多段
            # 先把已有的 parts 存起来
            if current_parts:
                chunks.append(system_prefix + "\n".join(current_parts))
                current_parts = []
                current_len = 0
            # 拆分这条超长消息
            role_prefix = ms.split(": ", 1)[0] + ": " if ": " in ms else ""
            body = ms[len(role_prefix):]
            suffix = "\n\n（内容未完，请不要回复，等待后续内容）"
            chunk_budget = available - len(role_prefix) - len(suffix) - 1
            pos = 0
            while pos < len(body):
                segment = body[pos:pos + chunk_budget]
                if pos + chunk_budget < len(body):
                    # 非最后一段，加提示让模型不要回复
                    chunks.append(system_prefix + role_prefix + segment + suffix)
                else:
                    # 最后一段，正常发送
                    current_parts.append(role_prefix + segment)
                    current_len = len(role_prefix) + len(segment) + 1
                pos += chunk_budget
        elif current_parts and current_len + part_len > available:
            # 当前 chunk 满了，保存并开始新 chunk
            chunks.append(system_prefix + "\n".join(current_parts))
            current_parts = [ms]
            current_len = part_len
        else:
            current_parts.append(ms)
            current_len += part_len

    if current_parts:
        chunks.append(system_prefix + "\n".join(current_parts))

    final = [c for c in chunks if c.strip()]
    print(f"[QueryGuard] Query split into {len(final)} chunks: {[len(c) for c in final]} chars")
    return final
