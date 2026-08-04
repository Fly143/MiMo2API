"""
Anthropic Messages API 路由 — MiMo2API 适配层

将 Anthropic Messages API 格式请求转换为 MiMo API 调用并转换回 Anthropic 格式。

转换路径:
  Anthropic Request → convert_request() → OpenAI dict
    → build_query_from_messages() + MimoClient
  MiMo Response → OCR/Analyze → OpenAI dict → convert_response() → Anthropic Response
"""
import json
import uuid
import time
import re
import httpx
import base64 as b64
from typing import Optional, AsyncIterator

from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import StreamingResponse, JSONResponse

from .anthropic import (
    convert_request as _anthropic_convert_request,
    convert_response as _anthropic_convert_response,
    stream_response as _anthropic_stream_response,
    nonstream_to_sse as _anthropic_nonstream_to_sse,
    error_response as _anthropic_error_response,
)
from .batch import (
    count_tokens as _anthropic_count_tokens,
    store_message as _anthropic_store_message,
    get_message as _anthropic_get_message,
    create_batch as _anthropic_create_batch,
    get_batch as _anthropic_get_batch,
    list_batches as _anthropic_list_batches,
    cancel_batch as _anthropic_cancel_batch,
    get_batch_results as _anthropic_get_batch_results,
    delete_batch as _anthropic_delete_batch,
    process_batch_requests as _anthropic_process_batch_requests,
)
from .batch import init_batch_storage as _anthropic_init_batch_storage
from .mimo_client import MimoClient, MimoApiError
from .config import config_manager
from .models import OpenAIMessage
from .utils import build_query_from_messages, extract_medias_from_messages, upload_media_to_mimo, upload_text_file_to_mimo
from .tool_call import extract_tool_call, get_tool_names, clean_tool_text
from .session_store import (
    get_or_create_session as _get_or_create_session,
    update_tokens as _update_session_tokens,
    update_fingerprint as _update_session_fingerprint,
)
from .usage_store import add_usage as _add_usage
from .routes import (
    _strip_citations, _strip_tool_result_blocks,
    _strip_tool_name_prefix, _strip_mimo_prefix, _safe_flush,
    validate_api_key,
)

router = APIRouter()

# ── Anthropic 模型名 → MiMo 内部模型名映射 ──
# Claude Code CLI 等工具期望 Anthropic 风格的模型名，MiMo 原生名不兼容。
# 此映射表在 Anthropic 端点请求时自动转换。
# ⚠️ 仅映射到 2.5 系列，使用旧版模型可能导致账号被封禁。
ANTHROPIC_MODEL_ALIASES = {
    # Claude 4.x 当前
    "claude-opus-4-6": "mimo-v2.5-pro",
    "claude-sonnet-4-6": "mimo-v2.5",
    "claude-haiku-4-5": "mimo-v2.5",
    # Claude 4.x 历史
    "claude-sonnet-4-5": "mimo-v2.5",
    "claude-opus-4-1": "mimo-v2.5-pro",
    "claude-opus-4-0": "mimo-v2.5-pro",
    "claude-sonnet-4-0": "mimo-v2.5",
    # Claude 3.x
    "claude-3-7-sonnet": "mimo-v2.5",
    "claude-3-5-sonnet": "mimo-v2.5",
    "claude-3-opus": "mimo-v2.5-pro",
    "claude-3-sonnet": "mimo-v2.5",
    "claude-3-haiku": "mimo-v2.5",
    # Search / nothinking 变体（MiMo 无联网/思考概念，映射到同一基础模型）
    "claude-opus-4-6-search": "mimo-v2.5-pro",
    "claude-sonnet-4-6-search": "mimo-v2.5",
    "claude-sonnet-4-6-nothinking": "mimo-v2.5",
    "claude-haiku-4-5-nothinking": "mimo-v2.5",
}


def _resolve_anthropic_model(model: str) -> str:
    """将 Anthropic 风格模型名映射为 MiMo 内部模型名。
    
    如果模型名已经是 MiMo 原生名（mimo-*），直接返回。
    如果在映射表中，返回对应的 MiMo 名。
    否则返回原值。
    """
    if not model or model.startswith("mimo-"):
        return model
    return ANTHROPIC_MODEL_ALIASES.get(model.lower(), model)

# ─── 常量 ─────────────────────────────────────────────────────

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

# ─── Anthropic SSE 辅助函数 ───────────────────────────────────

def _make_msg_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def _make_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _make_message_start(model: str, msg_id: str) -> str:
    return _make_sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })


def _make_cb_start(index: int, block: dict) -> str:
    return _make_sse("content_block_start", {
        "type": "content_block_start", "index": index, "content_block": block,
    })


def _make_text_delta(index: int, text: str) -> str:
    return _make_sse("content_block_delta", {
        "type": "content_block_delta", "index": index,
        "delta": {"type": "text_delta", "text": text},
    })


def _make_thinking_delta(index: int, text: str) -> str:
    return _make_sse("content_block_delta", {
        "type": "content_block_delta", "index": index,
        "delta": {"type": "thinking_delta", "thinking": text},
    })


def _make_cb_stop(index: int) -> str:
    return _make_sse("content_block_stop", {
        "type": "content_block_stop", "index": index,
    })


def _make_message_delta(stop_reason: str = "end_turn") -> str:
    return _make_sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason},
        "usage": {"output_tokens": 0},
    })


def _make_message_stop() -> str:
    return _make_sse("message_stop", {"type": "message_stop"})


# ─── 流式转换：MiMo SSE → Anthropic SSE ───────────────────────

class _StreamState:
    """追踪 Anthropic 流式状态。"""
    def __init__(self):
        self.buf = ""
        self.in_think = False
        self.text_index = None
        self.think_index = None
        self.next_index = 0
        self.think_active = False
        self.text_active = False
        self.any_think = False
        self.any_text = False
        self.finished = False
        self.collected_tool_calls = []
        self.text_buffer = ""  # 累积全文用于工具调用检测


async def _anthropic_stream_think_wrapper(
    mimo_stream: AsyncIterator[dict],
    model: str,
    msg_id: str,
    tool_names: list = None,
) -> AsyncIterator[str]:
    """
    将 MiMo 的流式事件（含 <think> 标签 + 可能含 TOOL_CALL）实时转换为 Anthropic SSE 事件。

    - 无工具：思考链实时流式，文本实时流式
    - 有工具：思考链实时流式，文本经 StreamSieve 实时筛分（TOOL_CALL 被捕获，正文流式）

    关键顺序约束（按 Anthropic 协议）：
      1. thinking block 必须在 text block 之前（index 更小）
      2. tool_use blocks 在 text 之后
      3. 每个 block 必须有 start→delta×N→stop
    """
    st = _StreamState()
    has_tools = tool_names is not None
    yield _make_message_start(model, msg_id)

    # 有工具时创建 StreamSieve 实时筛分 TOOL_CALL
    sieve = None
    collected_tool_calls = []
    if has_tools:
        from .tool_sieve import StreamSieve
        sieve = StreamSieve(
            mode='tool_call',
            parse_fn=lambda text: extract_tool_call(text, tool_names),
        )

    def _emit_text(text: str) -> list:
        """将清理后的文本以 text_delta 形式发出，返回 SSE 事件列表。"""
        if not text:
            return []
        events = []
        if not st.any_text:
            idx = st.next_index
            st.next_index += 1
            st.text_index = idx
            st.text_active = True
            st.any_text = True
            events.append(_make_cb_start(idx, {"type": "text", "text": ""}))
        events.append(_make_text_delta(st.text_index, text))
        return events

    def _flush_sieve(buf_text: str) -> list:
        """将文本送入 sieve 并返回 SSE 事件列表。"""
        if not buf_text:
            return []
        events = []
        if has_tools and sieve:
            for ev in sieve.feed(buf_text):
                if ev.type == 'text':
                    clean = _strip_tool_result_blocks(ev.data)
                    clean = _strip_citations(clean)
                    clean = _strip_tool_name_prefix(clean, tool_names)
                    clean = _strip_mimo_prefix(clean)
                    clean = clean_tool_text(clean)
                    if clean:
                        events.extend(_emit_text(clean))
                elif ev.type == 'tool_calls':
                    collected_tool_calls.extend(ev.data)
        else:
            clean = _strip_tool_result_blocks(buf_text)
            clean = _strip_citations(clean)
            clean = _strip_mimo_prefix(clean)
            clean = clean_tool_text(clean)
            events.extend(_emit_text(clean))
        return events

    async for ev in mimo_stream:
        if ev.get("type") == "usage":
            continue
        chunk = ev.get("content", "")
        if not chunk:
            continue

        st.buf += chunk.replace("\x00", "")

        while True:
            if not st.in_think:
                oi = st.buf.find(THINK_OPEN)
                if oi != -1:
                    pre = st.buf[:oi]
                    if pre:
                        for s in _flush_sieve(pre):
                            yield s
                    st.in_think = True
                    st.buf = st.buf[oi + len(THINK_OPEN):]
                    continue

                # 无 <think> → 普通文本（经 sieve）
                if st.buf:
                    for s in _flush_sieve(st.buf):
                        yield s
                    st.buf = ""
                break

            else:
                # in_think — 找 </think>
                ci = st.buf.find(THINK_CLOSE)
                if ci != -1:
                    think_text = st.buf[:ci]
                    if think_text:
                        if not st.any_think:
                            idx = st.next_index
                            st.next_index += 1
                            st.think_index = idx
                            st.think_active = True
                            st.any_think = True
                            yield _make_cb_start(idx, {"type": "thinking", "thinking": "", "signature": ""})
                        yield _make_thinking_delta(st.think_index, think_text)
                    if st.think_active:
                        yield _make_cb_stop(st.think_index)
                        st.think_active = False
                    st.in_think = False
                    st.buf = st.buf[ci + len(THINK_CLOSE):]
                    continue

                # thinking 未闭合 → flush 安全的 thinking delta
                safe, keep = _safe_flush(st.buf)
                if safe:
                    if not st.any_think:
                        idx = st.next_index
                        st.next_index += 1
                        st.think_index = idx
                        st.think_active = True
                        st.any_think = True
                        yield _make_cb_start(idx, {"type": "thinking", "thinking": "", "signature": ""})
                    yield _make_thinking_delta(st.think_index, safe)
                st.buf = keep
                break

    # --- 流结束：处理剩余 buffer ---
    if st.buf:
        if st.in_think:
            if not st.any_think:
                idx = st.next_index
                st.next_index += 1
                st.think_index = idx
                st.think_active = True
                st.any_think = True
                yield _make_cb_start(idx, {"type": "thinking", "thinking": "", "signature": ""})
            yield _make_thinking_delta(st.think_index, st.buf)
            if st.think_active:
                yield _make_cb_stop(st.think_index)
                st.think_active = False
        else:
            for s in _flush_sieve(st.buf):
                yield s

    if st.think_active:
        yield _make_cb_stop(st.think_index)
        st.think_active = False

    # --- 刷新 sieve 回收残留 text/tool_calls ---
    if has_tools and sieve:
        for ev in sieve.flush():
            if ev.type == 'text':
                clean = _strip_tool_result_blocks(ev.data)
                clean = _strip_citations(clean)
                clean = _strip_tool_name_prefix(clean, tool_names)
                clean = _strip_mimo_prefix(clean)
                clean = clean_tool_text(clean)
                if clean:
                    for s in _emit_text(clean):
                        yield s
            elif ev.type == 'tool_calls':
                collected_tool_calls.extend(ev.data)

    # --- 决定 stop_reason ---
    stop_reason = "end_turn"
    if collected_tool_calls:
        stop_reason = "tool_use"
        # 有工具调用：不发 text，关闭可能已开启的 text block
        st.text_active = False  # 重置，不发 text stop
        st.any_text = False
        # 发 tool_use blocks
        for tc in collected_tool_calls:
            fn = tc.get("function", {})
            try:
                arguments = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            idx = st.next_index
            st.next_index += 1
            yield _make_cb_start(idx, {
                "type": "tool_use",
                "id": tc.get("id", f"tu_{uuid.uuid4().hex[:24]}"),
                "name": fn.get("name", ""),
                "input": arguments,
            })
            yield _make_cb_stop(idx)
    else:
        # 无工具调用：text 已在流中发出，只需关闭 text block
        if st.text_active:
            yield _make_cb_stop(st.text_index)
            st.text_active = False

    yield _make_message_delta(stop_reason)
    yield _make_message_stop()


# ─── /v1/messages ────────────────────────────────────────────

@router.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
):
    """Anthropic Messages API 兼容端点。"""
    auth = authorization or (f"Bearer {x_api_key}" if x_api_key else None)
    if not validate_api_key(auth):
        raise HTTPException(
            status_code=401,
            detail=_anthropic_error_response("invalid api key", "authentication_error"),
        )

    body = await request.json()
    stream = body.get("stream", False)
    model = body.get("model", "mimo-v2.5")
    model = _resolve_anthropic_model(model)  # Anthropic 别名映射
    msg_id = _make_msg_id()

    # ── 转换格式：Anthropic → OpenAI ──
    openai_body = _anthropic_convert_request(body)
    openai_messages = openai_body.get("messages", [])
    openai_tools = openai_body.get("tools", None)

    # ── 获取账号 ──
    account = config_manager.get_next_account()
    if not account:
        raise HTTPException(
            status_code=503,
            detail=_anthropic_error_response("no mimo account", "service_error"),
        )

    # ── 构建 MiMo query ──
    # build_query_from_messages 需要 OpenAIMessage 对象（不是 dict）
    msgs_as_objects = []
    for m in openai_messages:
        if isinstance(m, dict):
            msgs_as_objects.append(OpenAIMessage(**m))
        else:
            msgs_as_objects.append(m)

    tools_dict = openai_tools
    query = build_query_from_messages(msgs_as_objects, tools=tools_dict)

    # ── 提取并上传图片/文件 ──
    query_text, base64_medias, text_files, processed_msgs = extract_medias_from_messages(msgs_as_objects)

    # 扫描 HTTP URL 图片（Anthropic source.type="url" → image_url with HTTP URL）
    http_images = []
    for m in openai_messages:
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "image_url":
                        img_url = item.get("image_url", {})
                        url = img_url.get("url", "") if isinstance(img_url, dict) else str(img_url)
                        if url and (url.startswith("http://") or url.startswith("https://")):
                            http_images.append(url)

    if http_images:
        async with httpx.AsyncClient(timeout=30) as http_client:
            for url in http_images:
                try:
                    resp = await http_client.get(url)
                    if resp.status_code == 200:
                        img_b64 = b64.b64encode(resp.content).decode()
                        content_type = resp.headers.get("content-type", "image/jpeg")
                        base64_medias.append({
                            "base64": img_b64,
                            "mimeType": content_type,
                            "type": "image"
                        })
                except Exception as e:
                    print(f"[Anthropic] failed to download image URL {url}: {e}")

    # 上传到 MiMo CDN
    multi_medias = []
    if base64_medias:
        for media in base64_medias:
            media_obj = await upload_media_to_mimo(
                media["base64"], media["mimeType"], account, model
            )
            if media_obj:
                multi_medias.append(media_obj)

    if text_files:
        for tf in text_files:
            media_obj = await upload_text_file_to_mimo(
                tf["base64"], tf["filename"], tf["mimeType"], account, model
            )
            if media_obj:
                multi_medias.append(media_obj)

    # ── 会话管理 ──
    conv_id, conv_is_new = _get_or_create_session(
        account.user_id, msgs_as_objects, model,
    )

    # ── 工具名（用于后续提取） ──
    tool_names = get_tool_names(tools_dict) if tools_dict else None

    client = MimoClient(account)

    # ═══════════════════════════════════════════════════════════
    # 流式
    # ═══════════════════════════════════════════════════════════
    if stream:
        async def _wrap():
            mimo_gen = client.stream_api(query, False, model, multi_medias=multi_medias, conversation_id=conv_id)
            async for event in _anthropic_stream_think_wrapper(
                mimo_gen, model, msg_id, tool_names=tool_names,
            ):
                yield event

        return StreamingResponse(
            _wrap(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    # ═══════════════════════════════════════════════════════════
    # 非流式
    # ═══════════════════════════════════════════════════════════
    try:
        content, think_content, usage = await client.call_api(
            query, False, model, multi_medias=multi_medias, conversation_id=conv_id,
        )

        # 保存用量
        if usage:
            _add_usage(model, usage.get("promptTokens", 0), usage.get("completionTokens", 0))
            _update_session_tokens(account.user_id, conv_id, usage.get("promptTokens", 0))

        # 清理模型输出
        content = _strip_tool_result_blocks(content)
        content = _strip_citations(content)

        # 提取工具调用
        tool_calls = None
        if tool_names:
            result = extract_tool_call(content, tool_names)
            if result:
                if result[0]:
                    tool_calls = result[0]
                if result[1] is not None:
                    content = result[1]  # 使用清理后的文本（含 MiMoML 残留清理）

        content = _strip_tool_name_prefix(content, tool_names or [])
        content = _strip_mimo_prefix(content)

        # 构建 OpenAI 格式的非流式响应
        message = {"role": "assistant", "content": content}
        if think_content:
            message["reasoning_content"] = think_content
        if tool_calls:
            message["tool_calls"] = tool_calls
            message["content"] = None  # 有工具调用时 content 必须为 null

        openai_result = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {
                "prompt_tokens": usage.get("promptTokens", 0) if usage else 0,
                "completion_tokens": usage.get("completionTokens", 0) if usage else 0,
                "total_tokens": (usage.get("promptTokens", 0) + usage.get("completionTokens", 0)) if usage else 0,
            },
        }

        # 转换为 Anthropic 格式
        anthropic_result = _anthropic_convert_response(openai_result, model, msg_id)

        # 存储消息
        _anthropic_store_message(msg_id, anthropic_result)

        return anthropic_result

    except MimoApiError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=_anthropic_error_response(f"MiMo API: {e.response_body[:200]}", "api_error"),
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=_anthropic_error_response(str(e), "internal_error"),
        )


# ─── /v1/messages/count_tokens ──────────────────────────────

@router.post("/v1/messages/count_tokens")
async def anthropic_count_tokens_ep(request: Request):
    """计算 Anthropic 格式消息的 token 数（本地估算）。"""
    body = await request.json()
    # 用 tiktoken 估算（需要 tiktoken 库）
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return _anthropic_count_tokens(body, enc)
    except ImportError:
        # 无 tiktoken 时返回 0
        return {"input_tokens": 0, "output_tokens": 0}


# ─── /v1/messages/{message_id} ──────────────────────────────

@router.get("/v1/messages/{message_id}")
async def anthropic_get_msg_ep(message_id: str):
    """查询已存储的消息。"""
    msg = _anthropic_get_message(message_id)
    if msg is None:
        raise HTTPException(
            status_code=404,
            detail=_anthropic_error_response(f"Message {message_id} not found", "not_found_error"),
        )
    return msg


# ─── Batches ────────────────────────────────────────────────

@router.post("/v1/messages/batches")
async def anthropic_create_batch_ep(request: Request):
    """创建批量任务。"""
    body = await request.json()
    requests_data = body.get("requests", [])
    model = body.get("model", "mimo-v2.5")
    model = _resolve_anthropic_model(model)  # Anthropic 别名映射
    batch = _anthropic_create_batch(requests_data, model)

    # 异步处理每个请求
    async def _process_one(req):
        ob = _anthropic_convert_request(req.get("body", {}))
        msgs = ob.get("messages", [])
        msgs_objs = [OpenAIMessage(**m) if isinstance(m, dict) else m for m in msgs]
        query = build_query_from_messages(msgs_objs)

        account = config_manager.get_next_account()
        if not account:
            return _anthropic_error_response("no mimo account", "service_error")

        client = MimoClient(account)
        try:
            c, tc, usage = await client.call_api(query, False, model)
            c = _strip_citations(c)
            message = {"role": "assistant", "content": c}
            if tc:
                message["reasoning_content"] = tc
            openai_resp = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion", "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            ant = _anthropic_convert_response(openai_resp, model)
            return ant
        except Exception as e:
            return _anthropic_error_response(str(e)[:500], "api_error")

    import asyncio
    asyncio.create_task(_anthropic_process_batch_requests(batch["id"], _process_one))
    return batch


@router.get("/v1/messages/batches")
async def anthropic_list_batches_ep(status: str = None, limit: int = 20, after_id: str = None):
    """批量任务列表。"""
    return _anthropic_list_batches(status, min(limit, 100), after_id)


@router.get("/v1/messages/batches/{batch_id}")
async def anthropic_get_batch_ep(batch_id: str):
    """批量任务详情。"""
    b = _anthropic_get_batch(batch_id)
    if b is None:
        raise HTTPException(
            status_code=404,
            detail=_anthropic_error_response(f"Batch {batch_id} not found", "not_found_error"),
        )
    return b


@router.post("/v1/messages/batches/{batch_id}/cancel")
async def anthropic_cancel_batch_ep(batch_id: str):
    """取消批量任务。"""
    b = _anthropic_cancel_batch(batch_id)
    if b is None:
        raise HTTPException(
            status_code=404,
            detail=_anthropic_error_response(f"Batch {batch_id} not found", "not_found_error"),
        )
    return b


@router.get("/v1/messages/batches/{batch_id}/results")
async def anthropic_batch_results_ep(batch_id: str):
    """下载批量任务结果。"""
    results = _anthropic_get_batch_results(batch_id)
    if results is None:
        raise HTTPException(
            status_code=404,
            detail=_anthropic_error_response(f"Results for batch {batch_id} not found", "not_found_error"),
        )
    return StreamingResponse(
        iter([json.dumps(r, ensure_ascii=False) + "\n" for r in results]),
        media_type="application/jsonl",
        headers={"Content-Disposition": f"attachment; filename={batch_id}_results.jsonl"},
    )


@router.delete("/v1/messages/batches/{batch_id}")
async def anthropic_delete_batch_ep(batch_id: str):
    """删除批量任务。"""
    _anthropic_delete_batch(batch_id)
    return {"id": batch_id, "type": "message_batch_deleted", "object": "message_batch"}
