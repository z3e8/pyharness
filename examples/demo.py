"""Run pyharness against a real task.

    uv pip install -e .
    ANTHROPIC_API_KEY=... uv run python examples/demo.py
"""

from pyharness import Budget, Session


def _trace(kind: str, text: str) -> None:
    print(f"--- {kind} ---\n{text}\n")


def main() -> None:
    session = Session(".sessions/demo", budget=Budget(limit_usd=2.0), on_event=_trace)
    answer = session.run(
        "Write a Python script fib.py that prints the first 10 Fibonacci "
        "numbers, run it, and confirm the output looks correct."
    )
    print("\n=== ANSWER ===\n" + answer)
    print(f"\nspent ${session.budget.spent_usd:.4f}")


if __name__ == "__main__":
    main()
