"""Mimo API客户端"""

import json
import uuid
import httpx
import traceback
from typing import Optional, Tuple, AsyncIterator
from .config import MimoAccount


class MimoApiError(Exception):
    """MiMo API上游错误，携带HTTP状态码和响应体"""
    def __init__(self, status_code: int, response_body: str):
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(f"MiMo API error {status_code}: {response_body[:200]}")


class MimoClient:
    """Mimo API客户端"""

    API_URL = "https://aistudio.xiaomimimo.com/open-apis/bot/chat"
    TIMEOUT = 120.0

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

    def _create_request_body(self, query: str, thinking: bool, model: str = "mimo-v2-pro", multi_medias: list = None, attachments: list = None) -> dict:
        """创建请求体"""
        return {
            "msgId": uuid.uuid4().hex[:32],
            "conversationId": uuid.uuid4().hex[:32],
            "query": query,
            "modelConfig": {
                "enableThinking": thinking,
                "temperature": 0.8,
                "topP": 0.95,
                "webSearchStatus": "disabled",
                "model": model
            },
            "multiMedias": multi_medias or [],
            "attachments": attachments or []
        }

    async def call_api(self, query: str, thinking: bool = False, model: str = "mimo-v2-pro", multi_medias: list = None, attachments: list = None) -> Tuple[str, str, dict]:
        """
        调用Mimo API（非流式）

        Returns:
            (content, think_content, usage)
        """
        body = self._create_request_body(query, thinking, model, multi_medias, attachments)

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

            result = []
            usage = {"promptTokens": 0, "completionTokens": 0}

            # 解析SSE流
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    try:
                        sse_data = json.loads(data)
                        if isinstance(sse_data, dict):
                            if sse_data.get("type") == "text":
                                content = sse_data.get("content", "")
                                # 过滤 MiMo 原生前缀
                                if content.strip() not in self._MIMO_SSE_PREFIXES:
                                    result.append(content)
                            if "promptTokens" in sse_data:
                                usage = {
                                    "promptTokens": sse_data.get("promptTokens", 0),
                                    "completionTokens": sse_data.get("completionTokens", 0)
                                }
                        # list 类型跳过
                        elif isinstance(sse_data, list):
                            continue
                    except json.JSONDecodeError:
                        continue

            # 合并结果并解析think标签
            full_text = "".join(result).replace("\x00", "")
            content, think_content = self._parse_think_tags(full_text)

            return content, think_content, usage

    async def stream_api(self, query: str, thinking: bool = False, model: str = "mimo-v2-pro", multi_medias: list = None, attachments: list = None) -> AsyncIterator[dict]:
        """
        调用Mimo API（流式）

        Yields:
            SSE数据字典（仅 type=text 且有 content 的，已过滤 MiMo 原生前缀）
        """
        body = self._create_request_body(query, thinking, model, multi_medias, attachments)

        chunk_count = 0

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

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    chunk_count += 1
                    try:
                        sse_data = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    # 安全的类型分发
                    if isinstance(sse_data, list):
                        continue
                    if not isinstance(sse_data, dict):
                        continue

                    # DEBUG 日志（已关闭）
                    # try:
                    #     with open('/data/data/com.termux/files/home/MiMo2API/debug_api.log', 'a') as _df:
                    #         _df.write(f"[SSE #{chunk_count}] type={sse_data.get('type','?')} content={repr(sse_data.get('content',''))[:200]} keys={list(sse_data.keys())}\n")
                    # except Exception:
                    #     pass

                    # 过滤 MiMo 原生 SSE 前缀事件（如 SSE #2 的 'webSearch'）
                    if sse_data.get("type") == "text" and sse_data.get("content"):
                        content_val = sse_data["content"].strip()
                        if content_val in self._MIMO_SSE_PREFIXES:
                            continue  # 跳过 MiMo 原生的工具名 SSE 事件

                    # 只 yield text 类型和 usage 事件
                    if sse_data.get("type") == "text" and sse_data.get("content"):
                        yield sse_data
                    elif "promptTokens" in sse_data:
                        yield {"type": "usage", "promptTokens": sse_data.get("promptTokens", 0),
                               "completionTokens": sse_data.get("completionTokens", 0),
                               "totalTokens": sse_data.get("totalTokens", 0)}

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
