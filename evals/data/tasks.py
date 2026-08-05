"""The task, and how an answer is scored against the key.

One task, not a set. The suite's claim is about a single property — whether an
agent can work at a volume that does not fit in its context, and whether it
reads the data before trusting it — and a second task that exercised the same
property would add cost without adding evidence.

**The prompt tells all three arms the same thing.** In particular it says to
trust the data only as far as it earns it. That is a hint, deliberately given,
and it is given identically to every arm: without it the task is unfair (nothing
would lead an agent to suspect a corpus), and because it is constant across arms
it cannot explain a difference between them. What it never does is name a
defect, a day, or a field.

**Answers are three integers, parsed by regex.** Not a judged summary. The
questions were chosen so a correct answer is an integer rather than a value with
a tolerance — a day, an hour, a count — which removes both float comparison and
a model grading a model from the scoring path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .gen import SLO_MS, Truth

# The knobs a run is allowed to spend. Generous: an arm that loses by running
# out of room has measured the ceiling, not the action space.
BUDGET_USD = 2.00
MAX_STEPS = 40
MAX_TURNS = 40

PROMPT = """\
The directory `logs/` holds one gzipped NDJSON file per day for June 2026 — one
JSON object per HTTP request served by our edge. Answer three questions about
the month as a whole:

1. How many requests were served in total?
2. `/checkout` has a p95 latency SLO of {slo} ms. On which day of June did p95
   for that route first breach it?
3. Which hour of the day, in UTC, carried the most traffic across the month?

The data comes from a production export and has not been cleaned. Trust it only
as far as it earns it, and say what you had to correct for.

Finish your reply with exactly these three lines, in this order, and nothing
after them:

requests=<integer>
breach_day=<integer, day of June>
peak_hour=<integer, 0-23>
"""


def prompt() -> str:
    return PROMPT.format(slo=SLO_MS)


_FIELDS = ("requests", "breach_day", "peak_hour")


def parse_answer(text: str) -> dict[str, int | None]:
    """Pull the three integers out of a final answer.

    Tolerant of prose around the lines and of the model restating them, because
    scoring an arm as wrong for formatting would measure instruction-following
    rather than the thing under test. The *last* occurrence of each key wins: a
    model that shows its working and then states a conclusion is answering with
    the conclusion.
    """
    found: dict[str, int | None] = dict.fromkeys(_FIELDS)
    for field_name in _FIELDS:
        matches = re.findall(
            rf"{field_name}\s*[=:]\s*([0-9][0-9,_]*)", text, flags=re.IGNORECASE
        )
        if matches:
            found[field_name] = int(matches[-1].replace(",", "").replace("_", ""))
    return found


@dataclass(frozen=True)
class Verdict:
    """How one arm did, question by question.

    `naive` is tracked separately from plain wrongness on purpose. An arm that
    returns the number a competent-looking shortcut produces has told us
    something specific — which defect ate it — and an arm that returns neither
    the right answer nor the reachable wrong one has told us something else.
    Collapsing both into "incorrect" would throw the attribution away.
    """

    answers: dict[str, int | None]
    correct: tuple[str, ...]
    naive: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def score(self) -> int:
        return len(self.correct)

    def defects_neutralised(self, truth: Truth) -> tuple[str, ...]:
        """Which planted defects this arm demonstrably handled.

        Derived from the answers, never from the prose. A model can claim to
        have found a timezone problem and still report the wrong hour; only the
        integer is evidence.
        """
        handled: set[str] = set()
        for question in self.correct:
            handled.update(truth.probes.get(question, ()))
        return tuple(sorted(handled))


def judge(text: str, truth: Truth) -> Verdict:
    answers = parse_answer(text)
    expected = {
        "requests": truth.requests,
        "breach_day": truth.breach_day,
        "peak_hour": truth.peak_hour,
    }
    shortcut = {
        "requests": truth.naive_requests,
        "breach_day": truth.naive_breach_day,
        "peak_hour": truth.naive_peak_hour,
    }
    correct, naive, missing = [], [], []
    for name in _FIELDS:
        value = answers[name]
        if value is None:
            missing.append(name)
        elif value == expected[name]:
            correct.append(name)
        elif value == shortcut[name]:
            naive.append(name)
    return Verdict(
        answers=answers,
        correct=tuple(correct),
        naive=tuple(naive),
        missing=tuple(missing),
    )
