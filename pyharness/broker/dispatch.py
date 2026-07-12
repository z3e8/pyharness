from __future__ import annotations

import functools
from dataclasses import dataclass
from types import ModuleType
from typing import Callable

from .. import telemetry
from ..audit import AuditLog
from ..budget import Budget
from ..security.policy import ActionCategory, Decision, Policy
from ..util import summarize_args


class PermissionDenied(Exception):
    """Raised when policy denies a capability call, or approval is refused."""


@dataclass(frozen=True)
class ApprovalRequest:
    """What the human is asked to sign off on.

    The broker builds this from the structured call — never from an agent-supplied
    display string — so what the approver shows is exactly what will execute.
    `category` is the harness's severity classification (see `ActionCategory`) and
    `summary` is a short, secret-safe, human-readable line describing the effect;
    `args`/`kwargs` are kept so a custom approver can inspect further."""

    action: str
    category: ActionCategory
    summary: str
    args: tuple
    kwargs: dict


# An approver renders a confirmation from an `ApprovalRequest` and returns
# True/False. It sees only the harness-built request (category + summary + the
# structured args), never an agent-supplied display string, so what is shown is
# exactly what executes.
Approver = Callable[["ApprovalRequest"], bool]


class Broker:
    """The single chokepoint every side effect flows through.

    For each call, in order: policy check -> audit -> budget (for metered
    actions) -> execute. In V1 execution is a direct in-process function call;
    the same interface later fronts an out-of-process child without the agent or
    any capability changing.
    """

    def __init__(
        self,
        policy: Policy,
        audit: AuditLog,
        budget: Budget,
        *,
        approver: Approver | None = None,
        metered: frozenset[str] = frozenset({"llm", "agents", "web"}),
    ):
        self.policy = policy
        self.audit = audit
        self.budget = budget
        self.approver = approver
        self.metered = metered
        self._ops: dict[tuple[str, str], Callable] = {}
        self._capabilities: dict[str, object] = {}
        self._core: set[str] = set()  # capabilities surfaced as bare-name builtins

    def register(self, capability, *, core: bool = True) -> None:
        """Register a capability's ops on the broker. `core=True` (the default)
        also surfaces them as always-in-scope builtins via `namespace()` /
        `op_names()`; `core=False` registers them for gating but leaves them off
        the bare-name namespace, to be reached through the tool registry instead
        (see `as_tool_module`). Either way every call is broker-gated identically."""
        self._capabilities[capability.name] = capability
        if core:
            self._core.add(capability.name)
        for name, func in capability.exports().items():
            self._ops[(capability.name, name)] = func

    def namespace(self) -> dict[str, Callable]:
        """The functions injected into the agent's kernel as always-in-scope
        builtins, each routed through this broker. Only `core` capabilities are
        surfaced here; non-core ones are reached via the tool registry."""
        return {op: self._proxy(cap, op) for (cap, op) in self._ops if cap in self._core}

    def op_names(self) -> list[str]:
        """The flat operation names the agent calls by bare name (`read`, `bash`,
        ...). The out-of-process child binds a proxy for each and addresses calls
        by name; must match `namespace()`, so it too is core-only."""
        return [op for (cap, op) in self._ops if cap in self._core]

    def as_tool_module(self, cap: str, *, summary: str = "") -> ModuleType:
        """Build a module whose public functions are broker-gated proxies for one
        capability's ops, each carrying the op's real signature and docstring.
        Registering this in the tool registry surfaces a capability through the
        discovery path (search_tools/describe_tool/use_tool) rather than as a bare
        builtin, with identical policy/audit/budget gating. Pairs with
        `register(cap, core=False)`."""
        module = ModuleType(cap)
        module.__doc__ = summary
        for (c, op), func in self._ops.items():
            if c != cap:
                continue
            proxy = self._proxy(cap, op)
            functools.wraps(func)(proxy)  # copy __doc__/__wrapped__ (signature) from the real op
            proxy.__module__ = cap  # so Registry._public_functions lists it as this module's
            setattr(module, op, proxy)
        return module

    def call_op(self, op: str, *args, **kwargs):
        """Dispatch by operation name alone — the address the child sends over
        IPC. Resolves to the owning capability, then runs the full `call` path."""
        cap = next(c for (c, o) in self._ops if o == op)
        return self.call(cap, op, *args, **kwargs)

    def _approval_request(self, cap: str, op: str, action: str, args, kwargs) -> ApprovalRequest:
        """Describe a gated call for the human. A capability that owns gated ops
        provides `preview(op, args, kwargs) -> (category, summary)` so the
        arg-shape knowledge stays with the capability; anything else falls back to
        a conservative OUTWARD classification and a rendered-args summary."""
        preview = getattr(self._capabilities.get(cap), "preview", None)
        if preview is not None:
            category, summary = preview(op, args, kwargs)
        else:
            category = ActionCategory.OUTWARD
            summary = f"{action}({summarize_args(args, kwargs)})"
        return ApprovalRequest(action, category, summary, args, kwargs)

    def _proxy(self, cap: str, op: str) -> Callable:
        def proxy(*args, **kwargs):
            return self.call(cap, op, *args, **kwargs)

        proxy.__name__ = op
        return proxy

    def call(self, cap: str, op: str, *args, **kwargs):
        action = f"{cap}.{op}"

        with telemetry.tool_span(action) as span:
            decision = self.policy.decide(action, args, kwargs)
            if decision is Decision.DENY:
                self.audit.record(action=action, decision="deny", ok=False)
                telemetry.record_tool(span, action=action, decision="deny", ok=False)
                raise PermissionDenied(f"policy denied {action}")
            if decision is Decision.APPROVE:
                request = self._approval_request(cap, op, action, args, kwargs)
                approved = bool(self.approver and self.approver(request))
                self.audit.record(
                    action=action,
                    decision="approve",
                    approved=approved,
                    category=request.category.value,
                )
                if not approved:
                    telemetry.record_tool(span, action=action, decision="approve", ok=False)
                    raise PermissionDenied(f"not approved: {action}")

            if cap in self.metered:
                self.budget.check()

            func = self._ops[(cap, op)]
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                self.audit.record(action=action, ok=False, error=repr(exc))
                telemetry.record_tool(
                    span, action=action, decision="allow", ok=False, error=repr(exc)
                )
                raise
            self.audit.record(action=action, ok=True, args=summarize_args(args, kwargs))
            telemetry.record_tool(span, action=action, decision="allow", ok=True)
            return result
