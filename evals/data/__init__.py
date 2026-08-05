"""The throughput suite: can the agent work at a volume it cannot read?

The other suites measure what the harness *refuses*. This one measures what it
can *do* — the claim the whole project rests on, which is that a Python action
space beats fine-grained tool calls once the data stops fitting in a context
window.

One task, three arms, three integer answers. A month of gzipped service logs
(~50MB uncompressed, roughly ten context windows) with five defects planted in
it, each load-bearing for exactly one question, none named in the prompt. A
shortcut that trusts the data produces a specific set of plausible wrong
numbers, which is what makes a correct answer evidence rather than arithmetic.

- `gen.py` — the corpus generator and the answer key, derived by reading the
  written bytes back rather than from the generator's intent
- `tasks.py` — the prompt and the scorer
- `runner.py` — the brokered arm, built the way any caller builds a `Session`
- `baseline.py` — two control arms: conventional file tools, and a shell
- `run.py` — `python -m evals.data.run`, and the board

The corpus is never committed; it regenerates from a seed. `BOARD.md` is, and is
dated, because refreshing it costs a real model call in each arm.
"""
