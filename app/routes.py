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
from fastapi.responses import StreamingResponse, Response
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
async def list_models(authorization: Optional[str] = Header(None)):
    if not validate_api_key(authorization):
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
async def refresh_models(authorization: Optional[str] = Header(None)):
    if not validate_api_key(authorization):
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
async def get_model(model_id: str, authorization: Optional[str] = Header(None)):
    if not validate_api_key(authorization):
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
    authorization: Optional[str] = Header(None)
):
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

    if not validate_api_key(authorization):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

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
    query = build_query_from_messages(request.messages, tools=tools_dict)

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
            _stream_response(client, query, thinking, effective_model, tools_dict, multi_medias,
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

        msg_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        # 提取工具调用
        tool_names = []
        tool_calls = None
        if tools_dict:
            tool_names = get_tool_names(tools_dict)
            result = extract_tool_call(content, tool_names)
            if result and result[0]:
                tool_calls = result[0]  # List[Dict]
                content = result[1] if len(result) > 1 else clean_tool_text(content)

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
    conv_id: str = None, account_id: str = None,
):
    """流式响应生成器。

    行为矩阵：
    | 场景              | reasoning  | content             |
    | 无 tools          | 流式       | 流式                |
    | 有 tools+无工具调用 | 流式      | 缓冲后一次性发送     |
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
                                        clean = _strip_tool_result_blocks(ev.data)
                                        clean = _strip_citations(clean)
                                        clean = _strip_tool_name_prefix(clean, tool_names)
                                        if clean:
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
                                    clean = _strip_tool_result_blocks(ev.data)
                                    clean = _strip_citations(clean)
                                    clean = _strip_tool_name_prefix(clean, tool_names)
                                    if clean:
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
                        clean = _strip_tool_result_blocks(ev.data)
                        clean = _strip_citations(clean)
                        clean = _strip_tool_name_prefix(clean, tool_names)
                        if clean:
                            yield _build_chunk(msg_id, model, created=created_t, content=clean)
                    elif ev.type == 'tool_calls':
                        collected_tool_calls.extend(ev.data)

            # 刷新 sieve，回收最终工具调用
            for ev in sieve.flush():
                if ev.type == 'text':
                    clean = _strip_tool_result_blocks(ev.data)
                    clean = _strip_citations(clean)
                    clean = _strip_tool_name_prefix(clean, tool_names)
                    if clean:
                        yield _build_chunk(msg_id, model, created=created_t, content=clean)
                elif ev.type == 'tool_calls':
                    collected_tool_calls.extend(ev.data)

            if collected_tool_calls:
                streaming_tc = [{**tc, "index": 0} for tc in collected_tool_calls]
                yield _build_chunk(msg_id, model, created=created_t,
                                   tool_calls=streaming_tc, finish_reason="tool_calls")
                yield "data: [DONE]\n\n"
                if last_usage:
                    _add_usage(model, last_usage.get("promptTokens", 0), last_usage.get("completionTokens", 0))
                    _update_session_tokens(account_id, conv_id, last_usage.get("promptTokens", 0))
                return

            # 无工具调用：content 已经流式发送，只发 finish
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
                                yield _build_chunk(msg_id, model, created=created_t, content=safe)
                            in_think = True
                            buffer = buffer[idx + len(THINK_OPEN):]
                            continue

                        safe, keep = _safe_flush(buffer)
                        if safe:
                            yield _build_chunk(msg_id, model, created=created_t, content=safe)
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
                clean = _strip_tool_result_blocks(buffer)
                clean = _strip_citations(clean)
                clean = _strip_mimo_prefix(clean)
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


# ─── 模型列表（免鉴权，供管理页面使用） ───────────────────────

@router.get("/api/models")
async def admin_models():
    """返回可用模型列表（无鉴权，仅供管理页面动态加载）。"""
    return {"models": get_models_list()}


# ─── TTS (语音合成) ──────────────────────────────────────────
# MiMo TTS 流程：POST /open-apis/tts/v2/generate (异步创建) →
#              GET  /open-apis/tts/generateStatus    (轮询)  →
#              GET  audioUrl                          (下载音频)
# 映射到 OpenAI /v1/audio/speech 格式

TT_API_BASE = "https://aistudio.xiaomimimo.com"

# OpenAI 音色 → MiMo 音色映射（可通过 style 参数直接指定小米音色名）
VOICE_MAP = {
    "alloy": "冰糖",
    "echo": "冰糖",
    "fable": "冰糖",
    "onyx": "冰糖",
    "nova": "冰糖",
    "shimmer": "冰糖",
}


def _generate_style(speed: float, style_hint: str = "") -> str:
    """从 OpenAI speed 参数生成 MiMo 风格描述。"""
    if style_hint:
        return style_hint
    if speed < 0.8:
        return "语速较慢，声音沉稳柔和"
    if speed > 1.2:
        return "语速稍快，声音明亮有活力"
    return "语速正常，声音自然流畅"


@router.post("/v1/audio/speech")
async def tts_speech(request: Request, authorization: Optional[str] = Header(None)):
    if not validate_api_key(authorization):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    body = await request.json()
    text = body.get("input", "")
    if not text:
        raise HTTPException(status_code=400, detail={"error": {"message": "input is required"}})

    model = body.get("model", "mimo-v2.5-tts")
    voice_name = body.get("voice", "alloy")
    speed = body.get("speed", 1.0)
    resp_format = body.get("response_format", "wav")
    style_hint = body.get("style", "")

    # 选账号 — 遍历找到有完整凭证的账号
    accounts = config_manager.config.mimo_accounts
    if not accounts:
        raise HTTPException(status_code=503, detail={"error": {"message": "no mimo accounts configured"}})
    acct = None
    for a in accounts:
        if a.xiaomichatbot_ph and a.service_token and a.user_id:
            acct = a
            break
    if not acct:
        raise HTTPException(status_code=503, detail={"error": {"message": "no account with complete TTS credentials"}})
    ph = acct.xiaomichatbot_ph
    st = acct.service_token
    uid = acct.user_id

    conversation_id = uuid.uuid4().hex[:32]
    msg_id = uuid.uuid4().hex[:32]

    cookie_str = f'userId={uid}; serviceToken="{st}"; xiaomichatbot_ph="{ph}"'
    query = f"xiaomichatbot_ph={ph}"
    base_headers = {"Content-Type": "application/json", "Cookie": cookie_str}

    # 模型路由：内置音色 / 音色设计 / 语音克隆（依赖模型名后缀）
    if model.endswith("-voicedesign"):
        user_content = style_hint or "生成一个自然流畅的声音"
        audio_config = {"format": "wav"}
    elif model.endswith("-voiceclone"):
        user_content = ""
        if not voice_name or voice_name == "alloy" or "," not in str(voice_name):
            raise HTTPException(status_code=400, detail={"error": {"message": "voiceclone requires voice=data:audio/...;base64,..."}})
        mime_type = "audio/wav"
        if ";" in voice_name:
            mime_part = voice_name.split(";")[0].replace("data:", "", 1)
            if mime_part:
                mime_type = mime_part
        uploaded = await upload_media_to_mimo(voice_name, mime_type, acct, model="mimo-v2-omni")
        if not uploaded or not uploaded.get("fileUrl"):
            raise HTTPException(status_code=502, detail={"error": {"message": "voiceclone audio upload failed"}})
        audio_config = {"format": "wav", "voice": uploaded["fileUrl"]}
    else:
        mimo_voice = VOICE_MAP.get(voice_name, voice_name)
        user_content = _generate_style(speed, style_hint)
        audio_config = {"format": "wav", "voice": mimo_voice}

    async with httpx.AsyncClient(timeout=300) as client:
        # 0) 创建 TTS 对话（否则 TTS generate 会报 conversation not exist）
        save_payload = {
            "conversationId": conversation_id,
            "title": "新对话",
            "type": "tts",
        }
        sr = await client.post(
            f"{TT_API_BASE}/open-apis/chat/conversation/save?{query}",
            json=save_payload, headers=base_headers,
        )
        if sr.status_code != 200 or sr.json().get("code") != 0:
            raise HTTPException(status_code=502, detail={"error": {"message": "TTS conversation create failed"}})

        # 1) 提交 TTS 任务
        payload = {
            "conversationId": conversation_id,
            "msgId": msg_id,
            "content": {
                "messages": [
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": text},
                ],
                "audio": audio_config,
            },
            "modelConfig": {
                "modelCode": model,
                "scene": "BRIEF_DESCRIPTION",
            },
        }
        r = await client.post(
            f"{TT_API_BASE}/open-apis/tts/v2/generate?{query}",
            json=payload, headers=base_headers,
        )
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail={"error": {"message": f"TTS generate failed: {r.status_code}"}})
        data = r.json()
        if data.get("code") != 0:
            raise HTTPException(status_code=502, detail={"error": {"message": f"TTS generate error: {data.get('msg', 'unknown')}"}})

        task_id = data["data"]["taskId"]

        # 2) 轮询生成状态（最多等 60 秒）
        for _ in range(60):
            await asyncio.sleep(1)
            sr = await client.get(
                f"{TT_API_BASE}/open-apis/tts/generateStatus?{query}&taskId={task_id}",
                headers=base_headers,
            )
            if sr.status_code != 200:
                continue
            sdata = sr.json()
            if sdata.get("code") != 0:
                continue
            status = sdata["data"].get("status")
            if status == "success":
                audio_url = sdata["data"]["audioUrl"]
                break
            elif status == "failed":
                raise HTTPException(status_code=502, detail={"error": {"message": "TTS generation failed"}})
        else:
            raise HTTPException(status_code=504, detail={"error": {"message": "TTS generation timed out"}})

        # 3) 下载音频
        ar = await client.get(audio_url)
        if ar.status_code not in (200, 206):
            raise HTTPException(status_code=502, detail={"error": {"message": "TTS audio download failed"}})
        audio_bytes = ar.content

    # 响应格式转换（默认 wav，有 ffmpeg 才支持其他格式）
    content_type = "audio/wav"
    if resp_format in ("wav",):
        content_type = "audio/wav"
    else:
        pass  # 需要 ffmpeg 转码，暂只支持 wav

    return Response(content=audio_bytes, media_type=content_type)
