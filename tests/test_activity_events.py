"""The broker's activity events: the live "happening right now" stream.

`action_start` is written before execution and `action_end` on every exit path
(success, error, policy deny, approval deny), and `approval_pending` fires
before the approver blocks on the human — so a viewer tailing the trace can
show what is running, what is stuck, and what it is waiting on.
"""

import pytest

from pyharness.audit import AuditLog
from pyharness.broker.capabilities import FilesCapability
from pyharness.broker.dispatch import ApprovalOutcome, Broker, PermissionDenied
from pyharness.budget import Budget
from pyharness.core.workspace import Workspace
from pyharness.security.policy import Policy


class _Boom:
    name = "boom"

    def exports(self):
        return {"go": self._go}

    def _go(self):
        raise ValueError("kaput")


def _broker(tmp_path, events, *, policy=None, approver=None, extra_caps=()):
    broker = Broker(
        policy or Policy(),
        AuditLog(tmp_path / "audit.jsonl"),
        Budget(),
        approver=approver,
        on_event=lambda kind, text="", **extra: events.append((kind, text, extra)),
    )
    broker.register(FilesCapability(Workspace(tmp_path)))
    for cap in extra_caps:
        broker.register(cap)
    return broker


def test_success_emits_start_then_end(tmp_path):
    events = []
    broker = _broker(tmp_path, events)

    broker.call("files", "write", "a.txt", "hi")

    kinds = [(k, t) for k, t, _ in events]
    assert kinds == [("action_start", "files.write"), ("action_end", "files.write")]
    start_extra, end_extra = events[0][2], events[1][2]
    assert "a.txt" in start_extra["args"]
    assert end_extra["ok"] is True
    assert end_extra["elapsed_s"] >= 0


def test_capability_error_ends_with_error(tmp_path):
    events = []
    broker = _broker(tmp_path, events, extra_caps=[_Boom()])

    with pytest.raises(ValueError):
        broker.call("boom", "go")

    end = events[-1]
    assert end[0] == "action_end"
    assert end[2]["ok"] is False
    assert "kaput" in end[2]["error"]


def test_policy_deny_still_pairs_start_with_end(tmp_path):
    events = []
    broker = _broker(tmp_path, events, policy=Policy(deny={"files.write"}))

    with pytest.raises(PermissionDenied):
        broker.call("files", "write", "a.txt", "hi")

    end = events[-1]
    assert end[0] == "action_end"
    assert end[2] == {"ok": False, "decision": "deny", "elapsed_s": end[2]["elapsed_s"]}


def test_approval_wait_is_visible_before_the_prompt_blocks(tmp_path):
    events = []
    seen_at_prompt = []

    def approver(request):
        # By the time the human is asked, the pending event is already on disk.
        seen_at_prompt.append([k for k, _, _ in events])
        return ApprovalOutcome.ONCE

    broker = _broker(
        tmp_path, events,
        policy=Policy(require_approval={"files.write"}), approver=approver,
    )
    broker.call("files", "write", "a.txt", "hi")

    kinds = [k for k, _, _ in events]
    assert kinds == ["action_start", "approval_pending", "approval_resolved", "action_end"]
    assert seen_at_prompt == [["action_start", "approval_pending"]]
    pending = next(e for e in events if e[0] == "approval_pending")
    assert pending[1] == "files.write"
    assert pending[2]["summary"]
    resolved = next(e for e in events if e[0] == "approval_resolved")
    assert resolved[2]["outcome"] == "once"


def test_refused_approval_ends_the_action(tmp_path):
    events = []
    broker = _broker(
        tmp_path, events,
        policy=Policy(require_approval={"files.write"}), approver=lambda req: False,
    )

    with pytest.raises(PermissionDenied):
        broker.call("files", "write", "a.txt", "hi")

    assert [k for k, _, _ in events][-2:] == ["approval_resolved", "action_end"]
    assert events[-1][2]["ok"] is False


def test_event_sink_failure_never_breaks_the_call(tmp_path):
    def explode(kind, text="", **extra):
        raise OSError("disk full")

    broker = Broker(
        Policy(), AuditLog(tmp_path / "audit.jsonl"), Budget(), on_event=explode
    )
    broker.register(FilesCapability(Workspace(tmp_path)))

    broker.call("files", "write", "a.txt", "hi")  # must not raise

    assert (Workspace(tmp_path).dir / "a.txt").read_text() == "hi"


def test_agent_emits_llm_start_before_each_completion():
    from pyharness.core.agent import Agent
    from pyharness.core.kernel import Kernel
    from pyharness.llm.client import Completion

    events = []

    class LLM:
        def complete(self, *, system, messages, tier="smart", tools=None, on_token=None, on_thinking=None, cache_anchor=None):
            events.append(("complete", ""))
            return Completion(text="done", tool_calls=[], content=[], stop_reason="end_turn")

    agent = Agent(LLM(), Kernel({}), Budget(), on_event=lambda k, t, **kw: events.append((k, t)))
    agent.run("task", [])

    assert events.index(("llm_start", "")) < events.index(("complete", ""))
