"""Run the harness against a real task.

Requires ANTHROPIC_API_KEY and the `anthropic` extra:
    uv pip install -e '.[anthropic]'
    uv run python examples/demo.py
"""

from pathlib import Path

from pyharness import Agent, AnthropicLLM, Workspace


def main() -> None:
    workspace = Workspace(Path("./.sessions/demo"))
    agent = Agent(AnthropicLLM(), workspace)
    answer = agent.run(
        "Write a Python script fib.py that prints the first 10 Fibonacci "
        "numbers, run it, and confirm the output looks correct."
    )
    print("\n=== FINAL ANSWER ===\n" + answer)


if __name__ == "__main__":
    main()
