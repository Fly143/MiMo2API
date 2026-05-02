"""
StreamSieve — 流式筛分引擎
逐字符喂入，实时分离正文与工具调用，不缓冲全文。

模式：
  - 'tool_call': 检测 TOOL_CALL: name(args) / <tool_call> / <function_call> / [调用工具:]
  - 'dsml':     检测 <|DSML|tool_calls> / <tool_calls>（deepseek-free-api 用）

参考: https://github.com/CJackHwang/ds2api (internal/toolstream/)
"""

from dataclasses import dataclass, field
from typing import List, Callable, Optional, Any, Tuple


@dataclass
class SieveEvent:
    type: str  # 'text' | 'tool_calls'
    data: Any  # str for text, list[dict] for tool_calls


class StreamSieve:
    """流式筛分器 — 实时分离正文与工具调用文本"""

    # 工具调用起始标记（按优先级）
    _TOOL_STARTS = [
        "TOOL_CALL:",
        "<tool_call",
        "<function_call",
        "<function=",
        "[调用工具:",
        "<|DSML|tool_calls>",
        "<tool_calls>",
    ]

    def __init__(
        self,
        mode: str = "tool_call",
        parse_fn: Optional[Callable[[str], Tuple]] = None,
    ):
        self.mode = mode
        self.parse_fn = parse_fn  # (text, tool_names) -> (tool_calls, cleaned_text)
        self._pending = ""         # 正常模式下的缓冲（最多保留尾部可疑字符）
        self._capture_buf = ""     # 捕获模式下的缓冲
        self._capturing = False    # 是否在捕获模式
        self._tool_start_idx = -1  # 捕获模式起始位置（在 _capture_buf 中的索引）

    def feed(self, chunk: str) -> List[SieveEvent]:
        """喂入一块文本，返回筛分事件列表"""
        events: List[SieveEvent] = []

        if self._capturing:
            self._capture_buf += chunk
            result = self._try_finish_capture()
            if result is not None:
                prefix_text, tool_calls, suffix = result
                if prefix_text:
                    events.append(SieveEvent("text", prefix_text))
                if tool_calls:
                    events.append(SieveEvent("tool_calls", tool_calls))
                if suffix:
                    self._pending = suffix
                self._capture_buf = ""
                self._capturing = False
                self._tool_start_idx = -1
                # 继续处理 suffix 可能含的新内容
                if suffix:
                    events.extend(self.feed(""))
            return events

        # 正常模式：检查 chunk 是否触发了工具调用检测
        self._pending += chunk
        start_idx = self._find_tool_start(self._pending)

        if start_idx >= 0:
            # 找到工具调用起始标记
            prefix = self._pending[:start_idx]
            rest = self._pending[start_idx:]
            self._pending = ""

            if prefix:
                events.append(SieveEvent("text", prefix))

            # 进入捕获模式
            self._capture_buf = rest
            self._capturing = True
            self._tool_start_idx = 0

            # 立即尝试完成捕获
            result = self._try_finish_capture()
            if result is not None:
                prefix_text, tool_calls, suffix = result
                if prefix_text:
                    events.append(SieveEvent("text", prefix_text))
                if tool_calls:
                    events.append(SieveEvent("tool_calls", tool_calls))
                if suffix:
                    self._pending = suffix
                self._capture_buf = ""
                self._capturing = False
                self._tool_start_idx = -1
        else:
            # 没有工具调用，安全释放
            safe, hold = self._split_safe(self._pending)
            if safe:
                events.append(SieveEvent("text", safe))
            self._pending = hold

        return events

    def flush(self) -> List[SieveEvent]:
        """流结束时调用，释放所有缓冲"""
        events: List[SieveEvent] = []

        if self._capturing:
            # 捕获模式下未完成 → 先尝试解析，失败则释放为正文
            result = self._try_finish_capture()
            if result is not None:
                prefix_text, tool_calls, suffix = result
                if prefix_text:
                    events.append(SieveEvent("text", prefix_text))
                if tool_calls:
                    events.append(SieveEvent("tool_calls", tool_calls))
                if suffix:
                    events.append(SieveEvent("text", suffix))
            else:
                # 无法完成 → 全部释放为正文
                if self._capture_buf:
                    events.append(SieveEvent("text", self._capture_buf))
            self._capture_buf = ""
            self._capturing = False
            self._tool_start_idx = -1

        if self._pending:
            events.append(SieveEvent("text", self._pending))
            self._pending = ""

        return events

    # ── 内部方法 ──────────────────────────────────────────

    def _find_tool_start(self, text: str) -> int:
        """查找工具调用起始标记的位置，跳过代码块内的"""
        idx = -1
        for tag in self._TOOL_STARTS:
            pos = text.find(tag)
            if pos >= 0 and (idx < 0 or pos < idx):
                idx = pos
        return idx

    def _split_safe(self, text: str) -> Tuple[str, str]:
        """分离安全文本和可能触发工具调用的尾部"""
        # 不应该扣留的安全标签前缀（含闭合标签）
        _SAFE_TAGS = ("<think", "<thinking", "<th", "<thi", "<thin", "<thinki",
                      "</think", "</thinking", "</th", "</thi", "</thin", "</thinki")
        
        for i in range(len(text) - 1, max(len(text) - 20, -1), -1):
            ch = text[i]
            if ch not in ("T", "t", "<", "[", "O", "o", "F", "f", "C", "c", "|"):
                continue
            tail = text[i:]
            tail_lower = tail.lower()
            # 跳过 think/thinking 相关前缀
            if any(t.startswith(tail_lower) for t in _SAFE_TAGS):
                return text, ""
            # 检查是否可能是工具调用标记前缀
            for tag in self._TOOL_STARTS:
                if tag.startswith(tail) or tail == tag[:len(tail)]:
                    return text[:i], tail
        return text, ""

    def _try_finish_capture(self):
        """尝试从捕获缓冲中完成工具调用解析。
        成功返回 (prefix_text, tool_calls, suffix)，失败返回 None。
        tool_calls 列表可为空（表示有工具标记但未匹配到工具）。
        """
        if not self._capture_buf or not self.parse_fn:
            return None

        # 检查是否已包含完整的工具调用块
        # 对 TOOL_CALL 文本格式：检测换行或双换行作为结束
        # 对 XML 格式：检测闭合标签
        if not self._is_capture_complete():
            return None

        # 解析
        result = self.parse_fn(self._capture_buf)
        if result is None:
            return None

        # 兼容不同 parse_fn 的返回格式
        if isinstance(result, tuple):
            tool_calls = result[0]
            cleaned = result[1] if len(result) > 1 else ""
        elif isinstance(result, list):
            tool_calls = result
            cleaned = ""
        else:
            return None

        # 分离 prefix 和 suffix
        prefix, suffix = self._extract_non_tool_parts(self._capture_buf)

        if tool_calls:
            return (prefix or "", tool_calls, suffix or "")
        else:
            # 有工具标记但解析出来没有调用 → 全部释放为正文
            return (self._capture_buf, None, "")

    def _is_capture_complete(self) -> bool:
        """判断捕获缓冲是否包含完整的工具调用块"""
        buf = self._capture_buf

        # TOOL_CALL: 文本格式 — 以换行结束
        if buf.lstrip().upper().startswith("TOOL_CALL:"):
            return "\n" in buf

        # [调用工具:] 中文格式
        if "[调用工具:" in buf:
            return "\n" in buf or "]" in buf

        # XML 格式 — 需要闭合标签
        if buf.lstrip().startswith("<"):
            # MiMo 原生格式: <function=NAME>...</function>
            # 注意：如果还有 <tool_call 未闭合，等 </tool_call> 而不仅是 </function>
            if "<function=" in buf:
                if "<tool_call" in buf and "</tool_call>" not in buf:
                    return False  # tool_call 还没闭合，继续等
                if "</function>" in buf:
                    return True
            # 标准格式
            if "<tool_call" in buf and "</tool_call>" in buf:
                return True
            if "<function_call" in buf and "</function_call>" in buf:
                return True
            if "<tool_calls>" in buf and "</tool_calls>" in buf:
                return True
            if "<|DSML|tool_calls>" in buf and "</|DSML|tool_calls>" in buf:
                return True
            return False

        return False

    def _extract_non_tool_parts(self, text: str) -> Tuple[str, str]:
        """提取工具调用前后的非工具文本"""
        # 找到工具调用的起始和结束位置
        start = -1
        end = -1

        for tag in self._TOOL_STARTS:
            pos = text.find(tag)
            if pos >= 0 and (start < 0 or pos < start):
                start = pos

        if start < 0:
            return text, ""

        prefix = text[:start]
        rest = text[start:]

        # 找到结束位置
        if rest.lstrip().upper().startswith("TOOL_CALL:"):
            nl = rest.find("\n")
            if nl >= 0:
                end = start + nl + 1
        elif "[调用工具:" in rest:
            end_pair = rest.find("]")
            if end_pair >= 0:
                end = start + end_pair + 1
        elif "<tool_call" in rest:
            close_pos = rest.find("</tool_call>")
            if close_pos >= 0:
                end = start + close_pos + len("</tool_call>")
        elif "<function=" in rest:
            close_pos = rest.find("</function>")
            if close_pos >= 0:
                end = start + close_pos + len("</function>")
        elif "<function_call" in rest:
            close_pos = rest.find("</function_call>")
            if close_pos >= 0:
                end = start + close_pos + len("</function_call>")
        elif "<tool_calls>" in rest:
            close_pos = rest.find("</tool_calls>")
            if close_pos >= 0:
                end = start + close_pos + len("</tool_calls>")
        elif "<|DSML|tool_calls>" in rest:
            close_pos = rest.find("</|DSML|tool_calls>")
            if close_pos >= 0:
                end = start + close_pos + len("</|DSML|tool_calls>")

        if end < 0:
            return prefix, ""

        suffix = text[end:]
        return prefix, suffix
