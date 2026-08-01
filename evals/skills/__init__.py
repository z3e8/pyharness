"""Suite D — the skill cost curve.

One task, five runs, one shared skills root. Run 1 has no skill and authors one;
runs 2-5 find it in the preamble and reuse it. The question is whether the second
and later runs are actually cheaper, and the answer is read out of the
`skill_run_costs` view `pyharness/obs/index.py` already ships rather than out of
a bespoke measurement.

It is deliberately *not* part of the demo suite's board. That board reports what
a model does when a page attacks it; this one reports what repetition costs, and
the two have nothing to say about each other.

Entry point:

    python -m evals.skills.run          # five runs against a real model (paid)

`evals/test_skills.py` drives everything except the model call with a scripted
agent, offline and under `make test`.
"""

from .runner import TASK, RunRow, curve, gather, run_once

__all__ = ["TASK", "RunRow", "curve", "gather", "run_once"]
