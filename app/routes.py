"""API路由"""

import time
import uuid
import json
import asyncio
from typing import Optional, Tuple
from pathlib import Path
from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import StreamingResponse, JSONResponse
from .models import (
    OpenAIRequest, OpenAIResponse, OpenAIChoice, OpenAIMessage,
    OpenAIDelta, OpenAIUsage, ParseCurlRequest, TestAccountRequest
)
from .config import config_manager, MimoAccount
from .mimo_client import MimoClient, MimoApiError
from .utils import parse_curl, build_query_from_messages, extract_medias_from_messages, upload_media_to_mimo
from .tool_call import extract_tool_call, normalize_tool_call, build_tool_prompt, get_tool_names, clean_tool_text

router = APIRouter()


def validate_api_key(authorization: Optional[str]) -> bool:
    """验证API Key"""
    if not authorization:
        return False
    key = authorization.replace("Bearer ", "").strip()
    return config_manager.validate_api_key(key)


# ====== 动态模型发现 ======

_models_cache = None
_models_lock = asyncio.Lock()

MODELS_CONFIG_URL = "https://aistudio.xiaomimimo.com/open-apis/bot/config"


async def _do_discover() -> list:
    """实时从 MiMo API config 端点获取可用模型列表"""
    global _models_cache

    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                MODELS_CONFIG_URL,
                headers={"User-Agent": "Mozilla/5.0"}
            )
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
            {"id": m, "object": "model", "created": 1681940951, "owned_by": "xiaomi"}
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
            {"id": m, "object": "model", "created": 1681940951, "owned_by": "xiaomi"}
            for m in models
        ]
    }


@router.get("/v1/models/{model_id}")
async def get_model(model_id: str, authorization: Optional[str] = Header(None)):
    if not validate_api_key(authorization):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    models = get_models_list()
    if model_id in models:
        return {"id": model_id, "object": "model", "created": 1681940951, "owned_by": "xiaomi"}

    raise HTTPException(status_code=404, detail={"error": {"message": f"Model {model_id} not found"}})


# ====== 响应构建辅助 ======

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


def _build_response(msg_id: str, model: str, content: str = None, tool_calls: list = None,
                    finish_reason: str = "stop", usage: dict = None) -> OpenAIResponse:
    """统一构建 OpenAI 非流式响应"""
    message = OpenAIMessage(
        role="assistant",
        content=content,
        tool_calls=tool_calls
    )
    usage_obj = None
    if usage:
        usage_obj = OpenAIUsage(
            prompt_tokens=usage.get("promptTokens", 0),
            completion_tokens=usage.get("completionTokens", 0),
            total_tokens=usage.get("promptTokens", 0) + usage.get("completionTokens", 0)
        )
    return OpenAIResponse(
        id=msg_id,
        object="chat.completion",
        created=int(time.time()),
        model=model,
        choices=[OpenAIChoice(index=0, message=message, finish_reason=finish_reason)],
        usage=usage_obj or OpenAIUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    )


def _build_chunk(msg_id: str, model: str, content: str = None, reasoning: str = None,
                  tool_calls: list = None, finish_reason: str = None, role: str = None) -> str:
    """统一构建 SSE chunk 字符串"""
    delta = OpenAIDelta(
        role=role,
        content=content,
        reasoning=reasoning,
        tool_calls=tool_calls
    )
    chunk = OpenAIResponse(
        id=msg_id,
        object="chat.completion.chunk",
        created=int(time.time()),
        model=model,
        choices=[OpenAIChoice(index=0, delta=delta, finish_reason=finish_reason)]
    )
    return f"data: {json.dumps(chunk.dict())}\n\n"


def _split_think(text: str) -> Tuple[str, str]:
    """从文本中分离 think 块和正文

    Returns:
        (main_content, think_content)
    """
    start = text.find(THINK_OPEN)
    if start == -1:
        return text, ""

    end = text.find(THINK_CLOSE, start)
    if end == -1:
        # 未闭合的 think 块
        return text[:start].strip(), text[start + len(THINK_OPEN):]

    think_content = text[start + len(THINK_OPEN):end]
    main = text[:start] + text[end + len(THINK_CLOSE):]
    return main.strip(), think_content


# ====== 聊天接口 ======

@router.post("/v1/chat/completions")
async def chat_completions(
    request: OpenAIRequest,
    authorization: Optional[str] = Header(None)
):
    """OpenAI兼容的聊天接口"""

    # 请求日志
    try:
        req_dump = request.dict()
        req_dump["messages"] = [
            {k: (v[:200] if isinstance(v, str) else v) for k, v in m.items()}
            for m in [msg.dict() for msg in request.messages]
        ]
        print(f"[REQ] model={request.model} stream={request.stream} tools={len(request.tools) if request.tools else 0} tool_choice={request.tool_choice}")
        print(f"[REQ] messages={json.dumps(req_dump['messages'], ensure_ascii=False)[:500]}")
    except Exception:
        pass

    if not validate_api_key(authorization):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key"}})

    account = config_manager.get_next_account()
    if not account:
        raise HTTPException(status_code=503, detail={"error": {"message": "no mimo account"}})

    # 转换 tools 为字典列表
    tools_dict = [t.dict() if hasattr(t, 'dict') else t for t in request.tools] if request.tools else None

    # 提取图片等媒体
    query_text, base64_medias, processed_msgs = extract_medias_from_messages(request.messages)

    # 上传图片到 MiMo
    multi_medias = []
    if base64_medias:
        effective_model = "mimo-v2-omni"
        for media in base64_medias:
            media_obj = await upload_media_to_mimo(
                media["base64"], media["mimeType"], account, effective_model
            )
            if media_obj:
                multi_medias.append(media_obj)
    else:
        effective_model = request.model

    # 构建查询字符串
    query = build_query_from_messages(request.messages, tools=tools_dict)

    # 判断是否启用深度思考
    thinking = bool(request.reasoning_effort)

    # 创建Mimo客户端
    client = MimoClient(account)

    # 流式响应
    if request.stream:
        return StreamingResponse(
            _stream_response(client, query, thinking, effective_model, tools_dict, multi_medias),
            media_type="text/event-stream"
        )

    # 非流式响应
    try:
        content, think_content, usage = await client.call_api(query, thinking, effective_model, multi_medias)

        msg_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        # 有工具定义时，检查工具调用
        if tools_dict:
            tool_names = get_tool_names(tools_dict)
            tool_call, cleaned = extract_tool_call(content, tool_names)
        else:
            tool_call, cleaned = None, content

        if tool_call:
            # 返回工具调用 + 清理后的自然语言（不含 TOOL_CALL 标签）
            return _build_response(
                msg_id, request.model,
                content=cleaned if cleaned else None,
                tool_calls=[tool_call],
                finish_reason="tool_calls",
                usage=usage
            )
        else:
            # 普通文本响应（含 think 块）
            full_content = content
            if think_content:
                full_content = f"{THINK_OPEN}{think_content}{THINK_CLOSE}\n{content}"
            return _build_response(
                msg_id, request.model,
                content=full_content,
                finish_reason="stop",
                usage=usage
            )

    except MimoApiError as e:
        raise HTTPException(status_code=e.status_code, detail={"error": {"message": f"MiMo API: {e.response_body[:200]}"}})
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail={"error": {"message": str(e)}})


async def _stream_response(client: MimoClient, query: str, thinking: bool, model: str,
                            tools: list = None, multi_medias: list = None):
    """流式响应生成器

    有工具定义时：先缓冲全部内容，收完后提取工具调用再输出
    无工具定义时：实时流式输出
    """
    msg_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    # 发送初始 role delta
    yield _build_chunk(msg_id, model, role="assistant")

    has_tools = tools is not None

    try:
        if has_tools:
            # 有工具时：缓冲全部内容，最后判断
            full_content = ""
            async for sse_data in client.stream_api(query, thinking, model, multi_medias):
                chunk = sse_data.get("content", "")
                if chunk:
                    full_content += chunk

            # 清理 null 字节
            full_content = full_content.replace("\x00", "")

            # 先分离 think 块，再提取工具调用
            main_text, think_text = _split_think(full_content)
            tool_names = get_tool_names(tools)
            tool_call, cleaned_main = extract_tool_call(main_text, tool_names)

            # 发送 think 内容
            if think_text:
                yield _build_chunk(msg_id, model, reasoning=think_text)

            if tool_call:
                # Send cleaned natural language content first, then tool calls
                if cleaned_main:
                    yield _build_chunk(msg_id, model, content=cleaned_main)
                streaming_tc = {**tool_call, "index": 0}
                yield _build_chunk(msg_id, model, tool_calls=[streaming_tc], finish_reason="tool_calls")
            else:
                # 无工具调用：发送正文
                if main_text:
                    yield _build_chunk(msg_id, model, content=main_text)
                yield _build_chunk(msg_id, model, finish_reason="stop")

            yield "data: [DONE]\n\n"

        else:
            # 无工具时：实时流式输出
            buffer = ""
            in_think = False

            async for sse_data in client.stream_api(query, thinking, model, multi_medias):
                chunk = sse_data.get("content", "")
                if not chunk:
                    continue

                buffer += chunk
                text = buffer.replace("\x00", "")

                # 处理 think 标签
                while True:
                    if not in_think:
                        idx = text.find(THINK_OPEN)
                        if idx != -1:
                            if idx > 0:
                                yield _build_chunk(msg_id, model, content=text[:idx])
                            in_think = True
                            text = text[idx + len(THINK_OPEN):]
                            continue

                        # 安全截断：保留可能的不完整标签
                        safe = len(text) - len(THINK_OPEN)
                        if safe > 0:
                            yield _build_chunk(msg_id, model, content=text[:safe])
                            text = text[safe:]
                        break

                    else:
                        idx = text.find(THINK_CLOSE)
                        if idx != -1:
                            if idx > 0:
                                yield _build_chunk(msg_id, model, reasoning=text[:idx])
                            in_think = False
                            text = text[idx + len(THINK_CLOSE):]
                            continue

                        safe = len(text) - len(THINK_CLOSE)
                        if safe > 0:
                            yield _build_chunk(msg_id, model, reasoning=text[:safe])
                            text = text[safe:]
                        break

                buffer = text

            # 发送剩余内容
            if buffer:
                if in_think:
                    yield _build_chunk(msg_id, model, reasoning=buffer)
                else:
                    yield _build_chunk(msg_id, model, content=buffer)

            yield _build_chunk(msg_id, model, finish_reason="stop")
            yield "data: [DONE]\n\n"

    except MimoApiError as e:
        error_data = {
            "error": {
                "message": f"MiMo API {e.status_code}: {e.response_body[:200]}",
                "type": "upstream_error",
                "code": e.status_code
            }
        }
        yield f"data: {json.dumps(error_data)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log_path = Path(__file__).parent.parent / "error.log"
        with open(log_path, "a") as f:
            f.write(f"=== STREAM ERROR ===\n{tb}\n\n")
        error_chunk = {"error": {"message": str(e)}}
        yield f"data: {json.dumps(error_chunk)}\n\n"


# ====== 管理页面 ======

from pathlib import Path as _Path
_ADMIN_HTML = (_Path(__file__).parent.parent / "web" / "index.html").read_text(encoding="utf-8")

@router.get("/admin")
@router.get("/")
async def admin_page():
    from starlette.responses import HTMLResponse
    return HTMLResponse(_ADMIN_HTML)


# ====== 账号管理 API ======

import re as _re
from datetime import datetime as _dt

@router.get("/api/accounts")
async def list_accounts():
    """列出所有账号（带掩码）"""
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
    """通过 Cookie 导入账号"""
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
    """通过 cURL 导入账号"""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "invalid json")

    curl = (data.get("curl") or "").strip()
    if not curl:
        return {"ok": False, "error": "请提供 cURL 命令"}

    # Parse cookies from cURL
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
    """验证凭证有效性并保存"""
    from .mimo_client import MimoClient, MimoApiError

    account = MimoAccount(service_token=service_token, user_id=user_id, xiaomichatbot_ph=xiaomichatbot_ph)
    client = MimoClient(account)

    try:
        content, _, _ = await client.call_api("hi", False)
        now = _dt.now().strftime("%m-%d %H:%M")

        # Check if account already exists, update; otherwise add
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
    """删除账号"""
    accounts = config_manager.config.mimo_accounts
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "account not found")
    removed = accounts.pop(idx)
    config_manager.save()
    return {"ok": True, "removed_user_id": removed.user_id}


@router.post("/api/accounts/{idx}/test")
async def test_account(idx: int):
    """测试账号连接"""
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


# ====== 旧版管理接口 (保留兼容) ======

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
async def test_account(request: TestAccountRequest):
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
