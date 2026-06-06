from __future__ import annotations

from types import ModuleType

from ...tools.registry import Registry


class ToolsCapability:
    name = "tools"

    def __init__(self, registry: Registry):
        self.registry = registry

    def exports(self) -> dict:
        return {"search_tools": self.search_tools, "use_tool": self.use_tool}

    def search_tools(self, query: str = "") -> str:
        return self.registry.search(query)

    def use_tool(self, name: str) -> ModuleType:
        return self.registry.use(name)
