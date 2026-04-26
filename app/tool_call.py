"""工具调用模块

将 OpenAI function calling 格式转译为 MiMo 可理解的纯文本提示词，
并从 MiMo 的纯文本响应中解析回结构化 tool_call。

设计原则：
  1. 防御性编程 — 任何字段缺失/None 都不能崩溃
  2. 多策略提取 — 正则 + JSON + 关键词匹配，尽力而为
  3. 单一职责 — 每个函数做一件事
"""

from __future__ import annotations

import re
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "build_tool_prompt",
    "get_tool_names",
    "extract_tool_call",
    "normalize_tool_call",
    "clean_tool_text",
]


# ─── 构建工具提示词 ────────────────────────────────────────────

def build_tool_prompt(tools: List[Dict[str, Any]]) -> str:
    """构建工具提示词 — 含格式指令和示例。

    必须告诉模型 TOOL_CALL 输出格式，否则模型会输出各种无法解析的格式
    （尤其是看到对话历史中的 [调用工具:] 中文标签后会模仿）。
    """
    if not tools:
        return ""

    lines = []
    lines.append("## 可用工具")
    lines.append("当需要调用工具时，你必须输出以下格式（单独一行）：")
    lines.append("TOOL_CALL: 工具名(参数1=值1, 参数2=\"值2\")")
    lines.append("")
    lines.append("规则:")
    lines.append("- TOOL_CALL 必须在单独一行，不要加任何前缀或后缀")
    lines.append("- 括号内参数用逗号分隔，字符串值用双引号包裹")
    lines.append("- 整数和布尔值不要加引号")
    lines.append("- 如果需要调用工具，只输出 TOOL_CALL 行，不要同时解释")
    lines.append("- 如果不需要调用工具，正常回答即可，不要输出 TOOL_CALL")
    lines.append("")
    lines.append("示例:")
    lines.append('TOOL_CALL: get_weather(city="北京")')
    lines.append('TOOL_CALL: search(query="最新AI新闻", page=1)')
    lines.append('TOOL_CALL: calculator(expression="3+5*2")')
    lines.append("")

    for i, tool in enumerate(tools, 1):
        func = _safe_get(tool, "function", default={})
        name = _safe_get(func, "name", default=f"unknown_{i}")
        desc = _safe_get(func, "description", default="")
        params = _safe_get(func, "parameters", default=None)

        param_lines = []
        if params and isinstance(params, dict):
            props = params.get("properties") or {}
            required = set(params.get("required") or [])
            for pname, pinfo in props.items():
                if not isinstance(pinfo, dict):
                    pinfo = {}
                ptype = pinfo.get("type", "string")
                pdesc = pinfo.get("description", "")
                marker = "*" if pname in required else ""
                extra = f" — {pdesc}" if pdesc else ""
                param_lines.append(f"    {pname}{marker} ({ptype}){extra}")

        d = f" — {desc}" if desc else ""
        lines.append(f"- {name}{d}")
        if param_lines:
            lines.extend(param_lines)

    return "\n".join(lines)


# ─── 提取工具名列表 ───────────────────────────────────────────

def get_tool_names(tools: List[Dict[str, Any]]) -> List[str]:
    """从 tools 列表提取所有 function name。"""
    names = []
    for tool in tools or []:
        func = _safe_get(tool, "function", default={})
        name = _safe_get(func, "name", default=None)
        if name:
            names.append(str(name))
    return names


# ─── 从文本中提取工具调用 ──────────────────────────────────────

def extract_tool_call(
    text: str, tool_names: List[str]
) -> Tuple[Optional[Dict[str, Any]], str]:
    """从 MiMo 输出文本中提取工具调用。

    策略（按优先级）：
      1. 正则匹配 TOOL_CALL: name(...)
      2. JSON 解析 {"name": ..., "arguments": ...}
      3. 关键词匹配 (name) 或 name(...)

    Returns:
        (tool_call_dict_or_None, cleaned_text_without_tool_call)
    """
    if not text or not tool_names:
        return None, text

    # 清理 null 字节
    text = text.replace("\x00", "")

    # ── 策略1: TOOL_CALL: name(args) ──
    tc = _extract_tool_call_pattern(text, tool_names)
    if tc:
        return tc, _remove_tool_call_text(text)

    # ── 策略2: JSON 格式 ──
    tc = _extract_json_tool_call(text, tool_names)
    if tc:
        return tc, _remove_json_tool_call(text)

    # ── 策略3: 自由文本匹配 ──
    tc = _extract_freeform_tool_call(text, tool_names)
    if tc:
        return tc, _remove_tool_call_text(text)

    # ── 策略4: <tool_call> XML 标签（MiMo 原生格式）──
    # 匹配: <tool_call><function=NAME><parameter=K>V</parameter>...</function></tool_call>
    tc = _extract_xml_tool_call(text, tool_names)
    if tc:
        return tc, _remove_tool_call_text(text)

    # ── 策略5: <function_call> XML 标签（内含 JSON）──
    # 匹配: <function_call>{"name":"x","arguments":{...}}</function_call>
    fc_pat = r"<function_calls?>(.*?)</function_calls?>"
    fc_m = re.search(fc_pat, text, re.DOTALL)
    if fc_m:
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
                    if name and name in tool_names:
                        args = data.get("arguments", {})
                        tc = normalize_tool_call({"name": name, "arguments": args})
                        if tc:
                            return tc, _remove_tool_call_text(text)
                except (json.JSONDecodeError, AttributeError):
                    pass

    # ── 策略6: [调用工具: NAME] 中文格式 ──
    tc = _extract_chinese_tool_call(text, tool_names)
    if tc:
        return tc, _remove_tool_call_text(text)

    return None, text


def _extract_chinese_tool_call(
    text: str, tool_names: List[str]
) -> Optional[Dict[str, Any]]:
    """策略6: 匹配 [调用工具: NAME] 中文格式（模型从历史中学到的格式）。

    模型看到对话历史中的 [调用工具:] 标签后会模仿，输出类似：
      [调用工具: terminal]
      bash
      pwd && whoami && id

    或：
      [调用工具: read_file]
      {"path": "..."}
    """
    pat = r"\[调用工具:\s*(\w+(?:,\s*\w+)*)\]"
    m = re.search(pat, text)
    if not m:
        return None

    names = [n.strip() for n in m.group(1).split(",")]
    # 取第一个可识别的工具名
    found_name = None
    for n in names:
        if n in tool_names:
            found_name = n
            break
    if not found_name:
        return None

    # 提取标签后面的内容作为参数
    after = text[m.end():].strip()
    args = {}

    if after:
        # 尝试 JSON 格式
        if after.startswith("{"):
            js = _find_balanced_json(after, 0)
            if js:
                try:
                    args = json.loads(js)
                except json.JSONDecodeError:
                    pass
        else:
            # 简单格式：第一行当默认参数值
            first_line = after.split("\n")[0].strip()
            if first_line and not first_line.startswith("["):
                args = {"input": first_line}

    return normalize_tool_call({"name": found_name, "arguments": args})


# ─── 标准化工具调用 ────────────────────────────────────────────

def normalize_tool_call(raw: Dict[str, Any]) -> Dict[str, Any]:
    """将各种格式的 tool_call dict 标准化为 OpenAI 格式。

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
        return raw

    # 已经是标准格式
    if "function" in raw and isinstance(raw["function"], dict):
        func = raw["function"]
        if "name" in func and "arguments" in func:
            if "id" not in raw:
                raw["id"] = f"call_{uuid.uuid4().hex[:24]}"
            if "type" not in raw:
                raw["type"] = "function"
            # 确保 arguments 是字符串
            if not isinstance(func["arguments"], str):
                func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)
            return raw

    # 扁平格式: {"name": "xxx", "arguments": {...}}
    if "name" in raw:
        args = raw.get("arguments") or raw.get("parameters") or raw.get("args") or {}
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        return {
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": raw["name"],
                "arguments": args,
            },
        }

    return raw


# ─── 清理工具文本 ──────────────────────────────────────────────

def clean_tool_text(text: str) -> str:
    """清理文本中的工具调用残留痕迹。"""
    if not text:
        return text

    # 移除 TOOL_CALL: xxx 行
    text = re.sub(r"TOOL_CALL:\s*\S+.*", "", text, flags=re.MULTILINE)
    # 移除 <function_call> / <function_calls> 标签
    text = re.sub(r"</?function_calls?>", "", text)
    # 移除 <tool_call>...</tool_call> 整块
    text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # 移除 <function=xxx> 和 <parameter=xxx> 标签
    text = re.sub(r"<function=\w+>.*?</function>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<parameter=\w+>.*?</parameter>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # 移除 [调用工具: xxx] 中文格式
    text = re.sub(r"\[\s*调用工具\s*:\s*\w+(?:\s*,\s*\w+)*\s*\].*", "", text, flags=re.MULTILINE)
    # 移除多余的空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ═══════════════════════════════════════════════════════════════
# 内部辅助函数
# ═══════════════════════════════════════════════════════════════

def _safe_get(d: Any, key: str, default: Any = None) -> Any:
    """安全取值 —— 对 dict、pydantic model、任意对象都能用。"""
    if d is None:
        return default
    if isinstance(d, dict):
        return d.get(key, default)
    return getattr(d, key, default)


def _find_balanced_json(text: str, start: int) -> str:
    """从 start 位置开始查找配对的 JSON 对象 {...}，处理好字符串转义。"""
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
                return text[start : i + 1]
    return ""


def _extract_xml_tool_call(
    text: str, tool_names: List[str]
) -> Optional[Dict[str, Any]]:
    """策略4: 匹配 <tool_call><function=NAME><parameter=K>V</parameter>...</function></tool_call>"""
    # 查找 <tool_call>...</tool_call> 块
    tc_pattern = r"<tool_call>(.*?)</tool_call>"
    m = re.search(tc_pattern, text, re.DOTALL | re.IGNORECASE)
    if not m:
        return None

    inner = m.group(1)

    # 提取 <function=NAME> ... </function>
    func_pattern = r"<function=(\w+)>(.*?)</function>"
    fm = re.search(func_pattern, inner, re.DOTALL | re.IGNORECASE)
    if not fm:
        return None

    name = fm.group(1).strip()
    if name not in tool_names:
        return None

    func_body = fm.group(2)

    # 提取 <parameter=KEY>VALUE</parameter>
    args = {}
    param_pattern = r"<parameter=(\w+)>(.*?)</parameter>"
    for pm in re.finditer(param_pattern, func_body, re.DOTALL | re.IGNORECASE):
        key = pm.group(1).strip()
        val = pm.group(2).strip()
        args[key] = _auto_type(val)

    return normalize_tool_call({"name": name, "arguments": args})


def _extract_tool_call_pattern(
    text: str, tool_names: List[str]
) -> Optional[Dict[str, Any]]:
    """策略1: 匹配 TOOL_CALL: name(...) 或 TOOL_CALL: name{...}"""
    # 匹配 TOOL_CALL: xxx(...)
    pattern = r"TOOL_CALL:\s*(\w+)\s*\((.*?)\)"
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        # 尝试 TOOL_CALL: name{...}  (JSON args)
        pattern2 = r"TOOL_CALL:\s*(\w+)\s*\{(.+?)\}\s*$"
        match = re.search(pattern2, text, re.DOTALL | re.MULTILINE)

    if not match:
        return None

    name = match.group(1).strip()
    if name not in tool_names:
        return None

    args_raw = match.group(2).strip()
    args = _parse_args_text(args_raw)

    return normalize_tool_call({"name": name, "arguments": args})


def _extract_json_tool_call(
    text: str, tool_names: List[str]
) -> Optional[Dict[str, Any]]:
    """策略2: 文本中包含 JSON 工具调用。"""
    # 尝试找 JSON 块
    json_patterns = [
        r"\{[^{}]*\"(?:name|function)\"[^{}]*\}",       # 简单 JSON
        r"\{[^{}]*\"(?:name|function)\"[^{}]*\"arguments\"[^{}]*\}",
    ]
    for pat in json_patterns:
        for m in re.finditer(pat, text, re.DOTALL):
            try:
                obj = json.loads(m.group())
                name = obj.get("name") or _safe_get(
                    obj.get("function", {}), "name"
                )
                if name and name in tool_names:
                    args = obj.get("arguments") or obj.get("parameters") or {}
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False)
                    return normalize_tool_call({"name": name, "arguments": args})
            except (json.JSONDecodeError, AttributeError):
                continue

    # 尝试匹配更大的 JSON 块 (带嵌套)
    try:
        start = text.find("{")
        while start != -1:
            depth = 0
            for i in range(start, min(start + 2000, len(text))):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : i + 1]
                        try:
                            obj = json.loads(candidate)
                            name = (
                                obj.get("name")
                                or _safe_get(obj.get("function", {}), "name")
                            )
                            if name and name in tool_names:
                                args = (
                                    obj.get("arguments")
                                    or obj.get("parameters")
                                    or {}
                                )
                                if not isinstance(args, str):
                                    args = json.dumps(args, ensure_ascii=False)
                                return normalize_tool_call(
                                    {"name": name, "arguments": args}
                                )
                        except (json.JSONDecodeError, AttributeError):
                            pass
                        break
            start = text.find("{", start + 1)
    except Exception:
        pass

    return None


def _extract_freeform_tool_call(
    text: str, tool_names: List[str]
) -> Optional[Dict[str, Any]]:
    """策略3: 自由文本匹配 —— 模型可能输出类似 call_xxx(yyy) 的内容。"""
    for name in tool_names:
        # 匹配 name(args) 模式
        pat = rf"(?:^|\s){re.escape(name)}\s*\((.+?)\)"
        m = re.search(pat, text, re.DOTALL)
        if m:
            args_raw = m.group(1).strip()
            args = _parse_args_text(args_raw)
            return normalize_tool_call({"name": name, "arguments": args})

    return None


def _parse_args_text(raw: str) -> str:
    """将函数参数文本转为 JSON 字符串。

    支持格式:
      key="value", key2=123
      key=value, key2=value2
      "json string"
    """
    raw = raw.strip()
    if not raw:
        return "{}"

    # 如果已经是 JSON 对象
    if raw.startswith("{") and raw.endswith("}"):
        try:
            obj = json.loads(raw)
            return json.dumps(obj, ensure_ascii=False)
        except json.JSONDecodeError:
            pass

    # key=value 解析
    args = {}
    # 匹配 key="value" 或 key=value 或 key=123
    pattern = r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^,\s]+))'
    for m in re.finditer(pattern, raw):
        key = m.group(1)
        val = m.group(2) or m.group(3) or m.group(4)
        # 尝试解析数字和布尔
        args[key] = _auto_type(val)

    if args:
        return json.dumps(args, ensure_ascii=False)

    # 无法解析，原样返回
    return json.dumps(raw, ensure_ascii=False)


def _auto_type(val: str) -> Any:
    """自动推断值类型。"""
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    if val.lower() == "null" or val.lower() == "none":
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


def _remove_tool_call_text(text: str) -> str:
    """移除文本中的 TOOL_CALL 行和 XML 工具调用标签。"""
    # 移除 TOOL_CALL: xxx 行
    cleaned = re.sub(r"TOOL_CALL:.*$", "", text, flags=re.MULTILINE)
    # 移除 <function_call> / <function_calls> 标签
    cleaned = re.sub(r"</?function_calls?>", "", cleaned)
    # 移除 <tool_call>...</tool_call> 整块
    cleaned = re.sub(r"<tool_call>.*?</tool_call>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    # 移除残留的 <function=xxx>...</function> 和 <parameter=xxx>...</parameter>
    cleaned = re.sub(r"</?function=\w+>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<parameter=\w+>.*?</parameter>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    # 移除 [调用工具: xxx] 中文格式及其后续参数行
    cleaned = re.sub(r"\[\s*调用工具\s*:\s*\w+(?:\s*,\s*\w+)*\s*\].*", "", cleaned, flags=re.MULTILINE)
    return cleaned.strip()


def _remove_json_tool_call(text: str) -> str:
    """移除文本中的 JSON 工具调用块。"""
    # 尝试找到并移除 JSON 块
    cleaned = text
    start = cleaned.find("{")
    while start != -1:
        depth = 0
        for i in range(start, min(start + 2000, len(cleaned))):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                        name = obj.get("name") or _safe_get(
                            obj.get("function", {}), "name"
                        )
                        if name:
                            cleaned = cleaned[:start] + cleaned[i + 1 :]
                            break
                    except (json.JSONDecodeError, AttributeError):
                        pass
                    break
        start = cleaned.find("{", start + 1)

    return cleaned.strip()
