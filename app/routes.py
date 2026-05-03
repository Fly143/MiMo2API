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

# MiMo V2 全系列上下文窗口 = 128K tokens
# 网页端实测：三体全集 (925K字) 可读取约 10.95% ≈ 100K 字
MIMO_CONTEXT = 131072

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
    return {
        "object": "list",
        "data": [
            {
                "id": m, "object": "model", "created": 1681940951, "owned_by": "xiaomi",
                "max_input_tokens": MIMO_CONTEXT, "max_output_tokens": MIMO_CONTEXT,
                "context_length": MIMO_CONTEXT, "context_window": MIMO_CONTEXT,
            }
            for m in models
        ]
    }


@router.post("/v1/models/refresh")
async def refresh_models(authorization: Optional[str] = Header(None)):
    if not validate_api_key(authorization):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})
    models = await discover_models()
    return {
        "object": "list",
        "data": [
            {
                "id": m, "object": "model", "created": 1681940951, "owned_by": "xiaomi",
                "max_input_tokens": MIMO_CONTEXT, "max_output_tokens": MIMO_CONTEXT,
                "context_length": MIMO_CONTEXT, "context_window": MIMO_CONTEXT,
            }
            for m in models
        ]
    }


@router.get("/v1/models/{model_id}")
async def get_model(model_id: str, authorization: Optional[str] = Header(None)):
    if not validate_api_key(authorization):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})
    models = get_models_list()
    if model_id in models:
        return {
            "id": model_id, "object": "model", "created": 1681940951, "owned_by": "xiaomi",
            "max_input_tokens": MIMO_CONTEXT, "max_output_tokens": MIMO_CONTEXT,
            "context_length": MIMO_CONTEXT, "context_window": MIMO_CONTEXT,
        }
    raise HTTPException(status_code=404, detail={"error": {"message": f"Model {model_id} not found"}})


# ─── 文本清洗辅助函数 ────────────────────────────────────────

def _strip_citations(text: str) -> str:
    """移除 MiMo 模型输出的引用标记，如 (citation:1)(citation:14)。"""
    if not text:
        return text
    return re.sub(r'\(citation:\d+\)\s*', '', text).strip()


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
    content: str = None,
    finish_reason: str = "stop", usage: dict = None
) -> OpenAIResponse:
    """统一构建 OpenAI 非流式响应。"""
    message = OpenAIMessage(role="assistant", content=content)
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
    finish_reason: str = None,
    role: str = None, created: int = None
) -> str:
    """统一构建 SSE chunk 字符串。

    exclude_none=True 去除 null 字段，避免客户端因 message:null
    等非标准字段误判为非流式模式。
    reasoning 同时输出 reasoning 和 reasoning_content（RikkaHub 兼容）。
    """
    delta = OpenAIDelta(
        role=role, content=content,
        reasoning=reasoning
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

    # 构建查询（无工具调用）
    query = build_query_from_messages(request.messages)

    thinking = bool(request.reasoning_effort)
    client = MimoClient(account)

    # 会话管理：通过消息指纹续接 MiMo conversationId
    conv_id, conv_is_new = _get_or_create_session(
        account.user_id, request.messages, request.model
    )
    # 立即用当前消息更新指纹
    _update_session_fingerprint(account.user_id, conv_id, request.messages)

    # 流式响应
    if request.stream:
        return StreamingResponse(
            _stream_response(client, query, thinking, effective_model, multi_medias,
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

        # 清理模型输出杂质
        content = _strip_citations(content)

        # 保存用量
        if usage:
            _add_usage(request.model, usage.get("promptTokens", 0), usage.get("completionTokens", 0))
            _update_session_tokens(account.user_id, conv_id, usage.get("promptTokens", 0))

        msg_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

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
    multi_medias: list = None,
    conv_id: str = None, account_id: str = None,
):
    """流式响应生成器（无工具调用版）。"""
    msg_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created_t = int(time.time())

    # 初始 role delta
    yield _build_chunk(msg_id, model, created=created_t, role="assistant")

    buffer = ""
    in_think = False
    last_usage = None  # 最后一次收到的用量数据

    try:
        async for sse_data in client.stream_api(query, thinking, model, multi_medias):
            # 用量事件
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
            clean = _strip_citations(buffer)
            if clean:
                if in_think:
                    yield _build_chunk(msg_id, model, created=created_t, reasoning=clean)
                else:
                    yield _build_chunk(msg_id, model, created=created_t, content=clean)

        yield _build_chunk(msg_id, model, created=created_t, finish_reason="stop")

        # 保存流式用量
        if last_usage:
            _add_usage(model, last_usage.get("promptTokens", 0), last_usage.get("completionTokens", 0))
            _update_session_tokens(account_id, conv_id, last_usage.get("promptTokens", 0))

        yield "data: [DONE]\n\n"

    except httpx.ReadTimeout:
        yield _build_chunk(msg_id, model, created=created_t, finish_reason="length")
        yield "data: [DONE]\n\n"
    except MimoApiError as e:
        error_data = {"error": {"message": f"MiMo API {e.status_code}: {e.response_body[:200]}",
                                "type": "upstream_error", "code": e.status_code}}
        yield f"data: {json.dumps(error_data)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'error': {'message': str(e)}})}\n\n"
        yield "data: [DONE]\n\n"
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
