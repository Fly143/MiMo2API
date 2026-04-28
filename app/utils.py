"""工具函数"""

import re
import hashlib
import httpx
from typing import Optional, List, Tuple, Dict, Any
from .config import MimoAccount


def parse_curl(curl_command: str) -> Optional[MimoAccount]:
    """
    解析cURL命令提取Mimo账号凭证

    Args:
        curl_command: cURL命令字符串

    Returns:
        MimoAccount对象或None
    """
    account = {
        'service_token': '',
        'user_id': '',
        'xiaomichatbot_ph': ''
    }

    # 提取cookies（支持多种格式）
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

    # 提取serviceToken
    service_token_match = re.search(r'serviceToken="([^"]+)"', cookies)
    if service_token_match:
        account['service_token'] = service_token_match.group(1)

    # 提取userId
    user_id_match = re.search(r'userId=(\d+)', cookies)
    if user_id_match:
        account['user_id'] = user_id_match.group(1)

    # 提取xiaomichatbot_ph
    ph_match = re.search(r'xiaomichatbot_ph="([^"]+)"', cookies)
    if ph_match:
        account['xiaomichatbot_ph'] = ph_match.group(1)

    # 验证必需字段
    if not account['service_token']:
        return None

    return MimoAccount(**account)


def safe_utf8_len(text: str, max_len: int) -> int:
    """
    安全的UTF-8字符串长度计算，避免在多字节字符中间截断

    Args:
        text: 文本字符串
        max_len: 最大长度

    Returns:
        安全的截断长度
    """
    if max_len <= 0 or max_len >= len(text):
        return len(text)
    return max_len


def extract_medias_from_messages(messages: list) -> Tuple[str, list, list]:
    """
    从消息列表中提取图片/视频/音频媒体

    Args:
        messages: 消息列表 (OpenAIMessage对象列表)

    Returns:
        (query_text, base64_medias, processed_messages)
    """
    base64_medias = []
    seen_base64 = set()

    processed_messages = []
    for msg in messages:
        text = ""
        content = msg.content or ""

        if isinstance(content, list):
            # 多模态格式: content 是数组
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text += item.get("text", "")
                elif item.get("type") == "image_url":
                    url = item.get("image_url", {}).get("url", "")
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
        else:
            text = str(content) if content else ""
        # 处理工具调用消息
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            calls = msg.tool_calls
            text = f"[工具调用] {calls}"
        # 处理工具返回结果
        if msg.role == "tool":
            tool_call_id = getattr(msg, 'tool_call_id', '')
            text = f"[工具结果 ID:{tool_call_id}] {text}"

        processed_messages.append({"role": msg.role, "text": text})

    # query 只取最后一条消息的文本
    query_text = processed_messages[-1]["text"] if processed_messages else ""

    return query_text, base64_medias, processed_messages


async def upload_media_to_mimo(
    base64_data: str,
    mime_type: str,
    account: MimoAccount,
    model: str = "mimo-v2-omni"
) -> Optional[Dict[str, Any]]:
    """
    上传媒体文件到小米Mimo服务器

    三步流程:
    1. genUploadInfo -> 获取上传签名URL
    2. PUT 上传二进制数据
    3. resource/parse -> 注册解析，获取资源ID

    Args:
        base64_data: base64编码的文件数据（不含data:前缀）
        mime_type: MIME类型 (image/jpeg, image/png, video/mp4等)
        account: Mimo账号凭证
        model: 模型名

    Returns:
        media对象 或 None (上传失败)
    """
    # 解析纯base64
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

    cookie = f"serviceToken={account.service_token}; userId={account.user_id}; xiaomichatbot_ph={account.xiaomichatbot_ph}"
    headers = {
        "Cookie": cookie,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://aistudio.xiaomimimo.com/",
        "Origin": "https://aistudio.xiaomimimo.com"
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            # Step 1: genUploadInfo
            ph = account.xiaomichatbot_ph
            info_res = await client.post(
                f"https://aistudio.xiaomimimo.com/open-apis/resource/genUploadInfo?xiaomichatbot_ph={ph}",
                json={"fileName": file_name, "fileContentMd5": md5},
                headers=headers
            )
            info_data = info_res.json()
            if info_data.get("code") != 0 or not info_data.get("data"):
                print(f"[uploadMedia] genUploadInfo failed: {info_data}")
                return None

            upload_url = info_data["data"]["uploadUrl"]
            resource_url = info_data["data"]["resourceUrl"]
            object_name = info_data["data"]["objectName"]

            # Step 2: PUT 上传二进制数据
            put_headers = {
                "Content-Type": "application/octet-stream",
                "content-md5": md5
            }
            put_res = await client.put(upload_url, content=binary_data, headers=put_headers)
            if put_res.status_code != 200:
                print(f"[uploadMedia] PUT failed: {put_res.status_code}")
                return None

            # Step 3: resource/parse
            parse_url = (
                f"https://aistudio.xiaomimimo.com/open-apis/resource/parse"
                f"?fileUrl={resource_url}"
                f"&objectName={object_name}"
                f"&model={model}"
                f"&xiaomichatbot_ph={ph}"
            )

            parse_res = None
            for attempt in range(5):
                try:
                    resp = await client.post(parse_url, json={}, headers={
                        "Cookie": cookie,
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                        "Referer": "https://aistudio.xiaomimimo.com/",
                        "Origin": "https://aistudio.xiaomimimo.com"
                    })
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


def build_query_from_messages(messages: list, tools: list = None,
                               max_messages: int = 6, max_content_len: int = 2000,
                               max_total_len: int = 8000) -> str:
    """
    从消息列表构建查询字符串

    Args:
        messages: 消息列表
        tools: 工具定义列表
        max_messages: 最大消息数量
        max_content_len: 单条消息最大长度
        max_total_len: query 总最大长度

    Returns:
        查询字符串
    """
    # 只保留最后N条消息
    if len(messages) > max_messages:
        messages = messages[-max_messages:]

    query_parts = []

    # 如果有工具定义，添加工具提示词
    if tools:
        from .tool_call import build_tool_prompt
        tool_prompt = build_tool_prompt(tools)
        query_parts.append(f"system: {tool_prompt}")

    for msg in messages:
        role = msg.role
        content = msg.content or ""

        # 处理多模态消息（content是数组）
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
            content = " ".join(text_parts)

        # 处理 tool_calls 消息 — 用 TOOL_CALL 格式序列化，与提示词一致
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            tc_lines = []
            for tc in msg.tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    fname = fn.get("name", "")
                    args_str = fn.get("arguments", "{}")
                    try:
                        import json as _json
                        args = _json.loads(args_str) if isinstance(args_str, str) else args_str
                        kv = ", ".join(f"{k}={v!r}" for k, v in args.items())
                    except Exception:
                        kv = str(args_str)
                    tc_lines.append(f"TOOL_CALL: {fname}({kv})")
                elif hasattr(tc, 'function'):
                    fn = tc.function
                    fname = getattr(fn, 'name', '')
                    args_str = getattr(fn, 'arguments', '{}')
                    try:
                        import json as _json
                        args = _json.loads(args_str) if isinstance(args_str, str) else args_str
                        kv = ", ".join(f"{k}={v!r}" for k, v in args.items())
                    except Exception:
                        kv = str(args_str)
                    tc_lines.append(f"TOOL_CALL: {fname}({kv})")
            content = "\n".join(tc_lines)

        # 处理 tool 返回结果 - 截断避免撑爆 query
        if role == "tool":
            tool_call_id = getattr(msg, 'tool_call_id', '')
            max_tool_content_len = 500
            if len(content) > max_tool_content_len:
                content = content[:max_tool_content_len] + "..."
            content = f"[tool_result id={tool_call_id[:8]}] {content}"

        # 截断过长的内容
        if len(content) > max_content_len:
            content = content[:max_content_len] + "..."
        query_parts.append(f"{role}: {content}")

    result = "\n".join(query_parts)

    # 总长度超限时，从前面截断（保留最后的消息）
    if len(result) > max_total_len:
        result = result[-max_total_len:]
        nl = result.find("\n")
        if nl > 0:
            result = result[nl+1:]

    return result

