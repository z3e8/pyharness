"""Secret masking on the exception path.

Property: once a vault secret has been resolved for injection, its cleartext
never surfaces through a *failing* call — not in the audit log, not in the
trace, and not in the traceback the agent sees as cell output — no matter what
the raising capability embedded in the exception. Success-path masking is each
capability's job; these tests pin the invariant for the paths a capability
does not control: the broker's audited error and the kernels' tracebacks.

The scenario is real: `httpx.HTTPStatusError`'s repr embeds the full request
URL (query params included) and `subprocess.TimeoutExpired`'s embeds the whole
argv — an exception class is a perfectly good exfiltration envelope.
"""

from __future__ import annotations

import pytest

from pyharness import Budget, Policy, Vault, Workspace
from pyharness.audit import AuditLog
from pyharness.broker import Broker
from pyharness.broker.capabilities import HttpSessionCapability
from pyharness.broker.remote import RemoteKernel
from pyharness.core.session import Session
from pyharness.security.sink import SecretSink

SECRET = "S3CR3T-token-XYZ"


class _LeakyCapability:
    """A capability that resolves a secret, then raises with it embedded —
    the shape of an httpx error carrying the full query-string URL."""

    name = "leaky"

    def __init__(self, sink: SecretSink):
        self._sink = sink

    def exports(self):
        return {"leak": self._leak}

    def _leak(self):
        secret = self._sink.resolve("tok")
        raise RuntimeError(
            f"connect failed for https://api.example.com/data?token={secret}"
        )


class _CleanErrorCapability:
    """A capability raising an ordinary, secret-free error — the exception type
    must survive to agent code untouched (masking must not wrap clean errors)."""

    name = "cleanboom"

    def exports(self):
        return {"cleanboom": self._boom}

    def _boom(self):
        raise ValueError("plain failure, nothing sensitive")


def _leaky_broker(tmp_path) -> Broker:
    vault = Vault({"tok": SECRET})
    session_sink = SecretSink(vault)
    broker = Broker(
        Policy(),
        AuditLog(tmp_path / "audit.jsonl"),
        Budget(),
        redact=session_sink.redact,
    )
    # The per-context sink mirrors into the session-wide one, exactly as the
    # capabilities wired by Session do.
    broker.register(_LeakyCapability(SecretSink(vault, mirror=session_sink)))
    broker.register(_CleanErrorCapability())
    return broker


def test_audit_and_trace_never_carry_a_resolved_secret(tmp_path):
    """A capability raising with the secret embedded still raises (errors stay
    debuggable, type intact), but the audit record of the failure is masked."""
    broker = _leaky_broker(tmp_path)
    with pytest.raises(RuntimeError) as excinfo:
        broker.call("leaky", "leak")
    # The exception itself still carries the real message parent-side — masking
    # applies to what gets *persisted and surfaced*, not to control flow.
    assert "connect failed" in str(excinfo.value)
    audit_text = (tmp_path / "audit.jsonl").read_text()
    assert SECRET not in audit_text
    assert "***" in audit_text
    assert "RuntimeError" in audit_text  # the type survives for debugging


def test_in_process_cell_output_masks_secret_bearing_traceback(tmp_path, monkeypatch):
    """End to end through a real Session: the agent triggers an http call whose
    client explodes with the secret-bearing URL in the message; the traceback
    the agent sees and every session artifact on disk stay cleartext-free."""
    import httpx

    class _ExplodingClient:
        def __init__(self, **kwargs):
            pass

        def request(self, method, url, **kwargs):
            # httpx.HTTPStatusError-style: the full target (params included)
            # rides in the message.
            raise RuntimeError(f"boom: {method} {url} params={kwargs.get('params')}")

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _ExplodingClient)
    session = Session(
        tmp_path / "s",
        llm=object(),  # the agent loop is never entered
        vault=Vault({"tok": SECRET}),
        approver=lambda request: True,  # secret-carrying requests are gated
        unsafe_in_process=True,
        skills_dir=tmp_path / "skills",
    )
    try:
        output = session.kernel.run(
            "t = use_tool('http')\n"
            "t.request(None, 'GET', 'https://api.example.com/data',\n"
            "          auth='tok', auth_style='query', auth_name='token')\n"
        )
    finally:
        session.close()
    # The cell failed and says so usefully...
    assert "RuntimeError" in output
    # ...but the cleartext appears nowhere the agent (or a later session
    # inspecting this one) can read: not the output, not audit, not trace.
    assert SECRET not in output
    assert "***" in output
    for artifact in ("audit.jsonl", "trace.jsonl"):
        text = (tmp_path / "s" / artifact).read_text()
        assert SECRET not in text, f"cleartext secret leaked into {artifact}"


def test_remote_kernel_masks_secret_bearing_exception(tmp_path):
    """The out-of-process path: the exception crosses the pipe to the child and
    is formatted into the child's traceback — the cleartext must not cross, so
    it can appear neither in the child's memory nor in the returned output."""
    kernel = RemoteKernel(_leaky_broker(tmp_path), workspace=Workspace(tmp_path))
    try:
        output = kernel.run("leak()")
    finally:
        kernel.close()
    assert SECRET not in output
    assert "***" in output
    assert "RuntimeError" in output  # original type named in the masked error
    audit_text = (tmp_path / "audit.jsonl").read_text()
    assert SECRET not in audit_text


def test_remote_kernel_keeps_clean_exception_types_catchable(tmp_path):
    """Masking must not tax the normal path: an ordinary secret-free error
    keeps its type across the pipe so agent code can catch it."""
    kernel = RemoteKernel(_leaky_broker(tmp_path), workspace=Workspace(tmp_path))
    try:
        output = kernel.run(
            "try:\n    cleanboom()\nexcept ValueError as e:\n    print('caught:', e)\n"
        )
    finally:
        kernel.close()
    assert "caught: plain failure, nothing sensitive" in output


def test_http_capability_mirrors_resolved_secrets_to_the_session_sink(
    tmp_path, monkeypatch
):
    """The wiring the invariant rests on: a secret resolved inside the http
    capability becomes redactable by the session-wide sink (not only by the
    per-request sink that resolved it)."""
    import httpx

    class _FailingClient:
        def __init__(self, **kwargs):
            pass

        def request(self, method, url, **kwargs):
            raise ConnectionError("refused")

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _FailingClient)
    vault = Vault({"tok": SECRET})
    session_sink = SecretSink(vault)
    http = HttpSessionCapability(
        Workspace(tmp_path), vault=vault, sink_mirror=session_sink
    )
    with pytest.raises(ConnectionError):
        # The send fails, but the secret was already resolved for injection.
        http.request(None, "GET", "https://api.example.com/x", auth="tok")
    assert session_sink.redact(f"leaked {SECRET} here") == "leaked *** here"
