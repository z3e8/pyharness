from __future__ import annotations

from dataclasses import dataclass
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

    def register(self, capability) -> None:
        self._capabilities[capability.name] = capability
        for name, func in capability.exports().items():
            self._ops[(capability.name, name)] = func

    def namespace(self) -> dict[str, Callable]:
        """The functions injected into the agent's kernel, each routed through
        this broker."""
        return {name: self._proxy(cap, name) for (cap, name) in self._ops}

    def op_names(self) -> list[str]:
        """The flat operation names the agent calls (`read`, `bash`, ...). The
        out-of-process child binds a proxy for each and addresses calls by name;
        names are unique across capabilities, matching `namespace()`."""
        return [op for (_cap, op) in self._ops]

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
