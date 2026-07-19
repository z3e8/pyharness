"""The provider API keys the parent uses to call the LLM must never be reachable
by agent code — not from the child's os.environ, nor from a bash subprocess. The
child has no LLM client (completions route through the broker), so it needs none
of them."""
from pyharness.core.session import Session


def test_bash_hides_provider_key(tmp_path, monkeypatch):
    # bash runs parent-side with a scrubbed environ; a live provider key must not
    # survive into it, or `printenv` would hand the agent cleartext.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    # shell.bash is approval-gated by default; this test is about the env, so
    # approve the call.
    session = Session(tmp_path, approver=lambda request: True)
    try:
        bash = session.broker.namespace()["bash"]
        out = bash("printenv ANTHROPIC_API_KEY || echo MISSING")
        assert "sk-should-not-leak" not in out
        assert "MISSING" in out
    finally:
        session.close()


def test_child_env_is_minimal_allowlist(tmp_path, monkeypatch):
    # The out-of-process child reduces its environment to the allowlist before
    # any agent code runs: provider keys AND unknown vars (default-deny) are
    # gone; allowlisted basics survive; PYHARNESS_ENV_PASSTHROUGH admits extras.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("CUSTOM_DB_URL", "postgres://secret")  # not on any denylist
    monkeypatch.setenv("OPTED_IN", "ok")
    monkeypatch.setenv("PYHARNESS_ENV_PASSTHROUGH", "OPTED_IN")
    session = Session(tmp_path, out_of_process=True)
    try:
        out = session.kernel.run(
            "import os\n"
            "print(os.environ.get('ANTHROPIC_API_KEY'))\n"
            "print(os.environ.get('CUSTOM_DB_URL'))\n"
            "print(os.environ.get('OPTED_IN'))\n"
            "print('PATH' in os.environ)\n"
        )
        assert out == "None\nNone\nok\nTrue"
    finally:
        session.close()
