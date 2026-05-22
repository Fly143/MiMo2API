"""API路由 — MiMo2API

OpenAI 兼容接口 / 模型发现 / 管理后台 / 账号管理。
"""

import time
import uuid
import json
import asyncio
import re
import httpx
from typing import Optional, Tuple
from pathlib import Path
from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import StreamingResponse
from .models import (
    OpenAIRequest, OpenAIResponse, OpenAIChoice, OpenAIMessage,
    OpenAIDelta, OpenAIUsage, ParseCurlRequest, TestAccountRequest
)
from .config import config_manager, MimoAccount
from .mimo_client import MimoClient, MimoApiError
from .utils import parse_curl, build_query_from_messages, extract_medias_from_messages, upload_media_to_mimo, upload_text_file_to_mimo
from .tool_call import extract_tool_call, normalize_tool_call, get_tool_names, clean_tool_text  # build_tool_prompt unused
from .tool_sieve import StreamSieve
from .usage_store import add_usage as _add_usage, get_usage as _get_usage, clear_usage as _clear_usage
from .session_store import (
    get_or_create_session as _get_or_create_session,
    update_tokens as _update_session_tokens,
    update_fingerprint as _update_session_fingerprint,
    get_expired_sessions as _get_expired_sessions,
    remove_session as _remove_session,
)
from .response_store import (
    save_response_record as _save_response_record,
    get_response_record as _get_response_record,
    delete_response_record as _delete_response_record,
    update_response_record as _update_response_record,
)

router = APIRouter()

# ─── 常量 ─────────────────────────────────────────────────────

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

MODELS_CONFIG_URL = "https://aistudio.xiaomimimo.com/open-apis/bot/config"

# ─── 模型上下文参数 ───────────────────────────────────────────
# 官方数据：https://platform.xiaomimimo.com/static/docs/pricing.md

def _model_context(model_id: str) -> dict:
    """返回 (context_length, max_output_tokens) 或 (默认, 默认)。"""
    m = model_id.lower()
    # Pro / v2.5 系列 — 1M 上下文
    if any(prefix in m for prefix in ("v2.5-pro", "v2-pro", "v2.5")):
        return {"context_length": 1048576, "max_output_tokens": 131072}
    # Flash — 256K 上下文, 64K 输出
    if "v2-flash" in m or "v2-flash" in m:
        return {"context_length": 262144, "max_output_tokens": 65536}
    # Omni — 256K 上下文
    if "v2-omni" in m:
        return {"context_length": 262144, "max_output_tokens": 131072}
    # 未知模型 → 不返回上下文信息
    return None

_models_cache = None
_models_lock = asyncio.Lock()


# ─── API Key 验证 ─────────────────────────────────────────────

def validate_api_key(authorization: Optional[str]) -> bool:
    if not authorization:
        return False
    key = authorization.replace("Bearer ", "").strip()
    return config_manager.validate_api_key(key)


# ─── 动态模型发现 ─────────────────────────────────────────────

async def _do_discover() -> list:
    global _models_cache
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(MODELS_CONFIG_URL, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                print(f"[模型发现] config端点返回 {r.status_code}")
                return []
            data = r.json()
            model_list = data.get("data", {}).get("modelConfigList", [])
            models = [m["model"] for m in model_list if "model" in m]
    except Exception as e:
        print(f"[模型发现] 请求失败: {e}")
        return []

    async with _models_lock:
        _models_cache = models
    print(f"[模型发现] 找到 {len(models)} 个可用模型: {models}")
    return models


async def discover_models() -> list:
    if config_manager.config.models:
        return config_manager.config.models
    return await _do_discover()


def get_models_list() -> list:
    if config_manager.config.models:
        return config_manager.config.models
    if _models_cache is not None:
        return _models_cache
    return []


async def _background_refresh():
    try:
        await _do_discover()
    except Exception as e:
        print(f"[模型发现] 后台刷新失败: {e}")


@router.get("/v1/models")
async def list_models(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})
    asyncio.create_task(_background_refresh())
    models = get_models_list()
    ctx_items = [(m, _model_context(m)) for m in models]
    return {
        "object": "list",
        "data": [
            {
                "id": m, "object": "model", "created": 1681940951, "owned_by": "xiaomi",
                "context_length": ctx["context_length"],
                "context_window": ctx["context_length"],
                "max_input_tokens": ctx["context_length"],
                "max_output_tokens": ctx["max_output_tokens"],
            }
            for m, ctx in ctx_items if ctx is not None
        ]
    }


@router.post("/v1/models/refresh")
async def refresh_models(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})
    models = await discover_models()
    ctx_items = [(m, _model_context(m)) for m in models]
    return {
        "object": "list",
        "data": [
            {
                "id": m, "object": "model", "created": 1681940951, "owned_by": "xiaomi",
                "context_length": ctx["context_length"],
                "context_window": ctx["context_length"],
                "max_input_tokens": ctx["context_length"],
                "max_output_tokens": ctx["max_output_tokens"],
            }
            for m, ctx in ctx_items if ctx is not None
        ]
    }


@router.get("/v1/models/{model_id}")
async def get_model(
    model_id: str,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})
    models = get_models_list()
    if model_id in models:
        ctx = _model_context(model_id)
        base = {
            "id": model_id, "object": "model", "created": 1681940951, "owned_by": "xiaomi",
        }
        if ctx:
            base.update({
                "context_length": ctx["context_length"],
                "context_window": ctx["context_length"],
                "max_input_tokens": ctx["context_length"],
                "max_output_tokens": ctx["max_output_tokens"],
            })
        return base
    raise HTTPException(status_code=404, detail={"error": {"message": f"Model {model_id} not found"}})


# ─── 文本清洗辅助函数 ────────────────────────────────────────

def _strip_tool_result_blocks(text: str) -> str:
    """移除模型幻觉输出的 TOOL_RESULT 标签。

    模型看到上下文中 [TOOL_RESULT] 和 <tool_result> 格式后学会复述。
    移除所有已知格式。
    """
    if not text:
        return text
    cleaned = re.sub(r'\[TOOL_RESULT\]\s*', '', text, flags=re.IGNORECASE)
    cleaned = re.sub(r'\[/TOOL_RESULT\]\s*', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\[tool_result\s+id=\S+\]\s*', '', cleaned, flags=re.IGNORECASE)
    # XML 格式: <tool_result>...</tool_result>（模型学会的另一种格式）
    cleaned = re.sub(r'</?tool_result>\s*', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _strip_citations(text: str) -> str:
    """移除 MiMo 模型输出的引用标记，如 (citation:1)(citation:14)。"""
    if not text:
        return text
    return re.sub(r'\(citation:\d+\)\s*', '', text).strip()


def _camel_case(name: str) -> str:
    """snake_case -> camelCase: web_search -> webSearch"""
    parts = name.split('_')
    return parts[0] + ''.join(p.capitalize() for p in parts[1:])


def _strip_tool_name_prefix(text: str, tool_names: list) -> str:
    """去掉模型作为独立 SSE 事件输出的工具名（如 'webSearch'）。

    处理 snake_case 和 camelCase 变体，大小写不敏感。
    """
    if not text or not tool_names:
        return text
    variants = []
    for n in tool_names:
        variants.append(re.escape(n))
        if '_' in n:
            variants.append(re.escape(_camel_case(n)))
    escaped = '|'.join(variants)
    cleaned = re.sub(rf'^({escaped})\s*\n?', '', text.strip(), flags=re.IGNORECASE)
    return cleaned.strip()


def _strip_mimo_prefix(text: str) -> str:
    """通用 MiMo 原生前缀清理（含 IGNORECASE）。

    在 mimo_client 层已过滤 SSE 事件，此处做兜底。
    """
    if not text:
        return text
    prefixes = ['webSearch', 'getTimeInfo', 'getTime', 'sessionSearch',
                'imageSearch', 'fileSearch', 'getLocation', 'webExtract',
                'getWeather', 'calculator']
    escaped = '|'.join(re.escape(p) for p in prefixes)
    cleaned = re.sub(rf'^({escaped})\s*\n?', '', text.strip(), flags=re.IGNORECASE)
    return cleaned.strip()


def _clean_response_text(text: str, tool_names: list = None) -> str:
    """综合文本清理管道：TOOL_RESULT + 引用 + 工具前缀 + MiMo前缀 + 工具文本残留。"""
    text = _strip_tool_result_blocks(text)
    text = _strip_citations(text)
    if tool_names:
        text = _strip_tool_name_prefix(text, tool_names)
    text = _strip_mimo_prefix(text)
    text = clean_tool_text(text)
    return text


# ─── Think 标签处理 ──────────────────────────────────────────

def _safe_flush(text: str) -> Tuple[str, str]:
    """分割文本为 (安全发送, 保留在缓冲区)。

    仅保留可能是 <think> 或 </think> 部分标签的最长后缀。
    其余全部立即刷新，避免 silence gap 导致客户端进入缓冲模式。
    """
    last_lt = text.rfind('<')
    if last_lt == -1:
        return text, ""
    suffix = text[last_lt:]
    if THINK_OPEN.startswith(suffix) or THINK_CLOSE.startswith(suffix):
        return text[:last_lt], suffix
    return text, ""


def _split_think(text: str) -> Tuple[str, str]:
    """从文本中分离 think 块和正文。

    Returns: (main_content, think_content)
    """
    start = text.find(THINK_OPEN)
    if start == -1:
        return text, ""

    end = text.find(THINK_CLOSE, start)
    if end == -1:
        return text[:start].strip(), text[start + len(THINK_OPEN):]

    think_content = text[start + len(THINK_OPEN):end]
    main = text[:start] + text[end + len(THINK_CLOSE):]
    return main.strip(), think_content


# ─── 响应构建 ─────────────────────────────────────────────────

def _build_response(
    msg_id: str, model: str,
    content: str = None, tool_calls: list = None,
    finish_reason: str = "stop", usage: dict = None
) -> OpenAIResponse:
    """统一构建 OpenAI 非流式响应。"""
    message = OpenAIMessage(role="assistant", content=content, tool_calls=tool_calls)
    usage_obj = None
    if usage:
        usage_obj = OpenAIUsage(
            prompt_tokens=usage.get("promptTokens", 0),
            completion_tokens=usage.get("completionTokens", 0),
            total_tokens=usage.get("promptTokens", 0) + usage.get("completionTokens", 0)
        )
    return OpenAIResponse(
        id=msg_id, object="chat.completion",
        created=int(time.time()), model=model,
        choices=[OpenAIChoice(index=0, message=message, finish_reason=finish_reason)],
        usage=usage_obj or OpenAIUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    )


def _build_chunk(
    msg_id: str, model: str,
    content: str = None, reasoning: str = None,
    tool_calls: list = None, finish_reason: str = None,
    role: str = None, created: int = None
) -> str:
    """统一构建 SSE chunk 字符串。

    exclude_none=True 去除 null 字段，避免客户端因 message:null
    等非标准字段误判为非流式模式。
    reasoning 同时输出 reasoning 和 reasoning_content（RikkaHub 兼容）。
    """
    delta = OpenAIDelta(
        role=role, content=content,
        reasoning=reasoning, tool_calls=tool_calls
    )
    chunk = OpenAIResponse(
        id=msg_id, object="chat.completion.chunk",
        created=created if created is not None else int(time.time()),
        model=model,
        choices=[OpenAIChoice(index=0, delta=delta, finish_reason=finish_reason)]
    )
    data = chunk.dict(exclude_none=True)
    if reasoning:
        for choice in data.get('choices', []):
            d = choice.get('delta', {})
            if 'reasoning' in d:
                d['reasoning_content'] = reasoning
    return f"data: {json.dumps(data)}\n\n"


# ─── 聊天接口 ─────────────────────────────────────────────────

@router.post("/v1/chat/completions")
async def chat_completions(
    request: OpenAIRequest,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    """OpenAI兼容的聊天接口。"""

    # # 请求日志（发版时关闭）
    # try:
    #     print(f"[REQ] model={request.model} stream={request.stream} "
    #           f"tools={len(request.tools) if request.tools else 0} "
    #           f"tool_choice={request.tool_choice} reasoning_effort={request.reasoning_effort}")
    #     try:
    #         logf = Path.home() / 'mimo_requests.log'
    #         if logf.exists() and logf.stat().st_size > 5 * 1024 * 1024:
    #             logf.write_text('')
    #         with open(str(logf), 'a') as rf:
    #             import datetime as dt2
    #             full = request.model_dump(exclude_none=True)
    #             full['_timestamp'] = dt2.datetime.now().isoformat()
    #             rf.write(json.dumps(full, ensure_ascii=False) + '\n')
    #     except Exception:
    #         pass
    # except Exception:
    #     pass

    account = config_manager.get_next_account()
    if not account:
        raise HTTPException(status_code=503, detail={"error": {"message": "no mimo account"}})

    # 转换 tools 为字典列表
    tools_dict = [t.dict() if hasattr(t, 'dict') else t for t in request.tools] if request.tools else None

    # 提取媒体和文本文件
    query_text, base64_medias, text_files, processed_msgs = extract_medias_from_messages(request.messages)
    effective_model = request.model

    multi_medias = []
    if base64_medias:
        for media in base64_medias:
            media_obj = await upload_media_to_mimo(
                media["base64"], media["mimeType"], account, effective_model
            )
            if media_obj:
                multi_medias.append(media_obj)

    # 上传文本文件到 MiMo（同样走 multiMedias，mediaType="file"）
    if text_files:
        for tf in text_files:
            media_obj = await upload_text_file_to_mimo(
                tf["base64"], tf["filename"], tf["mimeType"], account, effective_model
            )
            if media_obj:
                multi_medias.append(media_obj)

    # 构建查询
    passthrough_mode = request.passthrough or config_manager.config.tools_passthrough
    query = build_query_from_messages(request.messages, tools=tools_dict, passthrough=passthrough_mode)

    thinking = bool(request.reasoning_effort)
    client = MimoClient(account)

    # 会话管理：通过消息指纹续接 MiMo conversationId
    conv_id, conv_is_new = _get_or_create_session(
        account.user_id, request.messages, request.model
    )
    # 立即用当前消息更新指纹（对新会话：设置初值；对已有会话：更新续接后的指纹）
    _update_session_fingerprint(account.user_id, conv_id, request.messages)

    # 流式响应
    if request.stream:
        return StreamingResponse(
            _stream_response(client, query, thinking, effective_model, tools_dict, multi_medias, passthrough=passthrough_mode,
                             conv_id=conv_id, account_id=account.user_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            }
        )

    # 非流式响应
    try:
        content, think_content, usage = await client.call_api(
            query, thinking, effective_model, multi_medias, conversation_id=conv_id)

        # 保存用量
        if usage:
            _add_usage(request.model, usage.get("promptTokens", 0), usage.get("completionTokens", 0))
            _update_session_tokens(account.user_id, conv_id, usage.get("promptTokens", 0))

        # 首次消息：记录真实指纹
        _update_session_fingerprint(account.user_id, conv_id, request.messages)

        # 清理模型输出杂质
        content = _strip_tool_result_blocks(content)
        content = _strip_citations(content)
        content = clean_tool_text(content)

        msg_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        # 提取工具调用
        tool_names = []
        tool_calls = None
        if tools_dict:
            tool_names = get_tool_names(tools_dict)
            result = extract_tool_call(content, tool_names)
            if result:
                if result[0]:
                    tool_calls = result[0]  # List[Dict]
                if result[1] is not None:
                    content = result[1]  # 使用清理后的文本（含 MiMoML 残留清理）

        # 清洗工具名前缀
        content = _strip_tool_name_prefix(content, tool_names)

        if tool_calls:
            return _build_response(
                msg_id, request.model,
                content=None, tool_calls=tool_calls,
                finish_reason="tool_calls", usage=usage
            )
        else:
            full_content = content
            if think_content:
                full_content = f"{THINK_OPEN}{think_content}{THINK_CLOSE}\n{content}"
            return _build_response(
                msg_id, request.model,
                content=full_content, finish_reason="stop", usage=usage
            )

    except MimoApiError as e:
        raise HTTPException(status_code=e.status_code, detail={"error": {"message": f"MiMo API: {e.response_body[:200]}"}})
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail={"error": {"message": str(e)}})


async def _stream_response(
    client: MimoClient, query: str, thinking: bool, model: str,
    tools: list = None, multi_medias: list = None,
    passthrough: bool = False,
    conv_id: str = None, account_id: str = None,
):
    """流式响应生成器。

    行为矩阵：
    | 场景              | reasoning  | content             |
    | 无 tools          | 流式       | 流式                |
    | 有 tools+无工具调用 | 流式      | 流式                |
    | 有 tools+有工具调用 | 流式      | 不发（发 tool_calls）|

    修复 v4.1：
    - 统一使用 full_content 进行工具调用提取（而非 content_buffer）
    - 捕获 httpx.ReadTimeout
    - 无工具调用时也清理工具名前缀
    """
    msg_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created_t = int(time.time())

    # 初始 role delta
    yield _build_chunk(msg_id, model, created=created_t, role="assistant")

    has_tools = tools is not None

    try:
        if has_tools:
            # ═══════════════════════════════════════════════════
            # 有工具定义：reasoning 流式，正文通过筛分流式
            # sieve 实时分离 TOOL_CALL 文本与普通正文
            # ═══════════════════════════════════════════════════
            tool_names = get_tool_names(tools)
            sieve = StreamSieve(
                mode='tool_call',
                parse_fn=lambda text: extract_tool_call(text, tool_names),
            )
            collected_tool_calls = []
            content_buffer_chunks = []  # 收集 content（工具调用时丢弃）
            in_think = False
            buffer = ""
            last_usage = None

            async for sse_data in client.stream_api(query, thinking, model, multi_medias):
                # 用量事件
                if sse_data.get("type") == "usage":
                    last_usage = sse_data
                    continue
                chunk = sse_data.get("content", "")
                if not chunk:
                    continue

                buffer += chunk.replace("\x00", "")

                # 处理 think 标签
                while True:
                    if not in_think:
                        idx = buffer.find(THINK_OPEN)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                # Feed through sieve — stream text, collect tool calls
                                for ev in sieve.feed(safe):
                                    if ev.type == 'text':
                                        clean = _clean_response_text(ev.data, tool_names)
                                        if clean:
                                            content_buffer_chunks.append(clean)
                                            yield _build_chunk(msg_id, model, created=created_t, content=clean)
                                    elif ev.type == 'tool_calls':
                                        collected_tool_calls.extend(ev.data)
                            in_think = True
                            buffer = buffer[idx + len(THINK_OPEN):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            for ev in sieve.feed(safe):
                                if ev.type == 'text':
                                    clean = _clean_response_text(ev.data, tool_names)
                                    if clean:
                                        content_buffer_chunks.append(clean)
                                        yield _build_chunk(msg_id, model, created=created_t, content=clean)
                                elif ev.type == 'tool_calls':
                                    collected_tool_calls.extend(ev.data)
                        buffer = keep
                        break
                    else:
                        idx = buffer.find(THINK_CLOSE)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                yield _build_chunk(msg_id, model, created=created_t, reasoning=safe)
                            in_think = False
                            buffer = buffer[idx + len(THINK_CLOSE):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            yield _build_chunk(msg_id, model, created=created_t, reasoning=safe)
                        buffer = keep
                        break

            # 正文留在 buffer 中的追加到 sieve
            if buffer and not in_think:
                for ev in sieve.feed(buffer):
                    if ev.type == 'text':
                        clean = _clean_response_text(ev.data, tool_names)
                        if clean:
                            content_buffer_chunks.append(clean)
                            yield _build_chunk(msg_id, model, created=created_t, content=clean)
                    elif ev.type == 'tool_calls':
                        collected_tool_calls.extend(ev.data)

            # 刷新 sieve，回收最终残留 text/tool_calls
            for ev in sieve.flush():
                if ev.type == 'text':
                    clean = _clean_response_text(ev.data, tool_names)
                    if clean:
                        content_buffer_chunks.append(clean)
                        yield _build_chunk(msg_id, model, created=created_t, content=clean)
                elif ev.type == 'tool_calls':
                    collected_tool_calls.extend(ev.data)

            if collected_tool_calls:
                # 有工具调用 → 不发 content，只发 tool_calls
                streaming_tc = [{**tc, "index": 0} for tc in collected_tool_calls]
                yield _build_chunk(msg_id, model, created=created_t,
                                   tool_calls=streaming_tc, finish_reason="tool_calls")
                yield "data: [DONE]\n\n"
                if last_usage:
                    _add_usage(model, last_usage.get("promptTokens", 0), last_usage.get("completionTokens", 0))
                    _update_session_tokens(account_id, conv_id, last_usage.get("promptTokens", 0))
                return

            # 无工具调用：content 已在流中发出，只需发 stop
            yield _build_chunk(msg_id, model, created=created_t, finish_reason="stop")
            yield "data: [DONE]\n\n"
            if last_usage:
                _add_usage(model, last_usage.get("promptTokens", 0), last_usage.get("completionTokens", 0))
                _update_session_tokens(account_id, conv_id, last_usage.get("promptTokens", 0))

        else:
            # ═══════════════════════════════════════════════════
            # 无工具定义：实时流式输出
            # ═══════════════════════════════════════════════════
            buffer = ""
            in_think = False
            last_usage = None

            async for sse_data in client.stream_api(query, thinking, model, multi_medias):
                if sse_data.get("type") == "usage":
                    last_usage = sse_data
                    continue
                chunk = sse_data.get("content", "")
                if not chunk:
                    continue

                buffer += chunk.replace("\x00", "")

                while True:
                    if not in_think:
                        idx = buffer.find(THINK_OPEN)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                clean = _clean_response_text(safe)
                                if clean:
                                    yield _build_chunk(msg_id, model, created=created_t, content=clean)
                            in_think = True
                            buffer = buffer[idx + len(THINK_OPEN):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            clean = _clean_response_text(safe)
                            if clean:
                                yield _build_chunk(msg_id, model, created=created_t, content=clean)
                        buffer = keep
                        break
                    else:
                        idx = buffer.find(THINK_CLOSE)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                yield _build_chunk(msg_id, model, created=created_t, reasoning=safe)
                            in_think = False
                            buffer = buffer[idx + len(THINK_CLOSE):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            yield _build_chunk(msg_id, model, created=created_t, reasoning=safe)
                        buffer = keep
                        break

            # 发送剩余内容
            if buffer:
                clean = _clean_response_text(buffer)
                if clean:
                    if in_think:
                        yield _build_chunk(msg_id, model, created=created_t, reasoning=clean)
                    else:
                        yield _build_chunk(msg_id, model, created=created_t, content=clean)

            yield _build_chunk(msg_id, model, created=created_t, finish_reason="stop")
            yield "data: [DONE]\n\n"
            if last_usage:
                _add_usage(model, last_usage.get("promptTokens", 0), last_usage.get("completionTokens", 0))
                _update_session_tokens(account_id, conv_id, last_usage.get("promptTokens", 0))

    except httpx.ReadTimeout:
        # 连接读取超时 — 发送优雅结束
        yield _build_chunk(msg_id, model, created=created_t, finish_reason="length")
        yield "data: [DONE]\n\n"
    except MimoApiError as e:
        error_data = {"error": {"message": f"MiMo API {e.status_code}: {e.response_body[:200]}",
                                "type": "upstream_error", "code": e.status_code}}
        yield f"data: {json.dumps(error_data)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        # import traceback
        # tb = traceback.format_exc()
        # log_path = Path(__file__).parent.parent / "error.log"
        # if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
        #     log_path.write_text('')
        # with open(log_path, "a") as f:
        #     f.write(f"=== STREAM ERROR ===\n{tb}\n\n")
        yield f"data: {json.dumps({'error': {'message': str(e)}})}\n\n"
        yield "data: [DONE]\n\n"


# ─── 管理页面 ─────────────────────────────────────────────────

from pathlib import Path as _Path
_ADMIN_HTML = (_Path(__file__).parent.parent / "web" / "index.html").read_text(encoding="utf-8")


@router.get("/admin")
@router.get("/")
async def admin_page():
    from starlette.responses import HTMLResponse
    return HTMLResponse(_ADMIN_HTML)


# ─── 账号管理 API ─────────────────────────────────────────────

import re as _re
from datetime import datetime as _dt


@router.get("/api/accounts")
async def list_accounts():
    accounts = []
    for acc in config_manager.config.mimo_accounts:
        token = acc.service_token
        masked = token[:16] + "..." + token[-6:] if len(token) > 22 else "***"
        accounts.append({
            "user_id": acc.user_id,
            "token_masked": masked,
            "is_valid": acc.is_valid,
            "login_time": acc.login_time,
            "last_test": acc.last_test,
        })
    return {"accounts": accounts}


@router.post("/api/account/import-cookie")
async def import_cookie(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "invalid json")

    st = (data.get("serviceToken") or "").strip()
    uid = (data.get("userId") or "").strip()
    ph = (data.get("xiaomichatbot_ph") or "").strip()

    if not st or not uid or not ph:
        return {"ok": False, "error": "缺少必要字段 (serviceToken, userId, xiaomichatbot_ph)"}

    return await _validate_and_save(st, uid, ph)


@router.post("/api/account/import-curl")
async def import_curl(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "invalid json")

    curl = (data.get("curl") or "").strip()
    if not curl:
        return {"ok": False, "error": "请提供 cURL 命令"}

    cookie_match = _re.search(r"(?:-b|--cookie)\s+'([^']+)'", curl)
    if not cookie_match:
        cookie_match = _re.search(r"-H\s+'Cookie:\s*([^']+)'", curl)
    if not cookie_match:
        return {"ok": False, "error": "未从 cURL 中找到 Cookie"}

    cookies = cookie_match.group(1)
    st_m = _re.search(r'serviceToken="?([^";\s]+)', cookies)
    uid_m = _re.search(r'userId=(\d+)', cookies)
    ph_m = _re.search(r'xiaomichatbot_ph="?([^";\s]+)', cookies)

    if not st_m or not uid_m or not ph_m:
        return {"ok": False, "error": "未从 Cookie 中提取到 serviceToken/userId/xiaomichatbot_ph"}

    return await _validate_and_save(st_m.group(1), uid_m.group(1), ph_m.group(1))


async def _validate_and_save(service_token: str, user_id: str, xiaomichatbot_ph: str):
    from .mimo_client import MimoClient, MimoApiError

    account = MimoAccount(service_token=service_token, user_id=user_id, xiaomichatbot_ph=xiaomichatbot_ph)
    client = MimoClient(account)

    try:
        content, _, _ = await client.call_api("hi", False)
        now = _dt.now().strftime("%m-%d %H:%M")

        existing = False
        for i, acc in enumerate(config_manager.config.mimo_accounts):
            if acc.user_id == user_id:
                config_manager.config.mimo_accounts[i] = MimoAccount(
                    service_token=service_token, user_id=user_id,
                    xiaomichatbot_ph=xiaomichatbot_ph,
                    login_time=now, is_valid=True,
                )
                existing = True
                break
        if not existing:
            config_manager.config.mimo_accounts.append(MimoAccount(
                service_token=service_token, user_id=user_id,
                xiaomichatbot_ph=xiaomichatbot_ph,
                login_time=now, is_valid=True,
            ))
        config_manager.save()
        return {"ok": True, "user_id": user_id, "response": content[:100]}

    except MimoApiError as e:
        return {"ok": False, "error": f"验证失败 (HTTP {e.status_code}): {e.response_body[:100]}"}
    except Exception as e:
        return {"ok": False, "error": f"验证失败: {str(e)[:100]}"}


@router.delete("/api/accounts/{idx}")
async def delete_account(idx: int):
    accounts = config_manager.config.mimo_accounts
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "account not found")
    removed = accounts.pop(idx)
    config_manager.save()
    return {"ok": True, "removed_user_id": removed.user_id}


@router.post("/api/accounts/{idx}/test")
async def test_account(idx: int):
    accounts = config_manager.config.mimo_accounts
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "account not found")

    from .mimo_client import MimoClient, MimoApiError
    acc = accounts[idx]
    client = MimoClient(acc)

    try:
        content, _, _ = await client.call_api("hi", False)
        acc.is_valid = True
        acc.last_test = _dt.now().strftime("%m-%d %H:%M")
        config_manager.save()
        return {"ok": True, "response": content[:200]}
    except MimoApiError as e:
        acc.is_valid = False
        acc.last_test = _dt.now().strftime("%m-%d %H:%M")
        config_manager.save()
        return {"ok": False, "error": f"HTTP {e.status_code}: {e.response_body[:100]}"}
    except Exception as e:
        acc.is_valid = False
        config_manager.save()
        return {"ok": False, "error": str(e)[:200]}


# ─── 旧版管理接口（保留兼容） ────────────────────────────────

@router.get("/api/config")
async def get_config():
    return config_manager.get_config()


@router.post("/api/config")
async def update_config(request: Request):
    try:
        new_config = await request.json()
        config_manager.update_config(new_config)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": "invalid"})


@router.post("/api/parse-curl")
async def parse_curl_command(request: ParseCurlRequest):
    account = parse_curl(request.curl)
    if not account:
        raise HTTPException(status_code=400, detail={"error": "parse failed"})
    return account.to_dict()


@router.post("/api/test-account")
async def test_account_endpoint(request: TestAccountRequest):
    try:
        account = MimoAccount(
            service_token=request.service_token,
            user_id=request.user_id,
            xiaomichatbot_ph=request.xiaomichatbot_ph
        )
        client = MimoClient(account)
        content, _, _ = await client.call_api("hi", False)
        return {"success": True, "response": content}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─── 用量统计 API ─────────────────────────────────────────────

@router.get("/api/usage")
async def usage_stats():
    """返回用量统计：按模型分组 + 全部汇总。"""
    return _get_usage()


@router.delete("/api/usage")
async def clear_usage():
    """清空全部用量统计数据。"""
    _clear_usage()
    return {"ok": True}


@router.post("/api/cleanup")
async def manual_cleanup():
    """手动触发过期会话清理。"""
    try:
        expired = _get_expired_sessions()
        if not expired:
            return {"ok": True, "msg": "没有过期会话", "deleted": 0}

        print(f"[Cleanup] Found {len(expired)} expired sessions, deleting...")
        deleted = 0
        # 按账号分组
        by_account = {}
        for account_label, conv_id, model, days_ago in expired:
            by_account.setdefault(account_label, []).append(conv_id)

        for account_label, conv_ids in by_account.items():
            # 找到对应账号
            acc = None
            for a in config_manager.config.mimo_accounts:
                if a.user_id == account_label:
                    acc = a
                    break
            if not acc:
                continue

            client = MimoClient(acc)
            for conv_id in conv_ids:
                if await client.delete_conversations([conv_id]):
                    _remove_session(account_label, conv_id)
                    deleted += 1
                    print(f"[Cleanup] Deleted: {conv_id[:12]}...")

        print(f"[Cleanup] Done: {deleted}/{len(expired)} deleted")
        return {"ok": True, "msg": f"清理完成: {deleted}/{len(expired)}", "deleted": deleted}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


# ─── 模型列表（免鉴权，供管理页面使用） ───────────────────────

@router.get("/api/models")
async def admin_models():
    """返回可用模型列表（无鉴权，仅供管理页面动态加载）。"""
    return {"models": get_models_list()}
# ═══════════════════════════════════════════════════════════════
# OpenAI Responses API 兼容层
# 追加到 admin_models() 函数之后
# ═══════════════════════════════════════════════════════════════

# ─── 常量 ────────────────────────────────────────────────────

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

_RESPONSE_TERMINAL_STATUSES = {"completed", "failed", "incomplete", "cancelled"}

# ─── 辅助函数 ─────────────────────────────────────────────────

def _gen_response_id() -> str:
    """生成响应ID：resp_ + uuid4 hex[:32]"""
    return f"resp_{uuid.uuid4().hex[:32]}"


def _response_text_config(body: dict) -> dict:
    """从 body 中提取 text.format 配置。"""
    text = body.get("text")
    if isinstance(text, dict):
        cfg = dict(text)
        fmt = cfg.get("format")
        if isinstance(fmt, dict):
            cfg["format"] = dict(fmt)
        elif isinstance(fmt, str):
            cfg["format"] = {"type": fmt}
        else:
            cfg["format"] = {"type": "text"}
        return cfg
    return {"format": {"type": "text"}}


def _json_schema_from_text_config(text_config: dict | None) -> dict | None:
    """从 text_config 中提取 JSON Schema 字典。"""
    fmt = text_config.get("format") if isinstance(text_config, dict) else None
    if not isinstance(fmt, dict) or fmt.get("type") != "json_schema":
        return None
    schema = fmt.get("schema")
    if isinstance(schema, dict):
        return schema
    json_schema = fmt.get("json_schema")
    if isinstance(json_schema, dict):
        nested = json_schema.get("schema")
        return nested if isinstance(nested, dict) else json_schema
    return None


def _response_text_item(text: str, item_id: str | None = None) -> dict:
    """构建 OpenAI Responses output_text item。"""
    return {
        "id": item_id or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{
            "type": "output_text",
            "text": text or "",
            "annotations": [],
        }],
    }


def _response_reasoning_item(summary_text: str, item_id: str | None = None) -> dict:
    """构建 reasoning summary item。"""
    return {
        "id": item_id or f"rs_{uuid.uuid4().hex[:24]}",
        "type": "reasoning",
        "summary": [{
            "type": "summary_text",
            "text": summary_text or "",
        }],
    }


def _response_function_call_item(tool_call: dict, call_id: str | None = None) -> dict:
    """从 tool_call 构建 function_call item。"""
    fn = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
    return {
        "id": f"fc_{uuid.uuid4().hex[:24]}",
        "type": "function_call",
        "call_id": call_id or tool_call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
        "name": fn.get("name", ""),
        "arguments": fn.get("arguments", "{}"),
        "status": "completed",
    }


def _make_response_object(
    response_id: str, model: str, status: str,
    items: list, usage: dict | None,
    incomplete_details: dict | None = None,
) -> dict:
    """构建完整的 Response 对象字典。"""
    text_cfg = {"format": {"type": "text"}}
    output_text = ""
    for item in items or []:
        if item.get("type") == "message":
            for part in item.get("content", []) or []:
                if part.get("type") == "output_text":
                    output_text = part.get("text", "") or ""
    usage_obj = None
    if usage:
        usage_obj = {
            "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": int(usage.get("completion_tokens", 0) or 0),
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "error": None,
        "incomplete_details": incomplete_details,
        "instructions": None,
        "max_output_tokens": None,
        "model": model,
        "output": items or [],
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {},
        "store": True,
        "temperature": None,
        "text": text_cfg,
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": usage_obj,
        "user": None,
        "metadata": {},
        "output_text": output_text,
    }


def _convert_response_input_to_messages(input_data) -> list[dict]:
    """将 Responses API input 格式转换为 OpenAI Chat 消息列表。"""
    messages: list[dict] = []
    if input_data is None:
        return messages
    items = input_data if isinstance(input_data, list) else [input_data]
    for item in items:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        role = item.get("role")

        if role in ("system", "user", "assistant", "tool"):
            content = item.get("content", "")
            if isinstance(content, list):
                parts = []
                assistant_tool_calls = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype in ("input_text", "output_text", "text"):
                        parts.append({"type": "text", "text": part.get("text", "")})
                    elif ptype == "input_image":
                        image_url = part.get("image_url") or part.get("url") or ""
                        if image_url:
                            parts.append({"type": "image_url", "image_url": {"url": image_url}})
                    elif ptype == "input_file":
                        parts.append({
                            "type": "file",
                            "file": {
                                "filename": part.get("filename") or "file.txt",
                                "file_data": part.get("file_data") or part.get("data") or "",
                            }
                        })
                    elif ptype == "function_call" and role == "assistant":
                        assistant_tool_calls.append({
                            "id": part.get("call_id") or part.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function": {
                                "name": part.get("name", ""),
                                "arguments": part.get("arguments", "{}") if isinstance(part.get("arguments", "{}"), str)
                                else json.dumps(part.get("arguments", {}), ensure_ascii=False),
                            }
                        })
                msg = {"role": role, "content": parts if parts else ""}
                if assistant_tool_calls:
                    msg["tool_calls"] = assistant_tool_calls
                    if not parts:
                        msg["content"] = None
                messages.append(msg)
            else:
                msg = {"role": role, "content": content}
                if role == "assistant" and item_type == "function_call":
                    msg["tool_calls"] = [{
                        "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": item.get("arguments", "{}") if isinstance(item.get("arguments", "{}"), str)
                            else json.dumps(item.get("arguments", {}), ensure_ascii=False),
                        }
                    }]
                    if content in ("", None):
                        msg["content"] = None
                messages.append(msg)
            continue

        if item_type == "message":
            content = item.get("content", [])
            role = item.get("role", "user")
            parts = []
            assistant_tool_calls = []
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype in ("input_text", "output_text", "text"):
                        parts.append({"type": "text", "text": part.get("text", "")})
                    elif ptype == "input_image":
                        image_url = part.get("image_url") or part.get("url") or ""
                        if image_url:
                            parts.append({"type": "image_url", "image_url": {"url": image_url}})
                    elif ptype == "input_file":
                        parts.append({
                            "type": "file",
                            "file": {
                                "filename": part.get("filename") or "file.txt",
                                "file_data": part.get("file_data") or part.get("data") or "",
                            }
                        })
                    elif ptype == "function_call" and role == "assistant":
                        assistant_tool_calls.append({
                            "id": part.get("call_id") or part.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function": {
                                "name": part.get("name", ""),
                                "arguments": part.get("arguments", "{}") if isinstance(part.get("arguments", "{}"), str)
                                else json.dumps(part.get("arguments", {}), ensure_ascii=False),
                            }
                        })
            msg = {"role": role, "content": parts if parts else ""}
            if assistant_tool_calls:
                msg["tool_calls"] = assistant_tool_calls
                if not parts:
                    msg["content"] = None
            messages.append(msg)
            continue

        if item_type == "function_call_output":
            output = item.get("output", "")
            if isinstance(output, dict):
                output = json.dumps(output, ensure_ascii=False)
            elif output is None:
                output = ""
            tool_message = {"role": "tool", "content": str(output)}
            if item.get("call_id"):
                tool_message["tool_call_id"] = item.get("call_id")
            messages.append(tool_message)
            continue

        if item_type in ("input_text", "text"):
            messages.append({"role": "user", "content": item.get("text", "")})

    return messages


def _normalize_structured_output_text(output_text: str, text_config: dict | None) -> str:
    """对 structured output (json_object/json_schema) 规范化输出文本。"""
    if not output_text or not isinstance(text_config, dict):
        return output_text
    fmt = text_config.get("format")
    if not isinstance(fmt, dict):
        return output_text
    fmt_type = fmt.get("type")
    if fmt_type not in ("json_object", "json_schema"):
        return output_text

    # 尝试从文本中提取 JSON
    candidate = output_text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
    start_candidates = [i for i in (candidate.find("{"), candidate.find("[")) if i != -1]
    if start_candidates:
        start = min(start_candidates)
        end_candidates = [candidate.rfind("}"), candidate.rfind("]")]
        end = max(end_candidates)
        if end > start:
            candidate = candidate[start:end + 1]
    try:
        parsed = json.loads(candidate)
        return json.dumps(parsed, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError, TypeError):
        return output_text


# ─── Token 计数辅助 ──────────────────────────────────────────

def _count_response_input_tokens(
    input_value,
    instructions: str | None = None,
    tools: list[dict] | None = None,
) -> int:
    """估算 input tokens：字符数 / 4 的简单启发式方法。"""
    total_chars = 0
    if isinstance(input_value, list):
        serialized = json.dumps(input_value, ensure_ascii=False)
        total_chars += len(serialized)
    elif isinstance(input_value, str):
        total_chars += len(input_value)
    if instructions:
        total_chars += len(instructions)
    if tools:
        total_chars += len(json.dumps(tools, ensure_ascii=False))
    return max(1, total_chars // 4)


def _count_tokens(text: str) -> int:
    """简单 token 估算：字符数 / 4。"""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _build_response_usage(usage: dict | None) -> dict:
    """将 MiMo usage 转换为 Responses API usage 格式。"""
    usage = usage or {}
    input_tokens = int(usage.get("promptTokens", 0) or usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completionTokens", 0) or usage.get("completion_tokens", 0) or 0)
    total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": total_tokens,
    }


def _extract_output_text(output: list[dict]) -> str:
    """从 output items 列表中提取文本内容。"""
    texts: list[str] = []
    for item in output or []:
        if item.get("type") == "message":
            for content in item.get("content", []) or []:
                if content.get("type") == "output_text":
                    texts.append(content.get("text", "") or "")
    return "".join(texts)


def _response_output_from_message(msg: dict) -> list[dict]:
    """从 OpenAI chat message 构建 Responses output items。"""
    output: list[dict] = []
    reasoning = msg.get("reasoning_content", "")
    if reasoning:
        output.append(_response_reasoning_item(reasoning))
    content = msg.get("content", "")
    if isinstance(content, str) and content:
        output.append(_response_text_item(content))
    tool_calls = msg.get("tool_calls") or []
    for tc in tool_calls:
        output.append(_response_function_call_item(tc))
    if not output:
        output.append(_response_text_item(""))
    return output


def _response_status_from_finish_reason(finish_reason: str) -> str:
    if finish_reason in ("stop", "tool_calls"):
        return "completed"
    if finish_reason in ("length", "content_filter"):
        return "incomplete"
    return "completed"


def _sse_json(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ─── 核心函数：非流式 ────────────────────────────────────────

async def _do_response_chat(body: dict, account) -> tuple:
    """非流式 Responses API 聊天。

    Returns:
        (model_used, usage_dict, items_list)
    """
    model = body.get("model", "default")
    input_data = body.get("input", [])
    instructions = body.get("instructions")
    tools = body.get("tools")
    text_config = _response_text_config(body)

    # 转换 input 为消息列表
    messages = _convert_response_input_to_messages(input_data)

    # 处理 instructions：作为 system 消息前置
    if isinstance(instructions, str) and instructions.strip():
        messages.insert(0, {"role": "system", "content": instructions.strip()})

    # structured output 处理
    structured_format = text_config.get("format", {}).get("type")
    if structured_format in ("json_object", "json_schema"):
        has_system = any(m.get("role") == "system" for m in messages)
        if structured_format == "json_schema":
            schema = _json_schema_from_text_config(text_config)
            instruction_text = "Please respond with valid JSON matching this schema."
        else:
            schema = None
            instruction_text = "Please respond with valid JSON object."
        sys_instruction = {"role": "system", "content": instruction_text}
        if has_system:
            # 追加到最后一条 system 消息之后
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "system":
                    messages.insert(i + 1, sys_instruction)
                    break
        else:
            messages.insert(0, sys_instruction)

    # 转换 dict 消息为 OpenAIMessage 对象
    openai_messages = []
    for m in messages:
        openai_messages.append(OpenAIMessage(
            role=m.get("role", "user"),
            content=m.get("content"),
            tool_calls=m.get("tool_calls"),
            tool_call_id=m.get("tool_call_id"),
        ))

    # 提取媒体
    query_text, base64_medias, text_files, processed_msgs = extract_medias_from_messages(openai_messages)
    effective_model = model

    multi_medias = []
    if base64_medias:
        for media in base64_medias:
            media_obj = await upload_media_to_mimo(
                media["base64"], media["mimeType"], account, effective_model
            )
            if media_obj:
                multi_medias.append(media_obj)
    if text_files:
        for tf in text_files:
            media_obj = await upload_text_file_to_mimo(
                tf["base64"], tf["filename"], tf["mimeType"], account, effective_model
            )
            if media_obj:
                multi_medias.append(media_obj)

    # 构建 tools dict
    tools_dict = [dict(t) if hasattr(t, 'dict') else t for t in tools] if tools else None

    # 构建查询
    query = build_query_from_messages(openai_messages, tools=tools_dict)

    thinking = False

    # 调用 MimoClient
    client = MimoClient(account)
    try:
        content, think_content, usage = await client.call_api(
            query, thinking, effective_model, multi_medias
        )
    except MimoApiError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"error": {"message": f"MiMo API: {e.response_body[:200]}"}}
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail={"error": {"message": str(e)}})

    # 清理输出
    content = _strip_tool_result_blocks(content)
    content = _strip_citations(content)
    content = clean_tool_text(content)

    # 额外处理：模型可能输出多个 think 块，_parse_think_tags 只剥除了第一个
    remaining_thinks = []
    cleaned_content = re.sub(
        r'<think>(.*?)</think>',
        lambda m: remaining_thinks.append(m.group(1).strip()) or '',
        content,
        flags=re.DOTALL
    )
    # 过滤空 think 块（模型可能输出 <think></think>）
    remaining_thinks = [t for t in remaining_thinks if t]
    if remaining_thinks:
        content = cleaned_content.strip()
        extra_think = '\n'.join(remaining_thinks)
        think_content = (think_content + '\n' + extra_think).strip() if think_content else extra_think

    # 构建 items
    items = []
    has_thinking = False
    if think_content:
        items.append(_response_reasoning_item(think_content))
        has_thinking = True

    # 工具调用提取
    tool_names = []
    tool_calls = None
    if tools_dict:
        tool_names = get_tool_names(tools_dict)
        result = extract_tool_call(content, tool_names)
        if result:
            if result[0]:
                tool_calls = result[0]
            if result[1] is not None:
                content = result[1]  # 使用清理后的文本（含 MiMoML 残留清理）

    content = _strip_tool_name_prefix(content, tool_names)

    if tool_calls:
        for tc in tool_calls:
            items.append(_response_function_call_item(tc))
    elif content:
        normalized = _normalize_structured_output_text(content, text_config)
        items.append(_response_text_item(normalized))

    if not items:
        items.append(_response_text_item(""))

    return effective_model, usage, items


# ─── 核心函数：流式 ──────────────────────────────────────────

async def _stream_response_events(body: dict, account):
    """流式 Responses API 事件生成器。

    Yields:
        event dicts (response.created, response.output_text.delta, etc.)
    """
    model = body.get("model", "default")
    input_data = body.get("input", [])
    instructions = body.get("instructions")
    tools = body.get("tools")
    text_config = _response_text_config(body)

    # 转换 input 为消息列表（同非流式）
    messages = _convert_response_input_to_messages(input_data)
    if isinstance(instructions, str) and instructions.strip():
        messages.insert(0, {"role": "system", "content": instructions.strip()})

    structured_format = text_config.get("format", {}).get("type")
    if structured_format in ("json_object", "json_schema"):
        has_system = any(m.get("role") == "system" for m in messages)
        if structured_format == "json_schema":
            instruction_text = "Please respond with valid JSON matching this schema."
        else:
            instruction_text = "Please respond with valid JSON object."
        sys_instruction = {"role": "system", "content": instruction_text}
        if has_system:
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "system":
                    messages.insert(i + 1, sys_instruction)
                    break
        else:
            messages.insert(0, sys_instruction)

    openai_messages = []
    for m in messages:
        openai_messages.append(OpenAIMessage(
            role=m.get("role", "user"),
            content=m.get("content"),
            tool_calls=m.get("tool_calls"),
            tool_call_id=m.get("tool_call_id"),
        ))

    query_text, base64_medias, text_files, processed_msgs = extract_medias_from_messages(openai_messages)
    effective_model = model

    multi_medias = []
    if base64_medias:
        for media in base64_medias:
            media_obj = await upload_media_to_mimo(
                media["base64"], media["mimeType"], account, effective_model
            )
            if media_obj:
                multi_medias.append(media_obj)
    if text_files:
        for tf in text_files:
            media_obj = await upload_text_file_to_mimo(
                tf["base64"], tf["filename"], tf["mimeType"], account, effective_model
            )
            if media_obj:
                multi_medias.append(media_obj)

    tools_dict = [dict(t) if hasattr(t, 'dict') else t for t in tools] if tools else None
    query = build_query_from_messages(openai_messages, tools=tools_dict)
    thinking = False

    response_id = body.get("_response_id") or _gen_response_id()
    created_t = int(time.time())

    # 初始事件
    init_payload = {
        "id": response_id,
        "object": "response",
        "created_at": created_t,
        "status": "in_progress",
        "model": effective_model,
        "output": [],
        "usage": None,
    }
    yield {"type": "response.created", "response": dict(init_payload)}
    yield {"type": "response.in_progress", "response": dict(init_payload)}

    reasoning_parts: list[str] = []
    text_parts: list[str] = []
    reasoning_item_id = f"rs_{uuid.uuid4().hex[:24]}"
    message_item_id = f"msg_{uuid.uuid4().hex[:24]}"
    output_started: set[str] = set()
    output_indices: dict[str, int] = {}
    content_started = False
    tool_calls_map: dict[int, dict] = {}

    def _start_output_item(item: dict) -> tuple[int, dict | None]:
        item_id = item.get("id") or f"out_{len(output_indices)}"
        if item_id not in output_indices:
            output_indices[item_id] = len(output_indices)
        output_index = output_indices[item_id]
        if item_id not in output_started:
            output_started.add(item_id)
            return_event = {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": item,
            }
            return output_index, return_event
        return output_index, None

    client = MimoClient(account)
    has_tools = tools_dict is not None

    try:
        if has_tools:
            # 有工具定义：使用 StreamSieve
            tool_names = get_tool_names(tools_dict)
            sieve = StreamSieve(
                mode='tool_call',
                parse_fn=lambda text: extract_tool_call(text, tool_names),
            )
            in_think = False
            buffer = ""

            async for sse_data in client.stream_api(query, thinking, effective_model, multi_medias):
                if sse_data.get("type") == "usage":
                    continue
                chunk = sse_data.get("content", "")
                if not chunk:
                    continue

                buffer += chunk.replace("\x00", "")

                while True:
                    if not in_think:
                        idx = buffer.find(THINK_OPEN)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                for ev in sieve.feed(safe):
                                    if ev.type == 'text':
                                        clean = _clean_response_text(ev.data, tool_names)
                                        if clean:
                                            text_parts.append(clean)
                                            if not content_started:
                                                content_started = True
                                                item = _response_text_item("", message_item_id)
                                                oi, start_evt = _start_output_item(item)
                                                if start_evt:
                                                    yield start_evt
                                                yield {
                                                    "type": "response.content_part.added",
                                                    "item_id": message_item_id,
                                                    "output_index": oi,
                                                    "content_index": 0,
                                                    "part": item["content"][0],
                                                }
                                            yield {
                                                "type": "response.output_text.delta",
                                                "item_id": message_item_id,
                                                "output_index": output_indices.get(message_item_id, 0),
                                                "content_index": 0,
                                                "delta": clean,
                                            }
                            in_think = True
                            buffer = buffer[idx + len(THINK_OPEN):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            for ev in sieve.feed(safe):
                                if ev.type == 'text':
                                    clean = _clean_response_text(ev.data, tool_names)
                                    if clean:
                                        text_parts.append(clean)
                                        if not content_started:
                                            content_started = True
                                            item = _response_text_item("", message_item_id)
                                            oi, start_evt = _start_output_item(item)
                                            if start_evt:
                                                yield start_evt
                                            yield {
                                                "type": "response.content_part.added",
                                                "item_id": message_item_id,
                                                "output_index": oi,
                                                "content_index": 0,
                                                "part": item["content"][0],
                                            }
                                        yield {
                                            "type": "response.output_text.delta",
                                            "item_id": message_item_id,
                                            "output_index": output_indices.get(message_item_id, 0),
                                            "content_index": 0,
                                            "delta": clean,
                                        }
                                elif ev.type == 'tool_calls':
                                    for tc in ev.data:
                                        idx = len(tool_calls_map)
                                        fc_item = _response_function_call_item(tc)
                                        fc_id = fc_item["id"]
                                        tool_calls_map[idx] = {
                                            "id": fc_id,
                                            "call_id": fc_item.get("call_id", fc_id),
                                            "name": tc.get("function", {}).get("name", ""),
                                            "arguments": tc.get("function", {}).get("arguments", "{}"),
                                            "status": "completed",
                                        }
                                        # output_item.added 不预填 arguments
                                        added_item = {k: v for k, v in fc_item.items() if k != "arguments"}
                                        oi, start_evt = _start_output_item(added_item)
                                        if start_evt:
                                            yield start_evt
                                        # 始终通过 delta 传递参数
                                        args_str = fc_item.get("arguments", "{}")
                                        yield {
                                            "type": "response.function_call_arguments.delta",
                                            "item_id": fc_id,
                                            "output_index": oi,
                                            "delta": args_str,
                                        }
                        buffer = keep
                        break
                    else:
                        idx = buffer.find(THINK_CLOSE)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                reasoning_parts.append(safe)
                                if len(reasoning_parts) == len(safe) == len(safe):
                                    pass
                                item = _response_reasoning_item("", reasoning_item_id)
                                oi, start_evt = _start_output_item(item)
                                if start_evt:
                                    yield start_evt
                                yield {
                                    "type": "response.reasoning_text.delta",
                                    "item_id": reasoning_item_id,
                                    "output_index": output_indices.get(reasoning_item_id, 0),
                                    "content_index": 0,
                                    "delta": safe,
                                }
                            in_think = False
                            buffer = buffer[idx + len(THINK_CLOSE):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            reasoning_parts.append(safe)
                            if len(reasoning_parts) == len(safe):
                                item = _response_reasoning_item("", reasoning_item_id)
                                oi, start_evt = _start_output_item(item)
                                if start_evt:
                                    yield start_evt
                            yield {
                                "type": "response.reasoning_text.delta",
                                "item_id": reasoning_item_id,
                                "output_index": output_indices.get(reasoning_item_id, 0),
                                "content_index": 0,
                                "delta": safe,
                            }
                        buffer = keep
                        break

            # Flush buffer
            if buffer and not in_think:
                for ev in sieve.feed(buffer):
                    if ev.type == 'text':
                        clean = _clean_response_text(ev.data, tool_names)
                        if clean:
                            text_parts.append(clean)
                            if not content_started:
                                content_started = True
                                item = _response_text_item("", message_item_id)
                                oi, start_evt = _start_output_item(item)
                                if start_evt:
                                    yield start_evt
                                yield {
                                    "type": "response.content_part.added",
                                    "item_id": message_item_id,
                                    "output_index": oi,
                                    "content_index": 0,
                                    "part": item["content"][0],
                                }
                            yield {
                                "type": "response.output_text.delta",
                                "item_id": message_item_id,
                                "output_index": output_indices.get(message_item_id, 0),
                                "content_index": 0,
                                "delta": clean,
                            }

            for ev in sieve.flush():
                if ev.type == 'text':
                    clean = _clean_response_text(ev.data, tool_names)
                    if clean:
                        text_parts.append(clean)
                        if not content_started:
                            content_started = True
                            item = _response_text_item("", message_item_id)
                            oi, start_evt = _start_output_item(item)
                            if start_evt:
                                yield start_evt
                            yield {
                                "type": "response.content_part.added",
                                "item_id": message_item_id,
                                "output_index": oi,
                                "content_index": 0,
                                "part": item["content"][0],
                            }
                        yield {
                            "type": "response.output_text.delta",
                            "item_id": message_item_id,
                            "output_index": output_indices.get(message_item_id, 0),
                            "content_index": 0,
                            "delta": clean,
                        }
                elif ev.type == 'tool_calls':
                    for tc in ev.data:
                        idx = len(tool_calls_map)
                        fc_item = _response_function_call_item(tc)
                        fc_id = fc_item["id"]
                        tool_calls_map[idx] = {
                            "id": fc_id,
                            "call_id": fc_item.get("call_id", fc_id),
                            "name": tc.get("function", {}).get("name", ""),
                            "arguments": tc.get("function", {}).get("arguments", "{}"),
                            "status": "completed",
                        }
                        # output_item.added 不预填 arguments
                        added_item = {k: v for k, v in fc_item.items() if k != "arguments"}
                        oi, start_evt = _start_output_item(added_item)
                        if start_evt:
                            yield start_evt
                        # 始终通过 delta 传递参数
                        args_str = fc_item.get("arguments", "{}")
                        yield {
                            "type": "response.function_call_arguments.delta",
                            "item_id": fc_id,
                            "output_index": oi,
                            "delta": args_str,
                        }

        else:
            # 无工具：简单流式
            in_think = False
            buffer = ""

            async for sse_data in client.stream_api(query, thinking, effective_model, multi_medias):
                if sse_data.get("type") == "usage":
                    continue
                chunk = sse_data.get("content", "")
                if not chunk:
                    continue

                buffer += chunk.replace("\x00", "")

                while True:
                    if not in_think:
                        idx = buffer.find(THINK_OPEN)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                clean = _clean_response_text(safe)
                                if clean:
                                    text_parts.append(clean)
                                    if not content_started:
                                        content_started = True
                                        item = _response_text_item("", message_item_id)
                                        oi, start_evt = _start_output_item(item)
                                        if start_evt:
                                            yield start_evt
                                        yield {
                                            "type": "response.content_part.added",
                                            "item_id": message_item_id,
                                            "output_index": oi,
                                            "content_index": 0,
                                            "part": item["content"][0],
                                        }
                                    yield {
                                        "type": "response.output_text.delta",
                                        "item_id": message_item_id,
                                        "output_index": output_indices.get(message_item_id, 0),
                                        "content_index": 0,
                                        "delta": clean,
                                    }
                            in_think = True
                            buffer = buffer[idx + len(THINK_OPEN):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            clean = _clean_response_text(safe)
                            if clean:
                                text_parts.append(clean)
                                if not content_started:
                                    content_started = True
                                    item = _response_text_item("", message_item_id)
                                    oi, start_evt = _start_output_item(item)
                                    if start_evt:
                                        yield start_evt
                                    yield {
                                        "type": "response.content_part.added",
                                        "item_id": message_item_id,
                                        "output_index": oi,
                                        "content_index": 0,
                                        "part": item["content"][0],
                                    }
                                yield {
                                    "type": "response.output_text.delta",
                                    "item_id": message_item_id,
                                    "output_index": output_indices.get(message_item_id, 0),
                                    "content_index": 0,
                                    "delta": clean,
                                }
                        buffer = keep
                        break
                    else:
                        idx = buffer.find(THINK_CLOSE)
                        if idx != -1:
                            safe, keep = _safe_flush(buffer[:idx])
                            if safe:
                                reasoning_parts.append(safe)
                                item = _response_reasoning_item("", reasoning_item_id)
                                oi, start_evt = _start_output_item(item)
                                if start_evt:
                                    yield start_evt
                                yield {
                                    "type": "response.reasoning_text.delta",
                                    "item_id": reasoning_item_id,
                                    "output_index": output_indices.get(reasoning_item_id, 0),
                                    "content_index": 0,
                                    "delta": safe,
                                }
                            in_think = False
                            buffer = buffer[idx + len(THINK_CLOSE):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            reasoning_parts.append(safe)
                            if len(reasoning_parts) == 1:
                                item = _response_reasoning_item("", reasoning_item_id)
                                oi, start_evt = _start_output_item(item)
                                if start_evt:
                                    yield start_evt
                            yield {
                                "type": "response.reasoning_text.delta",
                                "item_id": reasoning_item_id,
                                "output_index": output_indices.get(reasoning_item_id, 0),
                                "content_index": 0,
                                "delta": safe,
                            }
                        buffer = keep
                        break

            # 发送剩余缓冲区内容
            if buffer:
                clean = _clean_response_text(buffer)
                if clean:
                    if in_think:
                        reasoning_parts.append(clean)
                        yield {
                            "type": "response.reasoning_text.delta",
                            "item_id": reasoning_item_id,
                            "output_index": output_indices.get(reasoning_item_id, 0),
                            "content_index": 0,
                            "delta": clean,
                        }
                    else:
                        text_parts.append(clean)
                        if not content_started:
                            content_started = True
                            item = _response_text_item("", message_item_id)
                            oi, start_evt = _start_output_item(item)
                            if start_evt:
                                yield start_evt
                            yield {
                                "type": "response.content_part.added",
                                "item_id": message_item_id,
                                "output_index": oi,
                                "content_index": 0,
                                "part": item["content"][0],
                            }
                        yield {
                            "type": "response.output_text.delta",
                            "item_id": message_item_id,
                            "output_index": output_indices.get(message_item_id, 0),
                            "content_index": 0,
                            "delta": clean,
                        }

        # ─── 完成事件 ─────────────────────────────────────
        # 构建 output items
        output_by_id: dict[str, dict] = {}
        if reasoning_parts:
            output_by_id[reasoning_item_id] = _response_reasoning_item("".join(reasoning_parts), reasoning_item_id)
        full_text = _normalize_structured_output_text("".join(text_parts), text_config) if text_parts else ""
        if full_text:
            output_by_id[message_item_id] = _response_text_item(full_text, message_item_id)
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            output_by_id[tc["id"]] = {
                "id": tc["id"],
                "type": "function_call",
                "call_id": tc.get("call_id", tc["id"]),
                "name": tc["name"],
                "arguments": tc["arguments"],
                "status": "completed",
            }

        if not output_by_id:
            output_by_id[message_item_id] = _response_text_item("", message_item_id)
            if message_item_id not in output_indices:
                item = _response_text_item("", message_item_id)
                oi, start_evt = _start_output_item(item)
                if start_evt:
                    yield start_evt

        output = [
            item for _, item in sorted(
                output_by_id.items(),
                key=lambda pair: output_indices.get(pair[0], len(output_indices))
            )
        ]

        # 发出 done 事件
        if reasoning_parts:
            yield {
                "type": "response.reasoning_text.done",
                "item_id": reasoning_item_id,
                "output_index": output_indices.get(reasoning_item_id, 0),
                "content_index": 0,
                "text": "".join(reasoning_parts),
            }
        if full_text:
            yield {
                "type": "response.output_text.done",
                "item_id": message_item_id,
                "output_index": output_indices.get(message_item_id, 0),
                "content_index": 0,
                "text": full_text,
            }
            yield {
                "type": "response.content_part.done",
                "item_id": message_item_id,
                "output_index": output_indices.get(message_item_id, 0),
                "content_index": 0,
                "part": _response_text_item(full_text, message_item_id)["content"][0],
            }
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            yield {
                "type": "response.function_call_arguments.done",
                "item_id": tc["id"],
                "output_index": output_indices.get(tc["id"], 0),
                "arguments": tc["arguments"],
            }
        for idx, item in enumerate(output):
            if item.get("type") == "reasoning":
                # 由 reasoning_text.done 结束，不发 output_item.done
                # 避免 RikkaHub 重复创建空白思维链卡片
                continue
            yield {
                "type": "response.output_item.done",
                "output_index": idx,
                "item": item,
            }

        # 用量估算
        total_reasoning = "".join(reasoning_parts)
        total_text = "".join(text_parts)
        approx_completion = _count_tokens(total_reasoning + total_text)
        approx_prompt = _count_tokens(query)
        completion_record = {
            "input_tokens": approx_prompt,
            "output_tokens": approx_completion,
            "total_tokens": approx_prompt + approx_completion,
        }

        completed_payload = dict(init_payload)
        completed_payload["status"] = "completed"
        completed_payload["completed_at"] = int(time.time())
        completed_payload["output"] = output
        completed_payload["usage"] = _build_response_usage(completion_record)
        if has_tools and tool_calls_map:
            completed_payload["status"] = "completed"
        yield {"type": "response.completed", "response": completed_payload}

    except MimoApiError as e:
        failed_payload = dict(init_payload)
        failed_payload["status"] = "failed"
        failed_payload["error"] = {"message": f"MiMo API {e.status_code}: {e.response_body[:200]}", "type": "server_error"}
        yield {"type": "response.failed", "response": failed_payload}
    except httpx.ReadTimeout:
        failed_payload = dict(init_payload)
        failed_payload["status"] = "incomplete"
        failed_payload["incomplete_details"] = {"reason": "max_output_tokens"}
        yield {"type": "response.incomplete", "response": failed_payload}
    except Exception as e:
        failed_payload = dict(init_payload)
        failed_payload["status"] = "failed"
        failed_payload["error"] = {"message": str(e)[:500], "type": "server_error"}
        yield {"type": "response.failed", "response": failed_payload}


async def _sse_stream_response(body: dict, account):
    """将 _stream_response_events 包装为 SSE 格式。"""
    async for event in _stream_response_events(body, account):
        yield _sse_json(event)


# ─── 路由：8 个 Responses API 端点 ──────────────────────────

@router.post("/v1/responses")
async def create_response(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """创建 Response（非流式/流式）。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    body = await request.json()
    stream = body.get("stream", False)
    body["_response_id"] = _gen_response_id()

    account = config_manager.get_next_account()
    if not account:
        raise HTTPException(status_code=503, detail={"error": {"message": "no mimo account"}})

    if stream:
        return StreamingResponse(
            _sse_stream_response(body, account),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            }
        )

    # 非流式
    try:
        model_used, usage, items = await _do_response_chat(body, account)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": {"message": str(e)}})

    response_id = body["_response_id"]
    response_obj = _make_response_object(
        response_id=response_id,
        model=model_used,
        status="completed",
        items=items,
        usage=usage,
    )

    # 保存记录
    record = dict(response_obj)
    record["_messages"] = _convert_response_input_to_messages(body.get("input", []))
    record["_input"] = body.get("input", [])
    record["_body"] = body
    try:
        _save_response_record(record)
    except Exception:
        pass

    # 记录用量
    if usage:
        _add_usage(model_used, usage.get("promptTokens", 0) or usage.get("prompt_tokens", 0),
                   usage.get("completionTokens", 0) or usage.get("completion_tokens", 0))

    return response_obj


@router.post("/v1/responses/input_tokens")
async def count_input_tokens(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """估算 input tokens。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    body = await request.json()
    input_value = body.get("input")
    instructions = body.get("instructions") if isinstance(body.get("instructions"), str) else None
    tools = body.get("tools") if isinstance(body.get("tools"), list) else None
    count = _count_response_input_tokens(input_value, instructions, tools)
    return {"object": "response.input_tokens", "input_tokens": count}


def _compact_response_record(source: dict, body: dict) -> dict:
    """创建 compacted response 记录。"""
    response_id = _gen_response_id()
    now = int(time.time())
    source_text = _extract_output_text(source.get("output", []))
    compact_text = body.get("summary") if isinstance(body.get("summary"), str) else source_text
    compact_item = _response_text_item(compact_text or "", response_id)
    compact_messages = [{"role": "assistant", "content": compact_text or ""}]
    compact_body = {
        "_response_id": response_id,
        "input": [compact_item],
        "model": body.get("model") or source.get("model", "default"),
        "previous_response_id": source.get("id"),
        "metadata": dict(source.get("metadata") or {}),
        "store": True,
    }
    compact_body["metadata"].update({
        "compacted": True,
        "source_response_id": source.get("id"),
    })
    record = _make_response_object(
        response_id=response_id,
        model=compact_body["model"],
        status="completed",
        items=[_response_text_item(compact_text or "")],
        usage={
            "prompt_tokens": _count_tokens(json.dumps(source.get("_input", []), ensure_ascii=False)),
            "completion_tokens": _count_tokens(compact_text or ""),
            "total_tokens": _count_tokens(json.dumps(source.get("_input", []), ensure_ascii=False)) + _count_tokens(compact_text or ""),
        },
    )
    record["_messages"] = compact_messages
    record["_input"] = compact_body["input"]
    record["_body"] = compact_body
    record["store"] = True
    return record


@router.post("/v1/responses/compact")
async def compact_response(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """压缩 response。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    body = await request.json()
    response_id = body.get("response_id") or body.get("previous_response_id")
    if not response_id:
        raise HTTPException(status_code=400, detail={"error": {"message": "response_id is required", "type": "invalid_request_error"}})
    source = _get_response_record(response_id)
    if not source:
        raise HTTPException(status_code=404, detail={"error": {"message": f"response {response_id} not found", "type": "invalid_request_error"}})
    record = _compact_response_record(source, body)
    _save_response_record(record)
    return record


@router.post("/v1/responses/{response_id}/compact")
async def compact_response_by_id(
    response_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """按 ID 压缩 response。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    body = await request.json()
    source = _get_response_record(response_id)
    if not source:
        raise HTTPException(status_code=404, detail={"error": {"message": f"response {response_id} not found", "type": "invalid_request_error"}})
    record = _compact_response_record(source, body)
    _save_response_record(record)
    return record


@router.post("/v1/responses/{response_id}/cancel")
async def cancel_response(
    response_id: str,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """取消 response。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    record = _get_response_record(response_id)
    if not record:
        raise HTTPException(status_code=404, detail={"error": {"message": f"response {response_id} not found", "type": "invalid_request_error"}})
    if record.get("status") == "cancelled":
        return record
    if record.get("status") in _RESPONSE_TERMINAL_STATUSES:
        return record

    now = int(time.time())
    record["status"] = "cancelled"
    record["completed_at"] = now
    record["error"] = None
    record["incomplete_details"] = None
    _update_response_record(response_id, record)
    return record


@router.get("/v1/responses/{response_id}")
async def get_response(
    response_id: str,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """获取 response 记录。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    record = _get_response_record(response_id)
    if not record:
        raise HTTPException(status_code=404, detail={"error": {"message": f"response {response_id} not found", "type": "invalid_request_error"}})
    return record


@router.get("/v1/responses/{response_id}/input_items")
async def get_response_input_items(
    response_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """获取 response 的 input items。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    record = _get_response_record(response_id)
    if not record:
        raise HTTPException(status_code=404, detail={"error": {"message": f"response {response_id} not found", "type": "invalid_request_error"}})

    stored_items = record.get("_input") or record.get("input") or []
    if not isinstance(stored_items, list):
        stored_items = [stored_items] if stored_items else []

    # 统一转换为 dict 格式（input 可能是纯字符串）
    normalized = []
    for item in stored_items:
        if isinstance(item, str):
            normalized.append({"id": f"inp_{uuid.uuid4().hex[:24]}", "type": "input_text", "text": item})
        elif isinstance(item, dict):
            normalized.append(item)
    stored_items = normalized

    limit_raw = request.query_params.get("limit")
    try:
        limit = max(1, min(int(limit_raw), 100)) if limit_raw is not None else 20
    except ValueError:
        raise HTTPException(status_code=400, detail={"error": {"message": "invalid limit", "type": "invalid_request_error"}})

    after = request.query_params.get("after")
    before = request.query_params.get("before")
    order = (request.query_params.get("order") or "desc").lower()
    if order not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail={"error": {"message": "invalid order", "type": "invalid_request_error"}})

    # 简单分页
    ordered = list(stored_items)
    if order == "desc":
        ordered = list(reversed(ordered))
    if after:
        idx = next((i for i, item in enumerate(ordered) if item.get("id") == after), -1)
        ordered = ordered[idx + 1:] if idx != -1 else []
    if before:
        idx = next((i for i, item in enumerate(ordered) if item.get("id") == before), -1)
        ordered = ordered[:idx] if idx != -1 else []

    has_more = len(ordered) > limit
    page_items = ordered[:limit]

    return {
        "object": "list",
        "data": page_items,
        "first_id": page_items[0].get("id") if page_items else None,
        "last_id": page_items[-1].get("id") if page_items else None,
        "has_more": has_more,
    }


@router.delete("/v1/responses/{response_id}")
async def delete_response(
    response_id: str,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """删除 response 记录。"""
    api_key = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(api_key):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    if not _delete_response_record(response_id):
        raise HTTPException(status_code=404, detail={"error": {"message": f"response {response_id} not found", "type": "invalid_request_error"}})
    return {"id": response_id, "object": "response", "deleted": True}
