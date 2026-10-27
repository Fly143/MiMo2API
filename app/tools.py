"""工具注册表与执行 — MiMo2API

按 OpenAI function-calling 语义管理可执行工具。

架构：
  - 内置两个工具（search / scrape）：由上游原生执行，
    客户端通过 `<antml:invoke>` 协议获取结果。
  - 其他工具：由本服务端执行并返回结果。

用法：
    from app.tools import registry, Tool

    registry.register(Tool(
        name="list_files",
        description="列出目录文件",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "目录路径"}},
            "required": [],
        },
        execute=list_files,
        sync=True,
    ))

    # 执行
    result = registry.execute("list_files", {"path": "."})
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

# 支持同步与异步执行函数
ExecFunc = Union[Callable[[Dict[str, Any]], Any],
                Callable[[Dict[str, Any]], "Awaitable[Any]"]]


@dataclass
class Tool:
    """一个可执行工具。"""
    name: str
    """工具名，需与上游模型能识别的命名一致（snake_case）。"""
    description: str
    """工具描述，供提示词展示。"""
    parameters: Dict[str, Any]
    """OpenAI 风格 JSON Schema（properties / required / type 等）。"""
    execute: ExecFunc
    """执行函数：接收参数 dict，返回结果。"""
    sync: bool = True
    """True = 同步函数；False = 异步函数（需 await）。"""
    required: List[str] = field(default_factory=list)
    """必填参数名列表。"""


class ToolRegistry:
    """工具注册表：按名称注册、查找、执行工具。"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list(self) -> List[str]:
        return list(self._tools.keys())

    def has(self, name: str) -> bool:
        return name in self._tools

    def execute(self, name: str, arguments: Dict[str, Any]) -> Any:
        """执行指定工具，并返回结果。"""
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"未知工具: {name}")
        try:
            if tool.sync:
                return tool.execute(arguments)
            else:
                import asyncio
                return asyncio.create_task(tool.execute(arguments))
        except Exception as exc:  # noqa: BLE001
            return {"error": f"工具执行失败: {exc}"}


# ─── 默认内置工具 ────────────────────────────────────────────────

def _list_files_impl(args: Dict[str, Any]) -> Dict[str, Any]:
    """列出目录文件（当前目录或指定目录）。"""
    import os

    path = args.get("path", ".")
    try:
        entries = os.listdir(path)
        files = sorted(e for e in entries if os.path.isfile(os.path.join(path, e)))
        dirs = sorted(e for e in entries if os.path.isdir(os.path.join(path, e)))
        return {
            "path": path,
            "files": files,
            "dirs": dirs,
            "count": len(files) + len(dirs),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "path": path}


def _shell_impl(args: Dict[str, Any]) -> Dict[str, Any]:
    """执行 shell 命令（仅 Linux / macOS；不执行危险命令）。"""
    import subprocess

    command = args.get("command", "")
    timeout = args.get("timeout", 10)
    if not command:
        return {"error": "缺少 command 参数"}
    # 阻断危险命令
    dangerous = ("rm ", "mkfs", "dd ", "> /dev/", "format", "shutdown", "reboot")
    if any(d in command for d in dangerous):
        return {"error": "禁止执行危险命令"}
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=timeout,
        )

        def _dec(b: bytes) -> str:
            # UTF-8（Linux/Android）优先，回退 GBK（中文 Windows cmd 报错），最后替换兜底
            if not b:
                return ""
            for enc in ("utf-8", "gbk"):
                try:
                    return b.decode(enc)
                except UnicodeDecodeError:
                    continue
            return b.decode("utf-8", errors="replace")

        return {
            "command": command,
            "exit_code": proc.returncode,
            "stdout": _dec(proc.stdout),
            "stderr": _dec(proc.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        return {"error": f"命令超时: {exc}", "command": command}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "command": command}


# 备注：search / scrape 由上游原生执行，不放在此处。
# 如需扩展，可在 routes.py 里继续使用 antml:invoke 协议。

_default_registry = ToolRegistry()
_default_registry.register(Tool(
    name="list_files",
    description="列出目录中的文件与文件夹。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要列出的目录路径，默认为当前工作目录。"},
        },
        "required": [],
    },
    execute=_list_files_impl,
    sync=True,
))
_default_registry.register(Tool(
    name="shell",
    description="执行一个 shell 命令并返回输出。",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令。"},
            "timeout": {"type": "integer", "description": "命令超时时间（秒），默认 10。"},
        },
        "required": ["command"],
    },
    execute=_shell_impl,
    sync=True,
))


# ─── 单例导出 ────────────────────────────────────────────────────

registry = _default_registry
