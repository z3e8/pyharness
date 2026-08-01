from .audit import AuditLog
from .broker import ApprovalOutcome
from .budget import Budget, BudgetExceeded
from .core import Agent, Kernel, Session, Workspace

# After .core: capabilities import core.workspace, so pulling SpawnResult out
# of broker.capabilities any earlier would close an import cycle.
from .broker.capabilities import SpawnResult
from .llm import AnthropicLLM, Completion, ToolCall
from .security import (
    ActionCategory,
    Decision,
    GrantLedger,
    GrantScope,
    Policy,
    ProfileStore,
    Vault,
)
from .tools import Registry

__all__ = [
    "ActionCategory",
    "Agent",
    "AnthropicLLM",
    "ApprovalOutcome",
    "AuditLog",
    "Budget",
    "BudgetExceeded",
    "Completion",
    "Decision",
    "GrantLedger",
    "GrantScope",
    "Kernel",
    "Policy",
    "ProfileStore",
    "Registry",
    "Session",
    "SpawnResult",
    "ToolCall",
    "Vault",
    "Workspace",
]

# The kernel-builtin names, so `from pyharness import use_tool` fails with a
# pointer instead of a bare "cannot import name". These are functions the
# harness injects into the kernel namespace (and into bundled skill code's
# globals) at runtime — they have never been package exports, and the model's
# natural guess that they are is exactly the trap this guard closes. Names
# shadowed by a real package attribute (`llm`, the subpackage) never reach
# `__getattr__`; a drift test pins this list against a live session's op names
# (tests/test_skills.py).
_KERNEL_BUILTINS = frozenset(
    {
        "read",
        "write",
        "edit",
        "bash",
        "search",
        "secrets",
        "create_login",
        "llm",
        "map_llm",
        "spawn",
        "wait",
        "spawn_status",
        "search_tools",
        "describe_tool",
        "use_tool",
        "add_mcp_server",
        "save_skill",
        "edit_skill",
        "record_skill_use",
        "history",
        "stats",
        "inspect_session",
        "notify",
    }
)


def __getattr__(name: str):
    if name in _KERNEL_BUILTINS:
        # ImportError (not AttributeError) on purpose: `from pyharness import X`
        # swallows an AttributeError's message into a generic "cannot import
        # name", while an ImportError propagates verbatim on both the import
        # and the attribute-access path.
        raise ImportError(
            f"{name!r} is a session builtin, not a pyharness export. In "
            "run_python cells and in a skill's bundled files the builtins are "
            "already in scope — call them directly (e.g. use_tool('web')), "
            "never import them. Embedding pyharness in your own program is "
            "different: see docs/reference/python-api.md."
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
