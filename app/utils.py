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


def extract_medias_from_messages(messages: list) -> Tuple[str, list, list]:
    """从消息列表中提取图片/视频/音频媒体。"""
    base64_medias = []
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
        else:
            text = str(content) if content else ""

        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            text = _serialize_tool_calls(msg.tool_calls)

        if msg.role == "tool":
            tool_call_id = getattr(msg, 'tool_call_id', '')
            clean = re.sub(r'\[TOOL_RESULT\]\s*', '', text, flags=re.IGNORECASE)
            text = f"[tool_result id={tool_call_id[:8]}] {clean}"

        processed_messages.append({"role": msg.role, "text": text})

    query_text = processed_messages[-1]["text"] if processed_messages else ""
    return query_text, base64_medias, processed_messages


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


async def upload_media_to_mimo(
    base64_data: str,
    mime_type: str,
    account: MimoAccount,
    model: str = "mimo-v2-omni"
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

            put_headers = {"Content-Type": "application/octet-stream", "content-md5": md5}
            put_res = await client.put(upload_url, content=binary_data, headers=put_headers)
            if put_res.status_code != 200:
                print(f"[uploadMedia] PUT failed: {put_res.status_code}")
                return None

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


def _build_contextual_tool_prompt(tools: list, user_msg: str) -> str:
    """按需工具注入 — 基础工具始终注入 + 根据关键词追加 2-3 个相关工具。

    基础工具 (始终可用): terminal, search_files, read_file, send_message
    意图工具 (关键词匹配): web_search, write_file, patch, get_time 等

    避免注入全部工具导致 MiMo 模型幻觉，同时保证多步骤任务（查文件→发消息）可用。
    """
    if not tools or not user_msg:
        return ""

    from .tool_call import _safe_get

    # 白名单候选工具
    _ALLOWED = {
        "web_search", "web_extract", "terminal", "read_file",
        "write_file", "search_files", "patch",
        "get_time", "get_time_info", "get_weather", "calculator",
        "send_message",
    }

    # 基础工具 — 所有请求都注入
    _BASE_TOOLS = {"terminal", "search_files", "read_file", "send_message"}

    # 关键词 → 优先工具映射
    _KEYWORD_MAP = [
        (["文件", "目录", "ls", "列出", "查看文件", "读取", "打开文件",
          "有什么文件", "哪些文件", "file", "list", "当前目录"],
         ["search_files", "read_file", "terminal"]),
        (["搜索", "查找", "查询", "百度", "谷歌", "google", "search",
          "搜一下", "帮我查", "找一下"],
         ["web_search", "web_extract"]),
        (["执行", "运行", "命令", "bash", "shell", "终端", "脚本",
          "run", "cmd", "exec"],
         ["terminal"]),
        (["写入", "保存", "创建文件", "新建", "写文件", "write", "create",
          "生成文件"],
         ["write_file", "terminal"]),
        (["修改", "编辑", "改", "替换", "patch", "edit", "modify",
          "更新文件"],
         ["patch", "write_file", "read_file"]),
        (["时间", "几点", "日期", "time", "date", "clock",
          "现在几点", "北京时间"],
         ["get_time", "get_time_info"]),
        (["天气", "weather", "气温", "下雨"],
         ["get_weather", "web_search"]),
        (["计算", "算", "等于", "calculator", "calc", "math"],
         ["calculator"]),
        (["网页", "链接", "网站", "url", "打开网页", "提取内容",
          "extract", "fetch"],
         ["web_extract", "web_search"]),
        (["发", "发送", "消息", "微信", "通知", "回复", "send",
          "message", "msg"],
         ["send_message"]),
    ]

    # 匹配关键词
    matched_tools = set()
    msg_lower = user_msg.lower()

    for keywords, tool_names in _KEYWORD_MAP:
        if any(kw.lower() in msg_lower for kw in keywords):
            for tn in tool_names:
                if tn in _ALLOWED:
                    matched_tools.add(tn)

    # 合并基准工具 + 意图匹配工具
    all_tools = _BASE_TOOLS | matched_tools

    # 从原始 tools 中筛选出匹配的工具定义
    filtered = []
    seen = set()
    for tool in tools:
        func = _safe_get(tool, "function", default={})
        name = _safe_get(func, "name", default="")
        if name in all_tools and name not in seen:
            filtered.append(tool)
            seen.add(name)

    # 上限 6 个（基础 4 个 + 意图匹配 ~2 个）
    if len(filtered) > 6:
        filtered = filtered[:6]

    if not filtered:
        return ""

    # 用 build_tool_prompt 生成极简格式
    from .tool_call import build_tool_prompt
    prompt = build_tool_prompt(filtered, max_tools=6)
    # 多步骤提示
    if len(filtered) > 3:
        prompt += "如果有多个步骤，可以依次调用工具。"
    return prompt


def build_query_from_messages(
    messages: list,
    tools: list = None,
    max_messages: int = 6,
    max_content_len: int = 2000,
    max_total_len: int = 8000
) -> str:
    """从消息列表构建查询字符串。

    格式：用户消息在前（明确任务），工具信息在末尾（简短参考）。
    MiMo API 没有 system/user 角色分离，query 是纯文本拼接。
    系统消息不传给 MiMo（它是 Hermes 自己用的）。

    工具注入策略（v4.4）：
    - RikkaHub（无 system + tools≤5）→ 全部注入
    - Hermes（有 system + tools>5）→ 基础工具(4个) + 意图匹配(2个) = 最多6个
    """
    if len(messages) > max_messages:
        messages = messages[-max_messages:]

    # 检测请求类型
    has_system = any(
        hasattr(m, 'role') and m.role == "system" or
        (isinstance(m, dict) and m.get('role') == "system")
        for m in messages
    )
    is_hermes = has_system or (tools and len(tools) > 5)

    # 提取最后一条用户消息内容
    last_user_content = ""
    for m in reversed(messages):
        role = m.role if hasattr(m, 'role') else m.get('role', '')
        if role == "user":
            content = m.content if hasattr(m, 'content') else m.get('content', '')
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            last_user_content = str(content or "")
            break

    # Hermes 请求：基础工具(4个) + 意图匹配(2个) = 最多6个
    # 不给工具 → MiMo 只能纯文本回复 → 无法触发工具循环
    # RikkaHub 请求（少量工具 + 无 system）：注入极简工具提示（get_time 等已验证可用）
    tool_prompt_text = ""
    if tools:
        if is_hermes:
            # 意图匹配：从 20+ 工具中选 2-3 个最相关的
            tool_prompt_text = _build_contextual_tool_prompt(tools, last_user_content)
        else:
            from .tool_call import build_tool_prompt
            tool_prompt_text = build_tool_prompt(tools)

    query_parts = []

    for msg in messages:
        role = msg.role
        content = msg.content or ""

        if role == "system":
            continue

        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
            content = " ".join(text_parts)

        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            content = _serialize_tool_calls(msg.tool_calls)

        if role == "tool":
            tool_call_id = getattr(msg, 'tool_call_id', '')
            clean = re.sub(r'\[TOOL_RESULT\]\s*', '', content, flags=re.IGNORECASE)
            clean = clean.strip()
            if len(clean) > 500:
                clean = clean[:500] + "..."
            content = f"[tool_result id={tool_call_id[:8]}] {clean}"

        if len(content) > max_content_len:
            content = content[:max_content_len] + "..."
        query_parts.append(f"{role}: {content}")

    # 工具提示：放用户消息之后
    # 如果消息里已有工具结果，说明不是第一轮，不再注入完整工具提示词
    has_tool_result = any(
        (hasattr(m, 'role') and m.role == "tool") or
        (isinstance(m, dict) and m.get('role') == "tool")
        for m in messages
    )
    if tool_prompt_text and not has_tool_result:
        query_parts.append(f"\n{tool_prompt_text}")
    elif has_tool_result and tools:
        # 后续轮：只列工具名，不加激进指令，避免循环
        from .tool_call import get_tool_names as _get_tool_names
        names = _get_tool_names(tools)
        if len(names) > 6:
            names = names[:6]
        query_parts.append(f"\n可用工具: {', '.join(names)}")

    result = "\n".join(query_parts)

    # 总长度超限 — 从前面截断（保留后面的消息和工具提示词）
    if len(result) > max_total_len:
        result = result[-max_total_len:]
        nl = result.find("\n")
        if nl > 0:
            result = result[nl + 1:]

        if tool_prompt_text and tool_prompt_text not in result:
            result += f"\n\n{tool_prompt_text}"
        elif has_tool_result and "TOOL_CALL: name(args)" not in result:
            result += "\n需要时可用 TOOL_CALL: name(args) 继续调用工具"

    return result
