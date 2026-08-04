"""
工具调用模块 — MiMo2API

将 OpenAI function calling 格式转译为 MiMo 可理解的 MiMoML 提示词，
并从 MiMo 的纯文本响应中解析回结构化 tool_call。

5 重提取策略 + camelCase 全链路匹配 + 防御性编程。
"""

from __future__ import annotations

import re
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple, Set

__all__ = [
    "build_tool_prompt",
    "get_tool_names",
    "extract_tool_call",
    "normalize_tool_call",
    "clean_tool_text",
]

# ─── 内部常量 ─────────────────────────────────────────────────

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


# ─── 安全取值 ─────────────────────────────────────────────────

def _safe_get(d: Any, key: str, default: Any = None) -> Any:
    """安全取值 — 兼容 dict、pydantic model、任意对象。"""
    if d is None:
        return default
    if isinstance(d, dict):
        return d.get(key, default)
    return getattr(d, key, default)


# ─── 构建工具提示词 ──────────────────────────────────────────

def build_tool_prompt(tools: List[Dict[str, Any]], passthrough: bool = False) -> str:
    """构建 MiMoML 工具提示词，动态提取客户端 tools 的名称和描述。

    passthrough=True 时跳过格式说明书，直接嵌入原始工具定义 JSON，
    适合 Roo Code / Cline 等自带工具定义的客户端，减少格式冲突。
    """
    if not tools:
        return ""

    if passthrough:
        # 透传模式：跳过冗长的 MiMoML 格式说明书，直接嵌入原始工具定义
        # 用更简洁的指令让模型自己决定用什么格式输出工具调用
        import json
        tools_json = json.dumps(tools, indent=2, ensure_ascii=False)
        return (
            "You have the following tools available. "
            "Use your native tool-calling format when you need to invoke one.\n\n"
            f"<tools>\n{tools_json}\n</tools>\n\n"
            "When you use a tool, use whatever tool call format you normally use "
            "(TOOL_CALL:, <|MiMoML|tool_calls>, or the standard format you prefer)."
        )


    prompt = """TOOL CALL FORMAT — FOLLOW EXACTLY:

<|MiMoML|tool_calls>
  <|MiMoML|invoke name="TOOL_NAME_HERE">
    <|MiMoML|parameter name="PARAMETER_NAME"><![CDATA[PARAMETER_VALUE]]></|MiMoML|parameter>
  </|MiMoML|invoke>
</|MiMoML|tool_calls>

RULES:
1) Use the <|MiMoML|tool_calls> wrapper format.
2) Put one or more <|MiMoML|invoke> entries under a single <|MiMoML|tool_calls> root.
3) Put the tool name in the invoke name attribute: <|MiMoML|invoke name="TOOL_NAME">.
4) All string values must use <![CDATA[...]]>, even short ones.
5) Every top-level argument must be a <|MiMoML|parameter name="ARG_NAME">...</|MiMoML|parameter> node.
6) Objects use nested XML elements. Arrays may repeat <item> children.
7) Numbers, booleans, and null stay plain text.
8) Use only the parameter names in the tool schema. Do not invent fields.
9) Do NOT wrap XML in markdown fences. Do NOT output explanations or role markers.
10) If you call a tool, the first non-whitespace characters must be exactly <|MiMoML|tool_calls>.
11) Never omit the opening <|MiMoML|tool_calls> tag.

【WRONG — Do NOT do these】:

Wrong 1 — mixed text after XML:
  <|MiMoML|tool_calls>...</|MiMoML|tool_calls> I hope this helps.

Wrong 2 — Markdown code fences:
  ```xml
  <|MiMoML|tool_calls>...</|MiMoML|tool_calls>
  ```

Wrong 3 — missing opening wrapper:
  <|MiMoML|invoke name="TOOL_NAME">...</|MiMoML|invoke>
  </|MiMoML|tool_calls>

Remember: The ONLY valid way to use tools is the <|MiMoML|tool_calls>...</|MiMoML|tool_calls> block.

"""

    prompt += "可用工具:\n"
    for tool in tools:
        func = _safe_get(tool, "function", default={})
        name = _safe_get(func, "name", default="") or _safe_get(tool, "name", default="")
        desc = _safe_get(func, "description", default="") or _safe_get(tool, "description", default="")
        desc = desc.split("\n")[0].strip()
        if name:
            prompt += f"  - {name}: {desc}\n" if desc else f"  - {name}\n"

    return prompt



# ─── 提取工具名列表 ──────────────────────────────────────────

def get_tool_names(tools: List[Dict[str, Any]]) -> List[str]:
    """从 tools 列表提取所有 function name。"""
    names = []
    for tool in tools or []:
        # 兼容两种格式：Chat Completions (tool.function.name) 和 Responses API (tool.name)
        func = _safe_get(tool, "function", default={})
        name = _safe_get(func, "name", default=None) or _safe_get(tool, "name", default=None)
        if name:
            names.append(str(name))
    return names


# ─── camelCase 工具名解析 ────────────────────────────────────

def _resolve_tool_name(name: str, tool_names: List[str]) -> Optional[str]:
    """将任意形式的工具名解析为规范的 snake_case。

    4 级匹配：
      1. 直接匹配 name in tool_names
      2. 大小写不敏感匹配
      3. camelCase -> snake_case 转换（getTimeInfo -> get_time_info）
      4. 转换后大小写不敏感匹配
    """
    if not name or not tool_names:
        return None

    # 1. 直接匹配
    if name in tool_names:
        return name

    # 2. 大小写不敏感
    name_lower = name.lower()
    for tn in tool_names:
        if tn.lower() == name_lower:
            return tn

    # 3. camelCase -> snake_case
    snake = re.sub(r'(?<=[a-z0-9])([A-Z])', r'_\1', name).lower()
    if snake in tool_names:
        return snake

    # 4. snake_case 大小写不敏感
    for tn in tool_names:
        if tn.lower() == snake:
            return tn

    return None


# ─── 主入口：从文本中提取工具调用 ──────────────────────────

def extract_tool_call(
    text: str, tool_names: List[str]
) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """从 MiMo 输出文本中提取工具调用。

    5 重策略（按优先级）：
      0. <|MiMoML|tool_calls> XML — MiMoML 格式（最高优先级）
      1. TOOL_CALL: name(args)  — 旧格式兜底
      2. JSON {"name":"x","arguments":{...}} — 内嵌 JSON
      3. <tool_call> XML — MiMo 原生格式
      4. <function_call> JSON+XML

    Returns:
        (tool_calls_list_or_None, cleaned_text)
    """
    if not text or not tool_names:
        return None, text

    text = text.replace("\x00", "")

    # 策略0: MiMoML 格式（最高优先级）
    tc = _extract_mimoml_tool_call(text, tool_names)
    if tc:
        return tc, clean_tool_text(text)

    # 策略1: TOOL_CALL: name(args)
    tc = _extract_tool_call_pattern(text, tool_names)
    if tc:
        return tc, clean_tool_text(text)

    # 策略2: JSON 格式
    tc = _extract_json_tool_call(text, tool_names)
    if tc:
        return tc, clean_tool_text(text)

    # 策略3: <tool_call> XML
    tc = _extract_xml_tool_call(text, tool_names)
    if tc:
        return tc, clean_tool_text(text)

    # 策略4: <function_call> JSON+XML
    tc = _extract_function_call_json(text, tool_names)
    if tc:
        return tc, clean_tool_text(text)

    # 无工具调用：若含 MiMoML / 工具标记残留，仍清理再返回
    if _has_ml_keyword(text.lower()) or _has_tool_markers(text):
        return None, clean_tool_text(text)
    return None, text


def _has_tool_markers(text: str) -> bool:
    """检测文本是否包含工具调用标记（MiMoML、XML 等）。"""
    markers = [
        "TOOL_CALL:", "<tool_call", "<function_call", "<function=",
        "<|MiMoML|", "<tool_calls>", "<invoke",
    ]
    text_lower = text.lower()
    return any(m.lower() in text_lower for m in markers)


# ─── 策略0: MiMoML 格式（最高优先级）─────────────────────

def _extract_mimoml_tool_call(
    text: str, tool_names: List[str]
) -> Optional[List[Dict[str, Any]]]:
    """匹配 MiMoML 格式:
    <|MiMoML|tool_calls>
      <|MiMoML|invoke name="TOOL">
        <|MiMoML|parameter name="X"><![CDATA[V]]></|MiMoML|parameter>
      </|MiMoML|invoke>
    </|MiMoML|tool_calls>
    """
    if not (_MIMOML_KEYWORD in text.lower() or
            _DSML_KEYWORD in text.lower() or
            "function_calls" in text.lower() or
            "tool_calls" in text.lower() or
            "invoke" in text.lower()):
        return None

    # 松散 CDATA 修复：处理未闭合的 CDATA
    text = _sanitize_loose_cdata(text)

    # 缺失开标签修复：有关闭标签但缺开头 → 补回 <|MiMoML|tool_calls>
    has_closing = ("</|MiMoML|tool_calls>" in text or
                   "</mimoml-tool_calls>" in text.lower() or
                   "</tool_calls>" in text.lower())
    has_opening = ("<|MiMoML|tool_calls" in text.replace(' ', '').replace('｜','|').replace('||','|') or
                   "<|MiMoML|function_calls" in text.replace(' ', '').replace('｜','|').replace('||','|') or
                   "<mimoml-tool_calls" in text.lower() or
                   "<mimoml-function_calls" in text.lower() or
                   "<tool_calls>" in text.lower() or
                   "<function_calls>" in text.lower())
    if has_closing and not has_opening:
        text = "<|MiMoML|tool_calls>\n" + text

    normalized = strip_mimoml(text)
    tool_calls = []

    tc_pattern = re.compile(r"<(?:tool_calls|function_calls)>(.*?)</(?:tool_calls|function_calls)>", re.DOTALL | re.IGNORECASE)
    tc_single = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL | re.IGNORECASE)

    blocks = [(m.group(1), m.start(), m.end()) for m in tc_pattern.finditer(normalized)]
    if not blocks:
        blocks = [(m.group(1), m.start(), m.end()) for m in tc_single.finditer(normalized)]

    # 也处理裸 <invoke>（无 <tool_calls> 包裹），模型有时省略外层
    if not blocks:
        invoke_bare = re.compile(
            r"<invoke\s*name=[\"']([^\"']+)[\"'][^>]*>(.*?)</invoke>",
            re.DOTALL | re.IGNORECASE,
        )
        # Fallback: invokename= (missing space)
        invoke_bare_nospace = re.compile(
            r"<invokename=[\"']([^\"']+)[\"']>(.*?)</invoke>",
            re.DOTALL | re.IGNORECASE,
        )
        for m in list(invoke_bare.finditer(normalized)) + list(invoke_bare_nospace.finditer(normalized)):
            name = m.group(1).strip()
            if _is_inside_think(normalized, m.start()):
                continue
            inner = m.group(2)
            args = _parse_mimoml_parameters(inner)
            resolved = _resolve_tool_name(name, tool_names)
            tc = normalize_tool_call({"name": resolved, "arguments": args}) if resolved else None
            if tc:
                tool_calls.append(tc)

    for block_text, _, _ in blocks:
        invoke_pattern = re.compile(
            r"<invoke\s*name=[\"']([^\"']+)[\"'][^>]*>(.*?)</invoke>",
            re.DOTALL | re.IGNORECASE,
        )
        # Fallback: invokename= (missing space)
        invoke_nospace = re.compile(
            r"<invokename=[\"']([^\"']+)[\"']>(.*?)</invoke>",
            re.DOTALL | re.IGNORECASE,
        )
        for m in list(invoke_pattern.finditer(block_text)) + list(invoke_nospace.finditer(block_text)):
            name = m.group(1).strip()
            inner = m.group(2)
            args = _parse_mimoml_parameters(inner)
            resolved = _resolve_tool_name(name, tool_names)
            tc = normalize_tool_call({"name": resolved, "arguments": args}) if resolved else None
            if tc:
                tool_calls.append(tc)

        # Fallback: invokes without closing </invoke> (model bug — MiMo sometimes omits closing tags)
        if not tool_calls:
            # Match <invoke name="X">... up to next <invoke, </tool_calls>, or end
            invoke_open = re.compile(
                r"<invoke\s*name=[\"']([^\"']+)[\"'][^>]*>",
                re.IGNORECASE,
            )
            open_matches = list(invoke_open.finditer(block_text))
            for i, m in enumerate(open_matches):
                name = m.group(1).strip()
                if not name:
                    continue  # 跳过空 name 的 invoke
                start = m.end()
                # 找到下一个 <invoke 或 </tool_calls> 或文本末尾
                end = len(block_text)
                next_invoke = re.search(r"<invoke\s*name=", block_text[start:], re.IGNORECASE)
                close_tc = block_text.find("</tool_calls>", start)
                if next_invoke:
                    end = start + next_invoke.start()
                if close_tc != -1 and close_tc < end:
                    end = close_tc
                inner = block_text[start:end]
                args = _parse_mimoml_parameters(inner)
                resolved = _resolve_tool_name(name, tool_names)
                if resolved:
                    tc = normalize_tool_call({"name": resolved, "arguments": args})
                    if tc:
                        tool_calls.append(tc)

    return tool_calls if tool_calls else None


# ─── 策略1: TOOL_CALL: name(...) ────────────────────────────

def _extract_tool_call_pattern(
    text: str, tool_names: List[str]
) -> Optional[List[Dict[str, Any]]]:
    """匹配 TOOL_CALL: name(args) 或 TOOL_CALL: name{...}"""
    results = []
    idx = 0
    while idx < len(text):
        m = re.search(
            r"(?:^|\n)\s*TOOL_CALL:\s*(\w+)\s*\(",
            text[idx:], re.IGNORECASE
        )
        if not m:
            break

        fname = m.group(1)
        if _is_inside_think(text, idx + m.start()):
            idx += m.end()
            continue

        paren = idx + m.end() - 1
        depth = 1
        in_s = False
        esc = False
        end = -1
        for i in range(paren + 1, len(text)):
            c = text[i]
            if esc:
                esc = False
                continue
            if c == "\\" and in_s:
                esc = True
                continue
            if c == '"':
                in_s = not in_s
                continue
            if in_s:
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break

        if end == -1:
            break

        args_raw = text[paren + 1:end]
        args = _parse_function_args(args_raw)

        resolved = _resolve_tool_name(fname, tool_names)
        if resolved:
            results.append({"name": resolved, "arguments": args})

        idx = end + 1

    if results:
        return [normalize_tool_call(tc) for tc in results if normalize_tool_call(tc)]
    return None


# ─── 策略2: JSON {"name":"x","arguments":{...}} ─────────────

def _extract_json_tool_call(
    text: str, tool_names: List[str]
) -> Optional[List[Dict[str, Any]]]:
    """从文本中提取 JSON 格式的工具调用。"""
    # 先尝试用 _find_balanced_json 找到所有可能的 JSON 对象
    start = 0
    while True:
        brace = text.find("{", start)
        if brace == -1:
            break

        if _is_inside_think(text, brace):
            start = brace + 1
            continue

        js = _find_balanced_json(text, brace)
        if not js:
            start = brace + 1
            continue

        try:
            obj = json.loads(js)
        except (json.JSONDecodeError, ValueError):
            start = brace + 1
            continue

        # 检查是否是工具调用 JSON
        name = obj.get("name") or _safe_get(obj.get("function", {}), "name")
        resolved = _resolve_tool_name(name, tool_names) if name else None
        if resolved:
            args = obj.get("arguments") or obj.get("parameters") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    pass
            tc = normalize_tool_call({"name": resolved, "arguments": args})
            if tc:
                return [tc]

        # 继续从当前 JSON 块之后搜索，避免重复命中同一个 "{" 导致死循环。
        start = brace + len(js)


# ─── 策略3 + 策略3.5: <tool_call> XML（MiMo 原生 + Roo Code 格式）───

def _extract_xml_tool_call(
    text: str, tool_names: List[str]
) -> Optional[List[Dict[str, Any]]]:
    """匹配 <tool_call> XML 格式，支持两种变体：

    MiMo 原生: <tool_call><function=NAME><parameter=K>V</parameter>...</function></tool_call>
    Roo Code:  <tool_call><tool_name>NAME</tool_name><PARAM>V</PARAM></tool_call>
    """
    tc_pattern = r"<tool_call>(.*?)</tool_call>"
    m = re.search(tc_pattern, text, re.DOTALL | re.IGNORECASE)
    if not m:
        return None

    if _is_inside_think(text, m.start()):
        return None

    inner = m.group(1)

    # 尝试 MiMo 原生格式: <function=NAME>
    func_pattern = r"<function=(\w+)>(.*?)</function>"
    fm = re.search(func_pattern, inner, re.DOTALL | re.IGNORECASE)
    if fm:
        name = fm.group(1).strip()
        func_body = fm.group(2)
        resolved = _resolve_tool_name(name, tool_names)
        if not resolved:
            return None
        # 提取 <parameter=KEY>VALUE</parameter>
        args = {}
        param_pattern = r"<parameter=(\w+)>(.*?)</parameter>"
        for pm in re.finditer(param_pattern, func_body, re.DOTALL | re.IGNORECASE):
            key = pm.group(1).strip()
            val = pm.group(2).strip()
            args[key] = _auto_type(val)
        tc = normalize_tool_call({"name": resolved, "arguments": args})
        return [tc] if tc else None

    # 回退：<function>NAME</function>（畸形闭合）
    content_pattern = r"<function>(.*?)(?:</function>|$)"
    fm = re.search(content_pattern, inner, re.DOTALL | re.IGNORECASE)
    if fm:
        name = fm.group(1).strip().rstrip(">")
        resolved = _resolve_tool_name(name, tool_names)
        if not resolved:
            return None
        args = {}
        param_pattern = r"<parameter=(\w+)>(.*?)</parameter>"
        for pm in re.finditer(param_pattern, inner, re.DOTALL | re.IGNORECASE):
            key = pm.group(1).strip()
            val = pm.group(2).strip()
            args[key] = _auto_type(val)
        tc = normalize_tool_call({"name": resolved, "arguments": args})
        return [tc] if tc else None

    # Roo Code 格式: <tool_name>NAME</tool_name> + 参数名即标签
    tn_pattern = r"<tool_name>(.*?)</tool_name>"
    tn_m = re.search(tn_pattern, inner, re.DOTALL | re.IGNORECASE)
    if tn_m:
        name = tn_m.group(1).strip()
        resolved = _resolve_tool_name(name, tool_names)
        if not resolved:
            return None
        # 提取所有 <PARAM_NAME>VALUE</PARAM_NAME>（排除 <tool_name> 本身）
        args = {}
        rc_param_pattern = r"<(\w+)>(.*?)</\1>"
        for pm in re.finditer(rc_param_pattern, inner, re.DOTALL):
            pname = pm.group(1).strip().lower()
            pval = pm.group(2).strip()
            if pname == "tool_name":
                continue
            args[pname] = _auto_type(pval)
        tc = normalize_tool_call({"name": resolved, "arguments": args})
        return [tc] if tc else None

    return None


# ─── 策略5: <function_call> JSON+XML ────────────────────────

def _extract_function_call_json(
    text: str, tool_names: List[str]
) -> Optional[List[Dict[str, Any]]]:
    """匹配 <function_call>{"name":"x","arguments":{...}}</function_call>"""
    fc_pat = r"<function_calls?>(.*?)</function_calls?>"
    fc_m = re.search(fc_pat, text, re.DOTALL)
    if not fc_m:
        return None

    if _is_inside_think(text, fc_m.start()):
        return None

    inner = fc_m.group(1)
    for block in re.split(r"</function_call>", inner):
        if not block.strip():
            continue
        block = re.sub(r"^.*?<function_call>", "", block, flags=re.DOTALL).strip()
        if not block:
            continue
        js_start = block.find("{")
        if js_start == -1:
            continue
        js = _find_balanced_json(block, js_start)
        if js:
            try:
                data = json.loads(js)
                name = data.get("name", "")
                resolved = _resolve_tool_name(name, tool_names) if name else None
                if resolved:
                    args = data.get("arguments", {})
                    tc = normalize_tool_call({"name": resolved, "arguments": args})
                    if tc:
                        return [tc]
            except (json.JSONDecodeError, AttributeError):
                pass

    return None


# ─── 标准化工具调用为 OpenAI 格式 ──────────────────────────

def normalize_tool_call(raw: Any) -> Optional[Dict[str, Any]]:
    """将各种格式的 tool_call 标准化为 OpenAI 格式。

    OpenAI 格式:
        {
            "id": "call_xxx",
            "type": "function",
            "function": {
                "name": "...",
                "arguments": "{...}"   # JSON 字符串
            }
        }
    """
    if not raw:
        return None

    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    if not isinstance(raw, dict):
        return None

    # 已经是标准格式
    if "function" in raw and isinstance(raw.get("function"), dict):
        func = raw["function"]
        if "name" in func and func["name"]:
            if "id" not in raw:
                raw["id"] = f"call_{uuid.uuid4().hex[:24]}"
            if "type" not in raw:
                raw["type"] = "function"
            # 确保 arguments 是字符串
            if "arguments" in func and not isinstance(func["arguments"], str):
                func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)
            elif "arguments" not in func:
                func["arguments"] = "{}"
            _fixup_skill_params(func)
            return raw

    # 扁平格式: {"name": "xxx", "arguments": {...}}
    name = raw.get("name")
    if not name:
        return None

    args = raw.get("arguments") or raw.get("parameters") or raw.get("args") or {}
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False)

    func = {"name": name, "arguments": args}
    _fixup_skill_params(func)
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": func,
    }


# ─── 参数修复：skill_name → name 兼容 ─────────────────────

_SKILL_TOOLS = {"skill_view", "skill_manage", "use_skill"}

def _fixup_skill_params(func: Dict[str, Any]) -> None:
    """修复 skill 工具的常见参数名偏差。

    MiMo 模型偶尔把 skill_view 的 name 参数写成 skill_name，
    这里自动重映射，兼容两种写法。
    """
    if func.get("name") not in _SKILL_TOOLS:
        return
    try:
        args = func["arguments"]
        if isinstance(args, str):
            args = json.loads(args)
            is_str = True
        else:
            is_str = False
        if "skill_name" in args and "name" not in args:
            args["name"] = args.pop("skill_name")
            if is_str:
                func["arguments"] = json.dumps(args, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError, KeyError):
        pass


# ─── 清理工具文本 ──────────────────────────────────────────

def clean_tool_text(text: str) -> str:
    """清理文本中的工具调用残留痕迹。

    移除所有已知格式的标签，保留纯自然语言内容。
    """
    if not text:
        return text

    # TOOL_CALL: xxx 行
    text = re.sub(r"TOOL_CALL:.*$", "", text, flags=re.MULTILINE | re.IGNORECASE)
    # TOOL_CALL: name(...) 内联
    text = re.sub(
        r"TOOL_CALL:\s*\w+\s*\([^)]*(?:\([^)]*\)[^)]*)*\)",
        "", text, flags=re.IGNORECASE
    )
    # <function_call> / <function_calls> 标签
    text = re.sub(r"</?function_calls?>", "", text)
    # MiMoML 标签残留（修复 \\| → \| 使正则正确匹配 MiMoML 标签）
    text = re.sub(r"</?\|MiMoML\|[^>]*>", "", text)
    text = re.sub(r"<tool_calls?>.*?</tool_calls?>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # 同时匹配 <invoke> 和 <|MiMoML|invoke> 两种形式
    text = re.sub(r"<(?:[|]MiMoML[|])?invoke[^>]*>.*?</(?:[|]MiMoML[|])?invoke>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # 同时匹配 <parameter> 和 <|MiMoML|parameter> 两种形式
    text = re.sub(r"<(?:[|]MiMoML[|])?parameter[^>]*>.*?</(?:[|]MiMoML[|])?parameter>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<!\[CDATA\[.*?\]\]>", "", text, flags=re.DOTALL)
    # <tool_call>...</tool_call>
    text = re.sub(
        r"<tool_call>.*?</tool_call>", "",
        text, flags=re.DOTALL | re.IGNORECASE
    )
    # <function=xxx>...</function>
    text = re.sub(
        r"<function=\w+>.*?</function>", "",
        text, flags=re.DOTALL | re.IGNORECASE
    )
    # <parameter=xxx>...</parameter>
    text = re.sub(
        r"<parameter=\w+>.*?</parameter>", "",
        text, flags=re.DOTALL | re.IGNORECASE
    )
    # python调用工具(xxx) 残留
    text = re.sub(r"</?function=\w+>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<parameter=\w+>", "", text)
    text = re.sub(r"</parameter>", "", text)
    # JSON tool_call 块
    text = re.sub(
        r"```(?:json)?\s*\n?\s*\{.*?\"tool_call\".*?\}\s*\n?\s*```",
        "", text, flags=re.DOTALL
    )
    # 空代码块
    text = re.sub(r"```\w*\s*\n?\s*```", "", text)
    # 多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text  # 不 strip — 流式分块中 \n 需要保留


# ═══════════════════════════════════════════════════════════
# 内部辅助函数
# ═══════════════════════════════════════════════════════════

def _find_balanced_json(text: str, start: int) -> str:
    """从 start 位置查找配对的 JSON {}，处理字符串转义。"""
    if start >= len(text) or text[start] != "{":
        return ""

    depth = 0
    in_str = False
    esc = False

    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\" and in_str:
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return ""


def _parse_function_args(raw: str) -> Dict[str, Any]:
    """解析函数参数字符串到 dict。

    支持格式:
      key="value", key2=123
      key=value
      {"json": "object"}
    """
    raw = raw.strip()
    if not raw:
        return {}

    # 已经是 JSON 对象
    if raw.startswith("{") and raw.endswith("}"):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass

    # key=value 格式，用智能分割处理嵌套
    args = {}
    for pair in _smart_split(raw, ","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            args[k] = _auto_type(v)

    if args:
        return args

    # 无法解析，返回原文本作为 input
    return {"input": raw}


def _smart_split(text: str, sep: str) -> List[str]:
    """智能分割字符串，正确处理括号嵌套和引号。"""
    parts = []
    current = []
    dp = db = dbr = 0  # 括号深度
    in_str = False
    esc = False

    for ch in text:
        if esc:
            current.append(ch)
            esc = False
            continue
        if ch == "\\" and in_str:
            current.append(ch)
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            current.append(ch)
            continue
        if in_str:
            current.append(ch)
            continue
        if ch == "(":
            dp += 1
        elif ch == ")":
            dp -= 1
        elif ch == "[":
            db += 1
        elif ch == "]":
            db -= 1
        elif ch == "{":
            dbr += 1
        elif ch == "}":
            dbr -= 1
        elif ch == sep and dp == 0 and db == 0 and dbr == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)

    if current:
        parts.append("".join(current).strip())

    return parts


def _auto_type(val: str) -> Any:
    """自动推断值类型。"""
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    if val.lower() in ("null", "none"):
        return None
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def _is_inside_think(text: str, pos: int) -> bool:
    """检查 pos 是否在 <think>...</think> 块内部。"""
    sf = 0
    while True:
        s = text.find(THINK_OPEN, sf)
        if s == -1:
            break
        e = text.find(THINK_CLOSE, s + 7)
        if e == -1:
            return pos >= s
        if s <= pos < e + 8:
            return True
        sf = e + 8
    return False


# ═══════════════════════════════════════════════════════════
# MiMoML 噪声容错前缀：重复 <、|、｜、空格、mimoml 关键字
_MIMOML_NOISE_CHARS = set("|｜ \t\r\n")
_MIMOML_KEYWORD = "mimoml"
_MIMOML_KEYWORD_LEN = len(_MIMOML_KEYWORD)
# DSML 变体（模型有时用 DSML 替代 MiMoML）
_DSML_KEYWORD = "dsml"
_DSML_KEYWORD_LEN = len(_DSML_KEYWORD)

# 检测文本是否含任一 ML 关键字
def _has_ml_keyword(text: str) -> bool:
    t = text.lower()
    return _MIMOML_KEYWORD in t or _DSML_KEYWORD in t or "function_calls" in t

# 连字符变体：mimoml-tool_calls → tool_calls
_MIMOML_HYPHENATED = {
    "mimoml-tool_calls": "tool_calls",
    "mimoml-tool-calls": "tool_calls",
    "mimoml-invoke": "invoke",
    "mimoml-parameter": "parameter",
}

_MIMOML_TAG_NAMES = {"tool_calls", "function_calls", "invoke", "parameter", "invokename"}
_CDATA_OPEN = "<![CDATA["
_CDATA_CLOSE = "]]>"


def strip_mimoml(text: str) -> str:
    """去除 MiMoML 前缀标记，转换为标准 XML，支持噪声容错。

    处理的变体：
      <|MiMoML|tool_calls>      →  <tool_calls>
      <MiMoML|tool_calls>       →  <tool_calls>     (缺开头 |)
      <<|MiMoML|tool_calls>     →  <tool_calls>     (重复 <)
      <|MiMoML tool_calls>      →  <tool_calls>     (空格)
      <｜MiMoML｜tool_calls>     →  <tool_calls>     (全宽管道)
      <MiMoMLtool_calls>        →  <tool_calls>     (无分隔符)
      <|MiMoML|tool_calls|>     →  <tool_calls>     (尾部 |)
      <mimoml-tool_calls>       →  <tool_calls>     (连字符)

    自动跳过 markdown 围栏代码块内的 MiMoML 示例。
    """
    if not text:
        return text

    result_parts = []
    i = 0
    n = len(text)
    text_lower = text.lower()

    while i < n:
        # CDATA 块 → 原样保留
        if text[i:].startswith(_CDATA_OPEN):
            close = text.find(_CDATA_CLOSE, i + len(_CDATA_OPEN))
            if close == -1:
                result_parts.append(text[i:])
                break
            result_parts.append(text[i:close + len(_CDATA_CLOSE)])
            i = close + len(_CDATA_CLOSE)
            continue

        # 围栏代码块 → 跳过
        i, skipped = _skip_fenced_block(text, i)
        if skipped:
            result_parts.append(skipped)
            continue

        c = text[i]
        if c != '<':
            result_parts.append(c)
            i += 1
            continue

        # 找到 > 结束
        end = text.find('>', i)
        if end == -1:
            result_parts.append(text[i:])
            break

        inner = text[i + 1 : end]
        closing = inner.startswith('/')
        rest = inner[1:] if closing else inner

        j, is_mimoml = _consume_mimoml_noise(rest, text_lower, i + 1 + (1 if closing else 0))

        if is_mimoml:
            tag_name = _match_mimoml_tag(rest, j)
            if tag_name:
                rest_after = rest[tag_name[1]:]
                actual_end = end
                if rest_after.startswith('|') or rest_after.startswith('｜'):
                    rest = rest[:tag_name[1]] + rest_after[1:] if len(rest_after) == 1 else rest[:tag_name[1]] + rest_after[3:] if rest_after.startswith('｜') else rest[:tag_name[1]] + rest_after[1:]
                    new_end = text.find('>', i)
                    if new_end != -1 and new_end < end:
                        actual_end = new_end

                prefix = '</' if closing else '<'
                result_parts.append(prefix)
                result_parts.append(rest[j:])
                result_parts.append('>')
                i = actual_end + 1
                continue

        result_parts.append(text[i : end + 1])
        i = end + 1

    return ''.join(result_parts)


def _skip_fenced_block(text: str, i: int) -> Tuple[int, Optional[str]]:
    """检查当前位置是否在围栏代码块开始处。

    支持 ``` 和 ~~~ 围栏，包括嵌套围栏。
    如果是，跳过整个代码块，返回 (新位置, 跳过的文本)。
    """
    n = len(text)
    # 反引号围栏 ```
    fence_len = _count_fence(text, i, '`')
    if fence_len >= 3:
        # 找到匹配的结束围栏
        end_pos = _find_fence_close(text, i + fence_len, '`', fence_len)
        if end_pos >= 0:
            return end_pos, text[i:end_pos]

    # 波浪线围栏 ~~~
    fence_len = _count_fence(text, i, '~')
    if fence_len >= 3:
        end_pos = _find_fence_close(text, i + fence_len, '~', fence_len)
        if end_pos >= 0:
            return end_pos, text[i:end_pos]

    return i, None


def _count_fence(text: str, start: int, char: str) -> int:
    """计算从 start 开始连续 char 的数量。"""
    count = 0
    while start + count < len(text) and text[start + count] == char:
        count += 1
    return count


def _find_fence_close(text: str, start: int, char: str, min_len: int) -> int:
    """找到匹配的围栏结束位置（包括换行符之前），返回结束位置或 -1。"""
    i = start
    # 跳过 fence 开始后的行内内容（语言标识等）
    nl = text.find('\n', i)
    if nl >= 0:
        i = nl + 1
    else:
        return -1

    while i < len(text):
        nl = text.find('\n', i)
        if nl < 0:
            return -1
        line_start = nl + 1
        fence_len = _count_fence(text, line_start, char)
        if fence_len >= min_len:
            # 确保后面是换行或结束
            after = line_start + fence_len
            if after >= len(text) or text[after] == '\n' or text[after] == '\r':
                return line_start + fence_len + (1 if after < len(text) and text[after] == '\n' else 0)
        i = line_start + 1

    return -1


def _consume_mimoml_noise(rest: str, text_lower: str, abs_pos: int) -> tuple:
    """消费 MiMoML 噪声前缀，返回 (j, is_mimoml)。"""
    j = 0
    is_mimoml = False
    rest_len = len(rest)

    while j < rest_len:
        ch = rest[j]
        # 重复 < → 噪声
        if ch == '<':
            j += 1
            is_mimoml = True
            continue
        # 单字符噪声（|、空格等）
        if ch in _MIMOML_NOISE_CHARS:
            j += 1
            is_mimoml = True
            continue
        # 全宽管道 ｜(U+FF5C)
        if rest[j:].startswith('｜'):
            j += 1
            is_mimoml = True
            continue
        # mimoml / dsml 关键字
        if (rest[j:j+_MIMOML_KEYWORD_LEN].lower() == _MIMOML_KEYWORD or
                rest[j:j+_DSML_KEYWORD_LEN].lower() == _DSML_KEYWORD):
            kw_len = _DSML_KEYWORD_LEN if rest[j:j+_DSML_KEYWORD_LEN].lower() == _DSML_KEYWORD else _MIMOML_KEYWORD_LEN
            j += kw_len
            is_mimoml = True
            # 如果后面紧跟 - 或 _（连字符变体），也吞掉
            if j < rest_len and rest[j] in ('-', '_'):
                j += 1
            continue
        break

    return j, is_mimoml


def _match_mimoml_tag(rest: str, j: int) -> Optional[tuple]:
    """匹配 MiMoML 标签名，返回 (canonical_name, end_pos) 或 None。"""
    rest_len = len(rest)
    # 先检查连字符变体
    for hyphenated, canonical in _MIMOML_HYPHENATED.items():
        if rest[j:j+len(hyphenated)].lower() == hyphenated:
            return (canonical, j + len(hyphenated))

    # 标准标签名匹配
    name_end = j
    while name_end < rest_len and (rest[name_end].isalnum() or rest[name_end] == '_'):
        name_end += 1

    if name_end == j:
        return None

    tag_name = rest[j:name_end].lower()
    if tag_name in _MIMOML_TAG_NAMES:
        return (tag_name, name_end)

    return None


def _parse_mimoml_parameters(inner_text: str) -> Dict[str, Any]:
    """解析 <parameter name="x">...</parameter> 为参数 dict。

    支持：
      - CDATA 提取
      - JSON 字面量自动识别
      - <item> 子节点 → 数组
      - 嵌套 XML → 结构化对象
      - 重复 key → 自动合并为数组
      - HTML 实体解码（&lt; &gt; &amp; 等）
    """
    args: Dict[str, Any] = {}
    param_pattern = re.compile(
        r"<parameter\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)</parameter>",
        re.DOTALL | re.IGNORECASE,
    )
    for m in param_pattern.finditer(inner_text):
        key = m.group(1).strip()
        val_raw = m.group(2).strip()
        val = _parse_param_value(val_raw, param_name=key)

        # 重复 key → 合并为数组
        if key in args:
            existing = args[key]
            if isinstance(existing, list):
                existing.append(val)
            else:
                args[key] = [existing, val]
        else:
            args[key] = val

    # Fallback: <parameter=KEY>VALUE</parameter> (等号格式)
    if not args:
        eq_pattern = re.compile(
            r"<parameter=(\w+)>(.*?)</parameter>",
            re.DOTALL | re.IGNORECASE,
        )
        for m in eq_pattern.finditer(inner_text):
            key = m.group(1).strip()
            val_raw = m.group(2).strip()
            val = _parse_param_value(val_raw, param_name=key)
            if key in args:
                existing = args[key]
                if isinstance(existing, list):
                    existing.append(val)
                else:
                    args[key] = [existing, val]
            else:
                args[key] = val

    return args


def _parse_param_value(val_raw: str, param_name: str = "") -> Any:
    """解析单个 parameter 的值。"""
    if not val_raw:
        return ""

    # CDATA 提取（安全版）
    if val_raw.startswith(_CDATA_OPEN) and val_raw.endswith(_CDATA_CLOSE):
        inner = _extract_cdata_safe(val_raw)
        if _preserves_cdata_string(param_name):
            return _html_unescape(inner)
        inner = _normalize_br(inner)
        if inner.strip().startswith("<") and ">" in inner:
            parsed = _parse_structured_xml(inner)
            if parsed is not None:
                return parsed
        return _html_unescape(inner)

    # 结构化 XML
    if "<" in val_raw and ">" in val_raw:
        parsed = _parse_structured_xml(val_raw)
        if parsed is not None and parsed != {}:
            return parsed

    # JSON
    try:
        return json.loads(val_raw)
    except (json.JSONDecodeError, ValueError):
        pass

    # JSON 修复
    repaired = _repair_loose_json(val_raw)
    if repaired != val_raw:
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, ValueError):
            pass

    result = _auto_type(val_raw)
    if isinstance(result, str):
        result = _html_unescape(result)
    return result


def _html_unescape(text: str) -> str:
    """解码常见 HTML 实体。"""
    if not isinstance(text, str):
        return text
    entities = {
        "&lt;": "<", "&gt;": ">", "&amp;": "&", "&quot;": '"',
        "&apos;": "'", "&#39;": "'", "&#x27;": "'",
    }
    for entity, char in entities.items():
        text = text.replace(entity, char)
    return text


def _parse_structured_xml(text: str) -> Optional[Dict[str, Any]]:
    """将结构化 XML 还原为 dict/array。

    处理：
      - <item>...</item> × N → [...]（数组）
      - <key>value</key> × N → {key: value, ...}（对象）
      - 混合情况保留为字符串
    """
    text = text.strip()
    if not text or text[0] != '<':
        return None

    # 提取所有顶层子节点
    items = []
    pos = 0
    item_pattern = re.compile(r"<(\w+)(?:\s[^>]*)?>(.*?)</\1>", re.DOTALL | re.IGNORECASE)

    for m in item_pattern.finditer(text):
        tag = m.group(1).lower()
        inner = m.group(2).strip()
        # 跳过 parameter 标签（这是外层不属于结构化内容）
        if tag == "parameter":
            continue

        # 递归解析子节点
        if "<" in inner and ">" in inner:
            child = _parse_structured_xml(inner)
            if child is not None:
                items.append({tag: child} if tag != "item" else child)
            else:
                items.append({tag: _html_unescape(inner)} if tag != "item" else _html_unescape(inner))
        else:
            val = _parse_param_value(inner)
            items.append({tag: val} if tag != "item" else val)

    if not items:
        return None

    # 全是纯值（无 dict key）→ 数组
    if all(not isinstance(it, dict) for it in items):
        return items

    # 合并：提取所有 key，同名合并
    result = {}
    all_items = True
    for it in items:
        if isinstance(it, dict):
            for k, v in it.items():
                if k != "item":
                    all_items = False
                if k in result:
                    existing = result[k]
                    if isinstance(existing, list):
                        existing.append(v)
                    else:
                        result[k] = [existing, v]
                else:
                    result[k] = v
        else:
            # 纯值：合并到 "item" key
            if "item" in result:
                if isinstance(result["item"], list):
                    result["item"].append(it)
                else:
                    result["item"] = [result["item"], it]
            else:
                result["item"] = it

    # 如果所有顶层都是 <item> → 返回纯数组
    if all_items and set(result.keys()) == {"item"}:
        val = result["item"]
        return val if isinstance(val, list) else [val]

    return result if result else None


def _sanitize_loose_cdata(text: str) -> str:
    """修复未闭合的 CDATA 段。

    如果 <![CDATA[ 打开但没有 ]]> 闭合，移除开标记，
    让剩余文本仍能被正常解析。
    """
    if not text:
        return text

    text_lower = text.lower()
    result_parts = []
    i = 0

    while i < len(text):
        start = text_lower.find(_CDATA_OPEN.lower(), i)
        if start < 0:
            result_parts.append(text[i:])
            break

        result_parts.append(text[i:start])
        content_start = start + len(_CDATA_OPEN)

        # 查找闭合的 ]]>
        close = text.find(_CDATA_CLOSE, content_start)
        if close >= 0:
            # 正常闭合 → 保留完整 CDATA
            result_parts.append(text[start:close + len(_CDATA_CLOSE)])
            i = close + len(_CDATA_CLOSE)
        else:
            # 未闭合 → 移除开标记，保留内容
            result_parts.append(text[content_start:])
            break

    return ''.join(result_parts)


# ══════ JSON修复 + 空参数 + Schema + CDATA保护 + br ══════

import re as _re2
_UNQUOTED_KEY_PAT = _re2.compile(r"([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:")
_MISSING_ARR_PAT = _re2.compile(r"(:\s*)(\{(?:[^{}]|\{[^{}]*\})*\}(?:\s*,\s*\{(?:[^{}]|\{[^{}]*\})*\})+)")
_BR_PAT = _re2.compile(r"<br\s*/?>", _re2.IGNORECASE)
_CDATA_STR = {"content","file_content","text","prompt","query","command","cmd","script","code","old_string","new_string","pattern","path","file_path","result","input"}

def _repair_loose_json(s):
    s=s.strip()
    if not s: return s
    s=_UNQUOTED_KEY_PAT.sub(r'"":',s)
    s=_MISSING_ARR_PAT.sub(r'[]',s)
    return _rjb(s)

def _rjb(s):
    if '\\' not in s: return s
    r=[]; i=0
    while i<len(s):
        if s[i]=='\\':
            if i+1<len(s):
                n=s[i+1]
                if n in ('"','\\','/','b','f','n','r','t'):
                    r.append('\\');r.append(n);i+=2;continue
                if n=='u' and i+5<len(s):
                    h=s[i+2:i+6]
                    if all(c in '0123456789abcdefABCDEF' for c in h):
                        r.append('\\u');r.append(h);i+=6;continue
            r.append('\\\\');i+=1
        else: r.append(s[i]);i+=1
    return ''.join(r)

def _has_meaningful_value(v):
    if v is None: return False
    if isinstance(v,str): return v.strip()!=""
    if isinstance(v,(int,float,bool)): return True
    if isinstance(v,dict): return bool(v) and any(_has_meaningful_value(c) for c in v.values())
    if isinstance(v,list): return bool(v) and any(_has_meaningful_value(c) for c in v)
    return True

def _coerce_string_params(tcs,tools=None):
    if not tools or not tcs: return tcs
    si={}
    for t in tools:
        fn=_safe_get(t,"function") or t
        n=_safe_get(fn,"name")
        p=_safe_get(fn,"parameters")
        if n and isinstance(p,dict): si[n]=p
    for tc in tcs:
        fn=tc.get("function",{})
        nm=fn.get("name","")
        a=fn.get("arguments","{}")
        if isinstance(a,dict): args=a
        else:
            try: args=json.loads(a) if isinstance(a,str) else a
            except: continue
        sc=si.get(nm)
        if not sc: continue
        pp=sc.get("properties",{})
        if not pp: continue
        for k,v in args.items():
            pr=pp.get(k,{})
            if isinstance(pr,dict) and pr.get("type")=="string" and not isinstance(v,str) and v is not None:
                args[k]=str(v)
        fn["arguments"]=json.dumps(args,ensure_ascii=False)
    return tcs

def _preserves_cdata_string(name):
    return name.strip().lower() in _CDATA_STR

def _extract_cdata_safe(text):
    if not text: return text
    lw=text.lower(); st=lw.find("<![cdata[")
    if st<0: return text
    cs=st+len("<![CDATA[")
    lines=text[cs:].split('\n')
    inf=False; fm=""; col=[]
    for ln in lines:
        sl=ln.lstrip()
        if not inf:
            if sl.startswith('```') or sl.startswith('~~~'):
                fm=sl[:3]; inf=True
        elif sl.startswith(fm): inf=False
        if not inf and ']]>' in ln:
            ei=ln.index(']]>')
            col.append(ln[:ei])
            return '\n'.join(col)
        col.append(ln)
    return '\n'.join(col)

def _normalize_br(text):
    if not text or '<br' not in text.lower(): return text
    return _BR_PAT.sub('\n',text).replace('\r\n','\n')
