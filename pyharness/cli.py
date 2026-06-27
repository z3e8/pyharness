from __future__ import annotations

import getpass
import os
import sys
from datetime import datetime
from pathlib import Path

from .budget import Budget, BudgetExceeded
from .core.session import Session
from .security.vault import _DEFAULT_FILE

_COLORS = {"note": "\033[2m", "code": "\033[36m", "output": "\033[2m"}
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
    if kind in ("llm_partial", "llm_call"):
        # llm_partial is for the observe UI; llm_call fires after streaming is done.
        # Both are already visible via streamed tokens above, so suppress here.
        return
    color = _COLORS.get(kind, "")
    label = {"code": "python", "output": "output", "note": "note"}.get(kind, kind)
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


def _approve(action: str, args: tuple, kwargs: dict) -> bool:
    print(f"\n⚠ approval required: {action}")
    print(f"  args: {args}  kwargs: {kwargs}")
    return input("  allow? [y/N] ").strip().lower() == "y"


def main() -> None:
    _load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set (add it to .env or your environment).")
    _prompt_vault_passphrase()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(f".sessions/cli-{ts}")
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
                except Exception as exc:
                    if attempt == 1:
                        print(f"\n[retry] stream failed ({type(exc).__name__}) — resending…")
                        continue
                    print(f"\n[error] {type(exc).__name__}: {exc} — turn aborted.")
            if answer is None:
                continue
            print(f"\n{answer}")
            print(f"\033[2m[spent ${session.budget.spent_usd:.4f} over {session.budget.calls} calls]{_RESET}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
