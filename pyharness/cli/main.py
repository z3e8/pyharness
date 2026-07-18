from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from ..broker.dispatch import ApprovalOutcome, ApprovalRequest
from ..budget import Budget, BudgetExceeded
from ..core.session import Session
from ..security.policy import ActionCategory
from ..security.profiles import PROFILES_DIR_ENV
from ..security.profiles import _DEFAULT_DIR as _PROFILES_DEFAULT_DIR
from ..security.vault import _DEFAULT_FILE
from ..obs.transcript import latest_session, render_transcript, session_digest

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


# Whether the line currently being streamed is a dim thinking marker that the
# next answer token must close (module-level: the renderer is a plain callback).
_thinking_open = False


def _close_thinking() -> None:
    global _thinking_open
    if _thinking_open:
        print(_RESET, flush=True)
        _thinking_open = False


def _trace(kind: str, text: str) -> None:
    global _thinking_open
    if kind == "llm_thinking":
        # The model is thinking (summarized deltas stream in the live viewer).
        # In the terminal, one dim marker per thinking span — not the full text,
        # which would drown the answer stream.
        if not _thinking_open:
            print("\033[2m[thinking…]", end="", flush=True)
            _thinking_open = True
        return
    if kind == "llm_token":
        # Stream LLM tokens inline as they arrive — no box frame, no newline
        _close_thinking()
        print(text, end="", flush=True)
        return
    _close_thinking()
    if kind in ("llm_call", "note", "llm_start"):
        # llm_call and note are already on screen from the streamed llm_token
        # chunks above (llm_call is the post-stream summary, note is preamble
        # text emitted before a tool call); llm_start is a trace-pairing marker
        # for the live viewer. Suppress all three to avoid noise.
        return
    if kind == "spawn_progress":
        # A background child's milestone (task/code/answer/error), one dim line
        # so the terminal shows spawned work progressing without drowning the
        # parent's own stream.
        print(f"\n\033[2m{text}{_RESET}", flush=True)
        return
    if kind == "worker":
        # An llm()/map_llm worker heartbeat, one dim line — the CLI is otherwise
        # blind to parent-side worker calls (the broker's action spinner is
        # viewer-only), so a slow completion or fan-out reads as a hang.
        print(f"\n\033[2m[{text}]{_RESET}", flush=True)
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


def _resolve_root(dir_arg: str | None, env: Mapping[str, str], now: str) -> Path:
    """Where a REPL session's state (workspace, audit, trace) lives. Precedence:
    an explicit CLI path argument wins; else a configured persistent workspace
    (`PYHARNESS_WORKSPACE`), so files dropped in — and files the agent creates —
    survive across runs; else a fresh timestamped directory under `.sessions/`."""
    if dir_arg:
        return Path(dir_arg)
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


def _reflect_enabled(env: Mapping[str, str]) -> bool:
    """Post-session reflection is opt-in: only an explicit truthy
    PYHARNESS_REFLECT runs the LLM pass at exit."""
    return env.get("PYHARNESS_REFLECT", "").strip().lower() in ("1", "true", "yes", "on")


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


def _headless_approver(approve_all: bool):
    """No stdin in a one-shot run: gated actions are DENIED (fail closed — the
    denial is audited and traced like any other) unless --approve-all, which
    answers each request ONCE. Per-call approval only, never a standing grant."""

    def approve(request: ApprovalRequest) -> ApprovalOutcome:
        return ApprovalOutcome.ONCE if approve_all else ApprovalOutcome.DENY

    return approve


def _build_session(root: Path, *, budget_usd: float, approver, on_event=None, **overrides) -> Session:
    """One Session construction shared by `repl` and `run`. `overrides` (llm,
    out_of_process, skills_dir, …) are a test seam; the CLI never passes them."""
    kwargs: dict = dict(
        budget=Budget(limit_usd=budget_usd),
        approver=approver,
        on_event=on_event,
        out_of_process=True,
        # Passed even when the file doesn't exist yet: the session mounts it only
        # if present, but keeps the path so add_mcp_server(save=True) can create it.
        mcp_config=Path(os.environ.get("PYHARNESS_MCP_CONFIG", ".mcp.json")),
        index_db=os.environ.get("PYHARNESS_INDEX_DB", "~/.pyharness/index.db"),
        # How many recent cells keep full output in the agent's context; older
        # outputs are elided (kernel variables persist). <= 0 disables elision.
        keep_outputs=int(os.environ.get("PYHARNESS_KEEP_OUTPUTS", "8")),
    )
    kwargs.update(overrides)
    return Session(root, **kwargs)


def _start_watch(root: Path) -> None:
    """The live viewer: a local page tailing this session's trace.jsonl from a
    daemon thread (dies with the process). Fail-open — no port, no viewer, but
    always a session. The URL goes to stderr so `run --json` stdout stays
    machine-clean."""
    if os.environ.get("PYHARNESS_WATCH", "true").strip().lower() in ("0", "false", "no", "off"):
        return
    from ..obs.watch import start_in_thread

    url = start_in_thread(root, port=int(os.environ.get("PYHARNESS_WATCH_PORT", "6061")))
    if url:
        print(f"\033[2m[watch] live view → {url}{_RESET}", file=sys.stderr)


# One exit code per outcome, so a scripted caller can branch without parsing.
# 1 is left to argparse/startup failures; aborted and empty collapse to 5 (the
# digest disambiguates).
_EXIT_BY_OUTCOME = {
    "answered": 0,
    "stopped:max_steps": 2,
    "stopped:budget": 3,
    "error": 4,
    "aborted": 5,
    "empty": 5,
}


def _run_once(task: str, root: Path, *, budget_usd: float, approve_all: bool,
              reflect: bool, watch: bool = True, **overrides) -> dict:
    """Run one task headlessly and return the session digest. Exceptions from
    the turn are already recorded in the trace by Session.run and classified by
    the digest — this only makes sure the session always closes."""
    session = _build_session(
        root, budget_usd=budget_usd, approver=_headless_approver(approve_all), **overrides
    )
    if watch:
        _start_watch(root)
    try:
        try:
            session.run(task)
        except Exception as exc:  # noqa: BLE001 — in the trace; the digest classifies it
            print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        if reflect:
            summary = session.reflect()
            if summary:
                print(f"[{summary}]", file=sys.stderr)
        session.close()
    return session_digest(root)


def _cmd_run(args: argparse.Namespace) -> None:
    _load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set (add it to .env or your environment).")
    # No vault passphrase prompt: a one-shot run has no stdin to give; vault-backed
    # features fail closed instead.
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path(args.dir) if args.dir else Path(f".sessions/run-{ts}")
    try:
        digest = _run_once(
            args.task, root,
            budget_usd=args.budget,
            approve_all=args.approve_all,
            reflect=args.reflect or _reflect_enabled(os.environ),
        )
    except KeyboardInterrupt:
        sys.exit(130)
    if args.json:
        print(json.dumps(digest))
    else:
        print(digest["answer"] or f"(no answer — outcome: {digest['outcome']})")
        print(
            f"[session {digest['name']}: {digest['outcome']}, "
            f"${digest['cost_usd']:.4f} over {digest['llm_calls']} LLM calls]",
            file=sys.stderr,
        )
    sys.exit(_EXIT_BY_OUTCOME.get(digest["outcome"], 1))


def _resolve_session(session: str | None, root: Path) -> Path:
    """A session dir for `show`: an explicit dir path, a name under `root`, or
    (no argument) the most recently active session under `root`."""
    if session:
        for candidate in (Path(session), root / session):
            if (candidate / "trace.jsonl").exists():
                return candidate
        sys.exit(
            f"no session {session!r} under {root} — list recent ones with:\n"
            '  pyharness-index --sql "SELECT name, outcome, cost_usd FROM sessions'
            ' ORDER BY started DESC LIMIT 10"'
        )
    latest = latest_session(root)
    if latest is None:
        sys.exit(f"no sessions under {root}")
    return latest


_SHOW_ANSWER_CLIP = 400


def _cmd_show(args: argparse.Namespace) -> None:
    session_dir = _resolve_session(args.session, Path(args.root))
    if args.transcript:
        print(render_transcript(session_dir / "trace.jsonl"))
        return
    digest = session_digest(session_dir)
    if args.json:
        print(json.dumps(digest))
        return
    for key in ("name", "outcome", "task", "tasks", "steps", "llm_calls", "errors",
                "actions", "denials", "cost_usd"):
        print(f"{key}: {digest[key]}")
    answer = digest["answer"]
    if answer is not None:
        if len(answer) > _SHOW_ANSWER_CLIP:
            answer = answer[:_SHOW_ANSWER_CLIP] + "… (full answer: --json)"
        print(f"answer: {answer}")
    for key in ("session", "trace", "audit", "workspace"):
        print(f"{key}: {digest[key]}")


def _cmd_repl(args: argparse.Namespace) -> None:
    _load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set (add it to .env or your environment).")
    _prompt_vault_passphrase()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = _resolve_root(args.dir, os.environ, ts)
    session = _build_session(root, budget_usd=5.0, approver=_approve, on_event=_trace)
    _start_watch(root)

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
            # A turn that fails must not crash the REPL. Transient stream
            # failures are already retried with backoff inside the LLM client;
            # anything surfacing here is either terminal for the turn or
            # deterministic, so resending the whole turn would just duplicate
            # cost and side effects. The history rollback in Agent.run leaves a
            # clean slate for the next prompt.
            answer = None
            try:
                answer = session.run(task)
            except BudgetExceeded as exc:
                print(f"\n[budget] {exc}")
            except KeyboardInterrupt:
                # Ctrl-C aborts the in-flight turn, not the whole session.
                # Ctrl-D still exits the REPL.
                print("\n[interrupted] turn aborted.")
            except Exception as exc:
                print(f"\n[error] {type(exc).__name__}: {exc} — turn aborted.")
            if answer is None:
                continue
            # The answer already streamed live via llm_token; just close its line
            # and print the spend summary. Reprinting it here would double-render.
            print(f"\n\033[2m[spent ${session.budget.spent_usd:.4f} over {session.budget.calls} calls]{_RESET}")
    finally:
        # Post-session reflection (direction 5): a cheap separate pass over the
        # trace that may propose one improvement — skill writes still prompt for
        # approval above. Opt-in: the deterministic record (skill journals, the
        # index) carries the default self-improvement loop.
        if _reflect_enabled(os.environ):
            summary = session.reflect()
            if summary:
                print(f"\n\033[2m[{summary}]{_RESET}")
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pyharness",
        description="An AI agent whose action space is Python.",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_repl = sub.add_parser("repl", help="interactive agent (the default)")
    p_repl.add_argument(
        "dir", nargs="?",
        help="session root (default: PYHARNESS_WORKSPACE or a fresh .sessions/cli-<ts>)",
    )

    p_run = sub.add_parser("run", help="run one task headlessly and print the answer")
    p_run.add_argument("task", help="the task to run")
    p_run.add_argument("--dir", help="session root (default: a fresh .sessions/run-<ts>)")
    p_run.add_argument("--budget", type=float, default=5.0, metavar="USD",
                       help="spend cap in USD (default: 5)")
    p_run.add_argument("--json", action="store_true",
                       help="print the session digest as JSON instead of the answer")
    p_run.add_argument("--approve-all", action="store_true",
                       help="auto-approve gated actions (default: deny them)")
    p_run.add_argument("--reflect", action="store_true",
                       help="run the post-session reflection pass (or set PYHARNESS_REFLECT)")

    p_show = sub.add_parser("show", help="inspect a past session without an API key")
    p_show.add_argument("session", nargs="?",
                        help="session dir or name under --root (default: the latest)")
    p_show.add_argument("--root", default=".sessions",
                        help="sessions directory (default: .sessions)")
    view = p_show.add_mutually_exclusive_group()
    view.add_argument("--transcript", action="store_true",
                      help="print the flattened transcript (prompt snapshots dropped)")
    view.add_argument("--json", action="store_true", help="print the digest as JSON")

    args = parser.parse_args(sys.argv[1:] or ["repl"])
    {"repl": _cmd_repl, "run": _cmd_run, "show": _cmd_show}[args.cmd](args)


if __name__ == "__main__":
    main()
