from __future__ import annotations

import functools
import threading
import time
from dataclasses import dataclass
from enum import Enum
from types import ModuleType
from typing import Callable

from ..obs import telemetry
from ..audit import AuditLog
from ..budget import Budget
from ..security.grants import GrantLedger, GrantScope
from ..security.policy import ActionCategory, Decision, Policy
from ..util import summarize_args


class PermissionDenied(Exception):
    """Raised when policy denies a capability call, or approval is refused."""


class ApprovalOutcome(Enum):
    """A human's answer to an approval prompt.

    `ONCE` allows this call only (today's "y"); `GRANT` allows it and mints a
    scoped grant for `request.scope` so matching calls flow without re-prompting.
    A bare `bool` from a simple approver is normalized: `True -> ONCE`, anything
    falsy -> `DENY`. The broker — not the approver — is the authority on whether a
    `GRANT` may mint (never for a non-grantable or IRREVERSIBLE call)."""

    DENY = "deny"
    ONCE = "once"
    GRANT = "grant"


def _normalize(result: "ApprovalOutcome | bool | None") -> "ApprovalOutcome":
    if isinstance(result, ApprovalOutcome):
        return result
    return ApprovalOutcome.ONCE if result else ApprovalOutcome.DENY


@dataclass(frozen=True)
class ApprovalRequest:
    """What the human is asked to sign off on.

    The broker builds this from the structured call — never from an agent-supplied
    display string — so what the approver shows is exactly what will execute.
    `category` is the harness's severity classification (see `ActionCategory`) and
    `summary` is a short, secret-safe, human-readable line describing the effect;
    `args`/`kwargs` are kept so a custom approver can inspect further. `scope` is
    the harness-derived `(action-class, host)` a grant would cover, or `None` when
    the call is not grantable — it is built from the structured call by the
    capability's `scope()` hook, never from agent- or page-supplied text."""

    action: str
    category: ActionCategory
    summary: str
    args: tuple
    kwargs: dict
    scope: GrantScope | None = None


# An approver renders a confirmation from an `ApprovalRequest` and returns an
# `ApprovalOutcome` (or a bare bool, normalized). It sees only the harness-built
# request (category + summary + scope + the structured args), never an
# agent-supplied display string, so what is shown is exactly what executes.
Approver = Callable[["ApprovalRequest"], "ApprovalOutcome | bool"]


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
        grants: GrantLedger | None = None,
        metered: frozenset[str] = frozenset({"llm", "web", "obs", "spawn"}),
        on_event: Callable[..., None] | None = None,
    ):
        self.policy = policy
        self.audit = audit
        self.budget = budget
        self.approver = approver
        # Activity events (`action_start`/`action_end`, `approval_pending`/
        # `approval_resolved`) — the live "what is happening right now" stream,
        # written before/around execution where the audit log records only
        # completed effects. The session wires this to trace.jsonl; fail-open,
        # observability must never break a call.
        self._on_event = on_event
        self.grants = grants or GrantLedger()
        self.metered = metered
        self._ops: dict[tuple[str, str], Callable] = {}
        # Actions currently executing, by an opaque token -> (action, started).
        # Spawned children call from parent-side threads, so access is locked.
        # `abort_inflight` (session teardown) uses this to close the audit
        # record of anything a killed session left running.
        self._inflight: dict[object, tuple[str, float]] = {}
        self._inflight_lock = threading.Lock()
        self._capabilities: dict[str, object] = {}
        self._core: set[str] = set()  # capabilities surfaced as bare-name builtins
        self._hidden: set[tuple[str, str]] = set()  # ops excluded from that surface

    def register(self, capability, *, core: bool = True) -> None:
        """Register a capability's ops on the broker. `core=True` (the default)
        also surfaces them as always-in-scope builtins via `namespace()` /
        `op_names()`; `core=False` registers them for gating but leaves them off
        the bare-name namespace, to be reached through the tool registry instead
        (see `as_tool_module`). Either way every call is broker-gated identically.

        A capability may additionally set `hidden_ops` (a set of its own op
        names) for ops that must stay internal even on a `core=True` capability
        — reachable via `call()`/`call_op()` (so other proxies can route through
        them) but never bound as a bare-name builtin. `tools.invoke` is the
        motivating case: it's the funnel every tool-module proxy calls through,
        not an entry point meant for agent code to call directly."""
        self._capabilities[capability.name] = capability
        if core:
            self._core.add(capability.name)
        hidden = getattr(capability, "hidden_ops", frozenset())
        for name, func in capability.exports().items():
            self._ops[(capability.name, name)] = func
            if name in hidden:
                self._hidden.add((capability.name, name))

    def namespace(self) -> dict[str, Callable]:
        """The functions injected into the agent's kernel as always-in-scope
        builtins, each routed through this broker. Only `core` capabilities are
        surfaced here, minus any op a capability marked `hidden_ops`; non-core
        ones are reached via the tool registry."""
        return {
            op: self._proxy(cap, op)
            for (cap, op) in self._ops
            if cap in self._core and (cap, op) not in self._hidden
        }

    def op_names(self) -> list[str]:
        """The flat operation names the agent calls by bare name (`read`, `bash`,
        ...). The out-of-process child binds a proxy for each and addresses calls
        by name; must match `namespace()`, so it applies the same core/hidden
        filter."""
        return [
            op for (cap, op) in self._ops if cap in self._core and (cap, op) not in self._hidden
        ]

    def as_tool_module(self, cap: str, *, summary: str = "") -> ModuleType:
        """Build a module whose public functions are broker-gated proxies for one
        capability's ops, each carrying the op's real signature and docstring.
        Registering this in the tool registry surfaces a capability through the
        discovery path (search_tools/describe_tool/use_tool) rather than as a bare
        builtin, with identical policy/audit/budget gating. Pairs with
        `register(cap, core=False)`."""
        module = ModuleType(cap)
        module.__doc__ = summary
        module._broker_gated = True  # so tools.use_tool doesn't gate it twice
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
        IPC. Resolves to the owning capability, then runs the full `call` path.

        The child only ever addresses ops this way that it holds as bare-name
        proxies — the `core` capabilities' ops (`namespace()`/`op_names()`), plus
        the hidden `tools.invoke` funnel every RemoteToolSpec routes through.
        Non-core capabilities (web/http/browser/inbox/packages) are reached only
        via `invoke`, never by their own op name. So resolution is restricted to
        `core` capabilities: this disambiguates op-name collisions between a core
        builtin and a non-core op (`files.read` vs `inbox.read`, `search.search`
        vs `inbox.search`) instead of relying on registration order, and an
        ambiguous or unknown name is an explicit error rather than a silent
        arbitrary pick that could route a bare call to the wrong capability."""
        caps = [c for (c, o) in self._ops if o == op and c in self._core]
        if not caps:
            raise KeyError(f"unknown operation {op!r}")
        if len(caps) > 1:
            raise KeyError(f"operation {op!r} is ambiguous across capabilities {sorted(caps)}")
        return self.call(caps[0], op, *args, **kwargs)

    def _approval_request(self, cap: str, op: str, action: str, args, kwargs) -> ApprovalRequest:
        """Describe a gated call for the human. A capability that owns gated ops
        provides `preview(op, args, kwargs) -> (category, summary)` so the
        arg-shape knowledge stays with the capability; anything else falls back to
        a conservative OUTWARD classification and a rendered-args summary. An
        optional `scope(op, args, kwargs) -> GrantScope | None` hook (same lookup)
        yields the harness-derived grant key; absent or None means not grantable."""
        capability = self._capabilities.get(cap)
        preview = getattr(capability, "preview", None)
        if preview is not None:
            category, summary = preview(op, args, kwargs)
        else:
            category = ActionCategory.OUTWARD
            summary = f"{action}({summarize_args(args, kwargs)})"
        scope_hook = getattr(capability, "scope", None)
        scope = scope_hook(op, args, kwargs) if scope_hook is not None else None
        return ApprovalRequest(action, category, summary, args, kwargs, scope)

    def _proxy(self, cap: str, op: str) -> Callable:
        def proxy(*args, **kwargs):
            return self.call(cap, op, *args, **kwargs)

        proxy.__name__ = op
        return proxy

    def _emit(self, kind: str, text: str = "", **extra) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, text, **extra)
        except Exception:  # noqa: BLE001 — events are observability, never a blocker
            pass

    def call(self, cap: str, op: str, *args, **kwargs):
        action = f"{cap}.{op}"
        started = time.perf_counter()
        self._emit("action_start", action, args=summarize_args(args, kwargs))
        # Two chained audit records per call: this intent record before anything
        # runs, and an outcome record (`phase: "end"`) on every exit path. An
        # action killed in flight then still exists in the tamper-evident chain
        # — as a start closed by `abort_inflight`, or as an unpaired start when
        # the process died too hard even for that.
        self.audit.record(action=action, phase="start", args=summarize_args(args, kwargs))
        token = object()
        with self._inflight_lock:
            self._inflight[token] = (action, started)

        def _end(**fields) -> None:
            self._emit(
                "action_end", action, elapsed_s=round(time.perf_counter() - started, 3), **fields
            )

        try:
            return self._dispatch(cap, op, action, args, kwargs, _end)
        finally:
            with self._inflight_lock:
                self._inflight.pop(token, None)

    def _dispatch(self, cap: str, op: str, action: str, args, kwargs, _end) -> object:
        with telemetry.tool_span(action) as span:
            decision = self.policy.decide(action, args, kwargs)
            if decision is Decision.DENY:
                self.audit.record(action=action, phase="end", decision="deny", ok=False)
                telemetry.record_tool(span, action=action, decision="deny", ok=False)
                _end(ok=False, decision="deny")
                raise PermissionDenied(f"policy denied {action}")
            if decision is Decision.APPROVE:
                request = self._approval_request(cap, op, action, args, kwargs)
                # IRREVERSIBLE actions are never covered by, and never mint, a
                # grant — they always re-prompt (see ActionCategory).
                grantable = (
                    request.scope is not None
                    and request.category is not ActionCategory.IRREVERSIBLE
                )
                grant = self.grants.find(request.scope) if grantable else None
                if grant is not None:
                    self.audit.record(
                        action=action,
                        decision="approve",
                        approved=True,
                        category=request.category.value,
                        grant_id=grant.id,
                    )
                else:
                    # Emitted before the approver blocks on the human, so a live
                    # view can show *what* the session is waiting on.
                    self._emit(
                        "approval_pending",
                        action,
                        summary=request.summary,
                        category=request.category.value,
                    )
                    outcome = (
                        _normalize(self.approver(request))
                        if self.approver
                        else ApprovalOutcome.DENY
                    )
                    self._emit("approval_resolved", action, outcome=outcome.value)
                    fields: dict = dict(
                        action=action,
                        decision="approve",
                        approved=outcome is not ApprovalOutcome.DENY,
                        category=request.category.value,
                    )
                    if outcome is ApprovalOutcome.GRANT and grantable:
                        minted = self.grants.add(request.scope)
                        fields["grant"] = {
                            "id": minted.id,
                            "action_class": minted.scope.action_class,
                            "target": minted.scope.target,
                            "expires_at": minted.expires_at,
                        }
                    if outcome is ApprovalOutcome.DENY:
                        # A refused approval ends the action: its decision record
                        # doubles as the outcome record.
                        fields.update(phase="end", ok=False)
                    self.audit.record(**fields)
                    if outcome is ApprovalOutcome.DENY:
                        telemetry.record_tool(span, action=action, decision="approve", ok=False)
                        _end(ok=False, decision="approve")
                        raise PermissionDenied(f"not approved: {action}")

            if cap in self.metered:
                self.budget.check()

            func = self._ops[(cap, op)]
            try:
                result = func(*args, **kwargs)
            except BaseException as exc:  # incl. KeyboardInterrupt — record, then re-raise
                self.audit.record(action=action, phase="end", ok=False, error=repr(exc))
                telemetry.record_tool(
                    span, action=action, decision="allow", ok=False, error=repr(exc)
                )
                _end(ok=False, error=repr(exc))
                raise
            self.audit.record(
                action=action, phase="end", ok=True, args=summarize_args(args, kwargs)
            )
            telemetry.record_tool(span, action=action, decision="allow", ok=True)
            _end(ok=True)
            return result

    def abort_inflight(self) -> list[str]:
        """Close the audit record of every action still executing — called on
        session teardown, when in-flight calls (e.g. a broker call stuck in a
        thread the abort orphaned) would otherwise vanish between their start
        record and never-written outcome. Appends `{phase: "end", ok: False,
        error: "aborted"}` per action, best-effort, and returns their names."""
        with self._inflight_lock:
            pending = list(self._inflight.values())
            self._inflight.clear()
        for action, started in pending:
            self.audit.record(action=action, phase="end", ok=False, error="aborted")
            self._emit(
                "action_end",
                action,
                elapsed_s=round(time.perf_counter() - started, 3),
                ok=False,
                error="aborted",
            )
        return [action for action, _ in pending]
