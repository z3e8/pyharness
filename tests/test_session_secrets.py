"""The provider API keys the parent uses to call the LLM must never be reachable
by agent code — not from the child's os.environ, nor from a bash subprocess. The
child has no LLM client (completions route through the broker), so it needs none
of them."""
from pyharness.core.session import Session


def test_bash_hides_provider_key(tmp_path, monkeypatch):
    # bash runs parent-side with a scrubbed environ; a live provider key must not
    # survive into it, or `printenv` would hand the agent cleartext.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    session = Session(tmp_path)
    try:
        bash = session.broker.namespace()["bash"]
        out = bash("printenv ANTHROPIC_API_KEY || echo MISSING")
        assert "sk-should-not-leak" not in out
        assert "MISSING" in out
    finally:
        session.close()


def test_child_env_hides_provider_key(tmp_path, monkeypatch):
    # The out-of-process child strips the key before any agent code runs.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("PATH_KEPT", "ok")  # non-secret env still passes through
    session = Session(tmp_path, out_of_process=True)
    try:
        out = session.kernel.run(
            "import os\n"
            "print(os.environ.get('ANTHROPIC_API_KEY'))\n"
            "print(os.environ.get('PATH_KEPT'))\n"
        )
        assert out == "None\nok"
    finally:
        session.close()
