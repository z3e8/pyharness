from __future__ import annotations

import os
import sys
from pathlib import Path

from .budget import Budget, BudgetExceeded
from .core.session import Session

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
    color = _COLORS.get(kind, "")
    label = {"code": "python", "output": "output", "note": "note"}.get(kind, kind)
    print(f"{color}┌─ {label} ─{_RESET}")
    print(f"{color}{text}{_RESET}")


def _approve(action: str, args: tuple, kwargs: dict) -> bool:
    print(f"\n⚠ approval required: {action}")
    print(f"  args: {args}  kwargs: {kwargs}")
    return input("  allow? [y/N] ").strip().lower() == "y"


def main() -> None:
    _load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY not set (add it to .env or your environment).")
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".sessions/cli")
    session = Session(root, budget=Budget(limit_usd=5.0), approver=_approve, on_event=_trace)

    print("pyharness — type a task, or Ctrl-D to exit.")
    while True:
        try:
            task = input("\n> ").strip()
        except EOFError:
            print()
            break
        if not task:
            continue
        try:
            answer = session.run(task)
        except BudgetExceeded as exc:
            print(f"\n[budget] {exc}")
            continue
        print(f"\n{answer}")
        print(f"\033[2m[spent ${session.budget.spent_usd:.4f} over {session.budget.calls} calls]{_RESET}")


if __name__ == "__main__":
    main()
