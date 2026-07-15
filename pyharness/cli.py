from __future__ import annotations

import getpass
import os
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from .broker.dispatch import ApprovalOutcome, ApprovalRequest
from .budget import Budget, BudgetExceeded
from .core.session import Session
from .security.policy import ActionCategory
from .security.vault import _DEFAULT_FILE

_COLORS = {"code": "\033[36m", "output": "\033[2m"}
_RESET = "\033[0m"


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines from a .env file into the environment so the API key
    is picked up without sourcing it first. Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _trace(kind: str, text: str) -> None:
    if kind == "llm_token":
        # Stream LLM tokens inline as they arrive — no box frame, no newline
        print(text, end="", flush=True)
        return
    if kind in ("llm_call", "note"):
        # Both are already on screen from the streamed llm_token chunks above:
        # llm_call is the post-stream summary, note is preamble text emitted
        # before a tool call. Suppress to avoid double-rendering.
        return
    color = _COLORS.get(kind, "")
    label = {"code": "python", "output": "output"}.get(kind, kind)
    print(f"\n{color}┌─ {label} ─{_RESET}")
    print(f"{color}{text}{_RESET}")


def _prompt_vault_passphrase() -> None:
    """If an encrypted vault file exists but no passphrase is set, prompt once and
    put it in the env so Session's Vault.from_env() picks it up. No file → no-op,
    so the dict/env backends still work without a passphrase."""
    if os.environ.get("PYHARNESS_VAULT_PASSPHRASE"):
        return
    path = Path(os.environ.get("PYHARNESS_VAULT_FILE", _DEFAULT_FILE))
    if path.exists():
        os.environ["PYHARNESS_VAULT_PASSPHRASE"] = getpass.getpass("vault passphrase: ")


def _resolve_root(argv: list[str], env: Mapping[str, str], now: str) -> Path:
    """Where this session's state (workspace, audit, trace) lives. Precedence:
    an explicit CLI path argument wins; else a configured persistent workspace
    (`PYHARNESS_WORKSPACE`), so files dropped in — and files the agent creates —
    survive across runs; else a fresh timestamped directory under `.sessions/`."""
    if len(argv) > 1:
        return Path(argv[1])
    persistent = env.get("PYHARNESS_WORKSPACE")
    if persistent:
        return Path(persistent).expanduser()
    return Path(f".sessions/cli-{now}")


# Human-legible names for a grant's action class, rendered from the harness's own
# structured scope — never from capability- or page-supplied text.
_GRANT_CLASS_LABEL = {
    "browser": "state-changing browser actions",
    "http": "state-changing HTTP requests",
    "mcp": "non-destructive MCP tool calls",
}


def _approve(request: ApprovalRequest) -> ApprovalOutcome:
    print(f"\n⚠ approval required [{request.category.value}]: {request.action}")
    print(f"  {request.summary}")
    # A grant is offered only when the harness marked the call grantable and it is
    # not irreversible (those always re-ask).
    grantable = request.scope is not None and request.category is not ActionCategory.IRREVERSIBLE
    if not grantable:
        note = "  (irreversible — always asks)\n" if request.category is ActionCategory.IRREVERSIBLE else ""
        return ApprovalOutcome.ONCE if input(f"{note}  allow? [y/N] ").strip().lower() == "y" else ApprovalOutcome.DENY
    label = _GRANT_CLASS_LABEL.get(request.scope.action_class, request.scope.action_class)
    print(f"  [y] this once  [a] all {label} on {request.scope.target} this session  [N] no")
    answer = input("  allow? [y/a/N] ").strip().lower()
    if answer == "a":
        return ApprovalOutcome.GRANT
    if answer == "y":
        return ApprovalOutcome.ONCE
    return ApprovalOutcome.DENY


def main() -> None:
    _load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set (add it to .env or your environment).")
    _prompt_vault_passphrase()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = _resolve_root(sys.argv, os.environ, ts)
    mcp_config = Path(os.environ.get("PYHARNESS_MCP_CONFIG", ".mcp.json"))
    session = Session(
        root,
        budget=Budget(limit_usd=5.0),
        approver=_approve,
        on_event=_trace,
        out_of_process=True,
        mcp_config=mcp_config if mcp_config.exists() else None,
    )

    print("pyharness — type a task, or Ctrl-D to exit.")
    try:
        while True:
            try:
                task = input("\n> ").strip()
            except EOFError:
                print()
                break
            if not task:
                continue
            # A turn that fails mid-stream must not crash the REPL. Retry once
            # (the history rollback in Agent.run leaves a clean slate for the
            # resend), then fall back to the prompt so the next message still works.
            answer = None
            for attempt in (1, 2):
                try:
                    answer = session.run(task)
                    break
                except BudgetExceeded as exc:
                    print(f"\n[budget] {exc}")
                    break
                except KeyboardInterrupt:
                    # Ctrl-C aborts the in-flight turn, not the whole session.
                    # Agent.run has rolled history back to before this turn, so
                    # the next prompt starts clean. Ctrl-D still exits the REPL.
                    print("\n[interrupted] turn aborted.")
                    break
                except Exception as exc:
                    if attempt == 1:
                        print(f"\n[retry] stream failed ({type(exc).__name__}) — resending…")
                        continue
                    print(f"\n[error] {type(exc).__name__}: {exc} — turn aborted.")
            if answer is None:
                continue
            # The answer already streamed live via llm_token; just close its line
            # and print the spend summary. Reprinting it here would double-render.
            print(f"\n\033[2m[spent ${session.budget.spent_usd:.4f} over {session.budget.calls} calls]{_RESET}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
