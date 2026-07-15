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
from .security.profiles import PROFILES_DIR_ENV
from .security.profiles import _DEFAULT_DIR as _PROFILES_DEFAULT_DIR
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
    if kind in ("llm_call", "note", "llm_start"):
        # llm_call and note are already on screen from the streamed llm_token
        # chunks above (llm_call is the post-stream summary, note is preamble
        # text emitted before a tool call); llm_start is a trace-pairing marker
        # for the live viewer. Suppress all three to avoid noise.
        return
    if kind == "notify":
        # Agent-authored note to the user. Rendered standalone under a fixed
        # prefix, visually distinct from the ⚠ approval prompt, and it never
        # takes input — so agent text cannot pass as a harness prompt.
        print(f"\n\033[33m[agent note] {text}{_RESET}", flush=True)
        return
    color = _COLORS.get(kind, "")
    label = {"code": "python", "output": "output"}.get(kind, kind)
    print(f"\n{color}┌─ {label} ─{_RESET}")
    print(f"{color}{text}{_RESET}")


def _prompt_vault_passphrase() -> None:
    """If encrypted state exists (a vault file or any browser profile) but no
    passphrase is set, prompt once and put it in the env so `Vault.from_env()` and
    `ProfileStore.from_env()` pick it up. No such state → no-op, so the dict/env
    backends still work without a passphrase."""
    if os.environ.get("PYHARNESS_VAULT_PASSPHRASE"):
        return
    vault_file = Path(os.environ.get("PYHARNESS_VAULT_FILE", _DEFAULT_FILE))
    profiles_dir = Path(os.environ.get(PROFILES_DIR_ENV, _PROFILES_DEFAULT_DIR))
    has_profiles = profiles_dir.exists() and any(profiles_dir.glob("*.enc"))
    if vault_file.exists() or has_profiles:
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
        # Passed even when the file doesn't exist yet: the session mounts it only
        # if present, but keeps the path so add_mcp_server(save=True) can create it.
        mcp_config=mcp_config,
        index_db=os.environ.get("PYHARNESS_INDEX_DB", "~/.pyharness/index.db"),
        # How many recent cells keep full output in the agent's context; older
        # outputs are elided (kernel variables persist). <= 0 disables elision.
        keep_outputs=int(os.environ.get("PYHARNESS_KEEP_OUTPUTS", "8")),
    )

    # The live viewer: a local page tailing this session's trace.jsonl from a
    # daemon thread (dies with the process). Fail-open — no port, no viewer,
    # but always a session.
    if os.environ.get("PYHARNESS_WATCH", "true").strip().lower() not in ("0", "false", "no", "off"):
        from .watch import start_in_thread

        url = start_in_thread(root, port=int(os.environ.get("PYHARNESS_WATCH_PORT", "6061")))
        if url:
            print(f"\033[2m[watch] live view → {url}{_RESET}")

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
        # Post-session reflection (direction 5): a cheap separate pass over the
        # trace that may propose one improvement — skill writes still prompt for
        # approval above. On by default; PYHARNESS_REFLECT=false opts out.
        if os.environ.get("PYHARNESS_REFLECT", "true").strip().lower() not in ("0", "false", "no", "off"):
            summary = session.reflect()
            if summary:
                print(f"\n\033[2m[{summary}]{_RESET}")
        session.close()


if __name__ == "__main__":
    main()
