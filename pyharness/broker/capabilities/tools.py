from __future__ import annotations

from types import ModuleType

from ...tools.registry import Registry


class ToolsCapability:
    name = "tools"

    def __init__(self, registry: Registry):
        self.registry = registry

    def exports(self) -> dict:
        return {
            "search_tools": self.search_tools,
            "describe_tool": self.describe_tool,
            "use_tool": self.use_tool,
            "invoke": self.invoke,
        }

    def search_tools(self, query: str = "", include_all: bool = False) -> str:
        return self.registry.search(query, include_all=include_all)

    def describe_tool(self, name: str) -> str:
        return self.registry.describe(name)

    def use_tool(self, name: str) -> ModuleType:
        return self.registry.use(name)

    def invoke(self, tool: str, func: str, args: tuple, kwargs: dict):
        """Call a tool's function through the broker. In-process the agent calls
        a tool module's functions directly; out-of-process the child can't hold
        the live module, so its proxy routes each call here — gaining the same
        policy/audit/budget gating as any other capability."""
        return getattr(self.registry.use(tool), func)(*args, **kwargs)
