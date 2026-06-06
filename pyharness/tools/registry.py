from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import dataclass
from types import ModuleType

from . import builtin


@dataclass(frozen=True)
class ToolInfo:
    name: str
    summary: str
    module: ModuleType


class Registry:
    """Index of tools. A tool is a Python module exposing functions; built-in,
    installed, and agent-authored skills all live here. Discovery returns the
    interface (signatures + docstrings), never the source — so searching tools
    doesn't flood the agent's context."""

    def __init__(self):
        self._tools: dict[str, ToolInfo] = {}
        self._load_package(builtin, source="core")

    def _load_package(self, package: ModuleType, source: str) -> None:
        for mod in pkgutil.iter_modules(package.__path__):
            if mod.name.startswith("_"):
                continue
            module = importlib.import_module(f"{package.__name__}.{mod.name}")
            summary = (module.__doc__ or "").strip().splitlines()[0] if module.__doc__ else ""
            self._tools[mod.name] = ToolInfo(mod.name, summary, module)

    def search(self, query: str = "") -> str:
        """Return the interface of matching tools: name, summary, and the
        signature + first docstring line of each public function."""
        q = query.lower()
        lines: list[str] = []
        for info in self._tools.values():
            if q and q not in info.name.lower() and q not in info.summary.lower():
                continue
            lines.append(f"# {info.name} — {info.summary}")
            for fname, func in _public_functions(info.module):
                doc = (func.__doc__ or "").strip().splitlines()
                first = doc[0] if doc else ""
                lines.append(f"    {fname}{inspect.signature(func)}  # {first}")
            lines.append("")
        return "\n".join(lines).strip() or "(no matching tools)"

    def use(self, name: str) -> ModuleType:
        if name not in self._tools:
            raise KeyError(f"tool {name!r} not found; try search_tools()")
        return self._tools[name].module


def _public_functions(module: ModuleType):
    for name, obj in inspect.getmembers(module, inspect.isfunction):
        if not name.startswith("_") and obj.__module__ == module.__name__:
            yield name, obj
