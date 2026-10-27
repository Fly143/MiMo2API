"""Mimo API客户端"""

import json
import os
import uuid
import httpx
import asyncio
import traceback
from typing import Optional, Tuple, AsyncIterator
from .config import MimoAccount

# ─── 重试配置 ─────────────────────────────────────────────
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 10.0


THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


async def stream_chat(client, query, thinking, model, multi_medias=None,
                      conversation_id=None, reasoning_effort=None):
    """stream_api 的兼容层：把 v3 的 reasoning 增量折成 <think> 标签。

    routes / anthropic_routes 各端点仍按标签解析思考块，这里只做协议形状转换，
    正文、usage、citations 原样透传。
    """
    in_think = False
    async for item in client.stream_api(
        query, thinking, model, multi_medias,
        conversation_id=conversation_id, reasoning_effort=reasoning_effort,
    ):
        kind = item.get("type")
        if kind == "reasoning":
            piece = item.get("content") or ""
            if not piece:
                continue
            if not in_think:
                in_think = True
                piece = THINK_OPEN + piece
            yield {"type": "text", "content": piece}
            continue
        if kind == "text":
            piece = item.get("content") or ""
            if not piece:
                continue
            if in_think:
                in_think = False
                piece = THINK_CLOSE + piece
            yield {"type": "text", "content": piece}
            continue
        yield item
    if in_think:
        yield {"type": "text", "content": THINK_CLOSE}


class MimoApiError(Exception):
    """MiMo API上游错误，携带HTTP状态码和响应体"""
    def __init__(self, status_code: int, response_body: str):
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(f"MiMo API error {status_code}: {response_body[:200]}")


class MimoClient:
    """Mimo API客户端"""

    # v3 协议（2026-10 起唯一可用）。旧 /open-apis/bot/chat 固定回 event:error
    # {"type":"text","content":"平台升级啦，请「刷新页面」畅享更好的体验！"}，不再产生任何回答。
    API_URL = "https://aistudio.xiaomimimo.com/open-apis/react/chat"
    LEGACY_API_URL = "https://aistudio.xiaomimimo.com/open-apis/bot/chat"
    # 主对话 HTTP 超时（秒）。默认 0 = 不限：思考+输出整条流纯透传，不设代理侧时限。
    # 需要保护时显式设 MIMO_CLIENT_TIMEOUT=600 等。
    _t = float(os.getenv("MIMO_CLIENT_TIMEOUT", "0") or "0")
    TIMEOUT = None if _t <= 0 else _t

    # MiMo API 原生 SSE 事件前缀（始终在 SSE #2 输出，独立于我们的工具定义）
    _MIMO_SSE_PREFIXES = {'webSearch', 'getTime', 'getTimeInfo', 'sessionSearch',
                          'imageSearch', 'fileSearch', 'getLocation', 'webExtract',
                          'getWeather', 'calculator'}

    def __init__(self, account: MimoAccount):
        self.account = account

    def _create_headers(self) -> dict:
        """创建请求头"""
        return {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": "https://aistudio.xiaomimimo.com",
            "Referer": "https://aistudio.xiaomimimo.com/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "x-timezone": "Asia/Shanghai",
        }

    def _create_cookies(self) -> dict:
        """创建Cookies"""
        return {
            "serviceToken": self.account.service_token,
            "userId": self.account.user_id,
            "xiaomichatbot_ph": self.account.xiaomichatbot_ph,
        }

    def _create_request_body(self, query: str, thinking: bool, model: str = "mimo-v2.5-pro", multi_medias: list = None, attachments: list = None, conversation_id: str = None, temperature: float = None, top_p: float = None, reasoning_effort: str = None) -> dict:
        """创建请求体。temperature/topP/reasoning_effort 仅在显式传入时带上；仅 thinking 且无档位时默认 high。"""
        model_config = {
            "enableThinking": thinking,
            "webSearchStatus": "disabled",
            "model": model,
        }
        if temperature is not None:
            model_config["temperature"] = temperature
        if top_p is not None:
            model_config["topP"] = top_p
        if reasoning_effort:
            model_config["reasoning_effort"] = reasoning_effort
        elif thinking:
            model_config["reasoning_effort"] = "high"
        return {
            "msgId": uuid.uuid4().hex[:32],
            "conversationId": conversation_id or uuid.uuid4().hex[:32],
            "query": query,
            "modelConfig": model_config,
            "multiMedias": multi_medias or [],
            "attachments": attachments or []
        }

    async def call_api(self, query: str, thinking: bool = False, model: str = "mimo-v2.5-pro", multi_medias: list = None, attachments: list = None, conversation_id: str = None, reasoning_effort: str = None) -> Tuple[str, str, dict]:
        """调用Mimo API（非流式），带重试"""
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                return await self._call_api_once(query, thinking, model, multi_medias, attachments, conversation_id, reasoning_effort)
            except MimoApiError as e:
                last_error = e
                if e.status_code in (401, 403, 404):
                    raise
                if attempt < MAX_RETRIES - 1:
                    delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                    print(f"[Retry] {attempt+1}/{MAX_RETRIES} failed ({e.status_code}), retry in {delay}s...")
                    await asyncio.sleep(delay)
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                    print(f"[Retry] {attempt+1}/{MAX_RETRIES} failed ({e}), retry in {delay}s...")
                    await asyncio.sleep(delay)
        raise last_error

    async def _call_api_once(self, query: str, thinking: bool = False, model: str = "mimo-v2.5-pro", multi_medias: list = None, attachments: list = None, conversation_id: str = None, reasoning_effort: str = None) -> Tuple[str, str, dict]:
        """
        调用Mimo API（非流式）单次执行

        Args:
            conversation_id: 复用现有 MiMo 会话 ID（None=新建）

        Returns:
            (content, think_content, usage)
        """
        body = self._create_request_body(query, thinking, model, multi_medias, attachments, conversation_id, reasoning_effort=reasoning_effort)

        async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
            response = await client.post(
                self.API_URL,
                params={"xiaomichatbot_ph": self.account.xiaomichatbot_ph},
                headers=self._create_headers(),
                cookies=self._create_cookies(),
                json=body
            )

            if response.status_code != 200:
                raise MimoApiError(response.status_code, response.text)

            content_parts = []
            think_parts = []
            citations = []
            usage = {"promptTokens": 0, "completionTokens": 0}

            async for event, payload in self._iter_events(response):
                if event == "error":
                    raise MimoApiError(502, json.dumps(payload, ensure_ascii=False))
                if event == "message" and isinstance(payload, dict):
                    think_parts.append(payload.get("reasoningContent") or "")
                    content_parts.append(payload.get("content") or "")
                elif event == "search" and isinstance(payload, list):
                    citations = payload
                elif isinstance(payload, dict) and "promptTokens" in payload:
                    usage = {
                        "promptTokens": payload.get("promptTokens", 0),
                        "completionTokens": payload.get("completionTokens", 0),
                        "totalTokens": payload.get("totalTokens", 0),
                        "nativeUsage": payload.get("nativeUsage", {}),
                    }

            # v3 把思考与正文分成两个字段；老式 <think> 标签仅作兜底
            full_content = "".join(content_parts).replace("\x00", "")
            think_content = "".join(think_parts).replace("\x00", "")
            if not think_content:
                full_content, think_content = self._parse_think_tags(full_content)

            return full_content, think_content, usage, citations

    @staticmethod
    async def _iter_events(response):
        """解析 v3 SSE：`id:` / `event:` / `data:` 三段式，按事件名逐个产出。

        事件名：dialogId / message / usage / finish / search / error。
        `data:[DONE]` 直接结束；解析失败的行丢弃。
        """
        event = None
        async for line in response.aiter_lines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            if raw == "[DONE]":
                return
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            yield (event or "message"), payload
            event = None

    async def stream_api(self, query: str, thinking: bool = False, model: str = "mimo-v2.5-pro", multi_medias: list = None, attachments: list = None, conversation_id: str = None, reasoning_effort: str = None) -> AsyncIterator[dict]:
        """
        调用Mimo API（流式，v3 协议）

        Yields:
            {"type":"text","content":...}      正文增量
            {"type":"reasoning","content":...} 思考增量（上游 reasoningContent）
            {"type":"usage", ...}              用量
            {"type":"citations","data":[...]}  搜索结果（webSearch 开启时）
        """
        body = self._create_request_body(query, thinking, model, multi_medias, attachments, conversation_id, reasoning_effort=reasoning_effort)

        async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
            async with client.stream(
                "POST",
                self.API_URL,
                params={"xiaomichatbot_ph": self.account.xiaomichatbot_ph},
                headers=self._create_headers(),
                cookies=self._create_cookies(),
                json=body
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    raise MimoApiError(response.status_code, error_body.decode(errors="replace"))

                async for event, payload in self._iter_events(response):
                    if event == "error":
                        raise MimoApiError(502, json.dumps(payload, ensure_ascii=False))
                    if event == "message" and isinstance(payload, dict):
                        reasoning = payload.get("reasoningContent")
                        if reasoning:
                            yield {"type": "reasoning", "content": reasoning}
                        text = payload.get("content")
                        if text:
                            yield {"type": "text", "content": text}
                    elif event == "search" and isinstance(payload, list):
                        yield {"type": "citations", "data": payload}
                    elif isinstance(payload, dict) and "promptTokens" in payload:
                        yield {"type": "usage", "promptTokens": payload.get("promptTokens", 0),
                               "completionTokens": payload.get("completionTokens", 0),
                               "totalTokens": payload.get("totalTokens", 0),
                               "nativeUsage": payload.get("nativeUsage", {})}

    @staticmethod
    def _parse_think_tags(text: str) -> Tuple[str, str]:
        """
        解析think标签

        Returns:
            (content, think_content)
        """
        start = text.find("<think>")
        if start == -1:
            return text, ""

        end = text.find("</think>")
        if end == -1:
            return text, ""

        think_content = text[start + 7:end]
        content = text[end + 8:]
        return content, think_content

    async def delete_conversations(self, conversation_ids: list) -> bool:
        """删除 MiMo 服务端对话记录。

        Args:
            conversation_ids: 要删除的 conversation_id 列表

        Returns:
            True 表示全部删除成功
        """
        if not conversation_ids:
            return True
        url = "https://aistudio.xiaomimimo.com/open-apis/chat/conversation/delete"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url,
                    params={"xiaomichatbot_ph": self.account.xiaomichatbot_ph},
                    headers=self._create_headers(),
                    cookies=self._create_cookies(),
                    json=conversation_ids,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("code") == 0
                print(f"[Cleanup] MiMo delete failed: HTTP {resp.status_code}")
                return False
        except Exception as e:
            print(f"[Cleanup] MiMo delete error: {e}")
            return False
