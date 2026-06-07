from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import dataclass
from types import ModuleType
from typing import Callable

from . import builtin


@dataclass
class ToolInfo:
    name: str
    summary: str
    module: ModuleType | None = None  # None until a lazy tool is resolved
    source: str = "core"  # core | installed | learned
    loader: Callable[[], ModuleType] | None = None  # set for lazy (e.g. MCP) tools
    error: str | None = None  # last failure, if a lazy load could not connect


class Registry:
    """Index of tools. A tool is a Python module exposing functions; built-in,
    installed, and agent-authored skills all live here. Discovery returns the
    interface (signatures + docstrings), never the source — so searching tools
    doesn't flood the agent's context."""

    def __init__(self):
        self._tools: dict[str, ToolInfo] = {}
        self._mcp_clients: list = []  # closed on teardown
        self._load_package(builtin, source="core")

    def _load_package(self, package: ModuleType, source: str) -> None:
        for mod in pkgutil.iter_modules(package.__path__):
            if mod.name.startswith("_"):
                continue
            module = importlib.import_module(f"{package.__name__}.{mod.name}")
            self.register(module, source=source, name=mod.name)

    def register(self, module: ModuleType, *, source: str = "installed", name: str | None = None) -> str:
        """Add a tool module to the registry. Built-ins, locally installed
        modules, and MCP-wrapped servers all enter the same index — distinguished
        only by `source`. Returns the name under which it was registered."""
        name = name or module.__name__.rsplit(".", 1)[-1]
        summary = (module.__doc__ or "").strip().splitlines()[0] if module.__doc__ else ""
        self._tools[name] = ToolInfo(name, summary, module, source)
        return name

    def add_mcp_server(
        self,
        name: str,
        command: str | None = None,
        args: tuple[str, ...] = (),
        *,
        url: str | None = None,
        env: dict | None = None,
        headers: dict | None = None,
        cwd: str | None = None,
        summary: str | None = None,
        timeout: float = 30.0,
    ) -> str:
        """Connect to an MCP server — local (`command`) or remote (`url`) — wrap
        each of its tools as a Python function, and register the result as one
        tool module named `name`. The server's client is closed by `close()`."""
        from .mcp import wrap_mcp_server

        module = wrap_mcp_server(
            name, command, args, url=url, env=env, headers=headers, cwd=cwd,
            summary=summary, timeout=timeout,
        )
        self._mcp_clients.append(module._mcp_client)
        return self.register(module, source="installed", name=name)

    def register_lazy(
        self, name: str, loader: Callable[[], ModuleType], *, source: str = "installed", summary: str = ""
    ) -> str:
        """Register a tool whose module is built on first use, not now. The
        `loader` is called the first time the tool is searched or used; until
        then nothing is connected, so a server that is slow or down can neither
        delay nor abort registration."""
        self._tools[name] = ToolInfo(name, summary, source=source, loader=loader)
        return name

    def _resolve(self, info: ToolInfo) -> ModuleType | None:
        """Return a tool's module, building it on demand for lazy tools. Returns
        None (and records `info.error`) if a lazy load fails — callers stay up."""
        if info.module is not None or info.loader is None:
            return info.module
        try:
            module = info.loader()
        except Exception as exc:
            info.error = f"{type(exc).__name__}: {exc}"
            return None
        info.module, info.error = module, None
        doc = (module.__doc__ or "").strip().splitlines()
        if doc:
            info.summary = doc[0]
        client = getattr(module, "_mcp_client", None)
        if client is not None:
            self._mcp_clients.append(client)
        return module

    def search(self, query: str = "") -> str:
        """Return the interface of matching tools: name, summary, and the
        signature + first docstring line of each public function. Matching a
        lazy tool resolves it (connecting an MCP server); one that can't connect
        is shown as unavailable rather than breaking the search."""
        q = query.lower()
        lines: list[str] = []
        for info in self._tools.values():
            if q and q not in info.name.lower() and q not in info.summary.lower():
                continue
            module = self._resolve(info)
            if module is None:
                note = f"(unavailable: {info.error})" if info.error else "(not loaded)"
                lines.append(f"# {info.name} — {info.summary} {note}".rstrip())
                lines.append("")
                continue
            lines.append(f"# {info.name} — {info.summary}")
            for fname, func in _public_functions(module):
                doc = (func.__doc__ or "").strip().splitlines()
                first = doc[0] if doc else ""
                lines.append(f"    {fname}{inspect.signature(func)}  # {first}")
            lines.append("")
        return "\n".join(lines).strip() or "(no matching tools)"

    def use(self, name: str) -> ModuleType:
        if name not in self._tools:
            raise KeyError(f"tool {name!r} not found; try search_tools()")
        module = self._resolve(self._tools[name])
        if module is None:
            raise RuntimeError(f"tool {name!r} is unavailable: {self._tools[name].error}")
        return module

    def close(self) -> None:
        """Close every MCP server connection this registry opened."""
        for client in self._mcp_clients:
            try:
                client.close()
            except Exception:
                pass
        self._mcp_clients.clear()


def _public_functions(module: ModuleType):
    for name, obj in inspect.getmembers(module, inspect.isfunction):
        if not name.startswith("_") and obj.__module__ == module.__name__:
            yield name, obj
