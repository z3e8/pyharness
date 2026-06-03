"""Run the harness against a real task.

Requires ANTHROPIC_API_KEY and the `anthropic` extra:
    uv pip install -e '.[anthropic]'
    uv run python examples/demo.py
"""

from pathlib import Path

from pyharness import AnthropicProvider, Master, RulePolicy, Session


def main() -> None:
    session = Session(Path("./.sessions/demo"))
    ws = session.workspace
    policy = RulePolicy(
        allow=[
            "bash:*",
            f"file.read:{ws}/*",
            f"file.write:{ws}/*",
            f"file.edit:{ws}/*",
            "search:*",
            "llm:*",
        ]
    )
    master = Master(AnthropicProvider(), session, policy)
    answer = master.run(
        "Write a Python script fib.py that prints the first 10 Fibonacci "
        "numbers, run it, and confirm the output looks correct."
    )
    print("\n=== FINAL ANSWER ===\n" + answer)


if __name__ == "__main__":
    main()
