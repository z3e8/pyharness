"""The adversarial suite as a CI gate.

**Why this runs in `make test`.** An attack suite that only runs when someone
remembers to run it is a snapshot, not a guarantee: the defenses it describes
get refactored and nobody finds out until the number is quoted at an interview.
The usual argument against gating commits on adversarial tests — flakiness — does
not apply here. Nothing in the suite touches the network, calls a model, or
asserts on timing: the transport and the DNS seam are both replaced, and the
whole suite runs in a few seconds. So it is a plain deterministic test.

**What failing means.** The gate is `regressions()`, which is bidirectional. A
new hole fails, obviously. But a `known_gap` that starts reporting BLOCKED fails
too, because the gap has been closed while `SCOREBOARD.md` and the threat-model
writeup still say it is open — a suite that quietly under-claims stops being a
source of truth just as surely as one that over-claims. Either way the fix is
the same: re-run `make evals`, commit the regenerated scoreboard, and update the
prose that quotes it.
"""

from __future__ import annotations

import re
from pathlib import Path

from evals.attacks import ATTACKS
from evals.scoreboard import Verdict, regressions, render, run_suite

_ROOT = Path(__file__).resolve().parents[1]

# Prose that quotes the suite, and the only places allowed to. Anything else
# should link to the scoreboard rather than restate a number.
_PROSE = [_ROOT / "README.md", *sorted((_ROOT / "docs").rglob("*.md"))]

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}  # fmt: skip

# "32 of 43 attacks blocked" — both halves at once.
_N_OF_M = re.compile(r"(\d+) of (\d+)\s+(?:[a-z]+\s+)?attacks", re.I)
# The number immediately in front of the noun: "the 43 attacks", "11 known
# gaps", "four of the ten gaps". This is the shape that actually rotted.
_BEFORE_NOUN = re.compile(
    r"(\d+|[a-z]+)\s+(?:known\s+|published\s+)?(attacks|gaps)\b", re.I
)


def test_no_attack_deviates_from_its_documented_expectation():
    results = run_suite(ATTACKS)
    bad = regressions(results)
    assert not bad, render(results)


def test_every_attack_states_a_property_and_a_unique_id():
    """The discipline the number depends on, enforced rather than trusted."""
    ids = [attack.id for attack in ATTACKS]
    assert len(ids) == len(set(ids)), "duplicate attack ids"
    for attack in ATTACKS:
        assert len(attack.property) >= 60, (
            f"{attack.id}: the security property is too thin to be a claim a "
            "sceptical reader could check"
        )
        assert attack.description, f"{attack.id}: no description"


def test_every_known_gap_carries_a_rationale():
    """A gap without a stated reason reads as unfinished work; the whole point
    of publishing them is that each one is a boundary somebody chose."""
    for attack in ATTACKS:
        if attack.known_gap:
            assert len(attack.known_gap) >= 200, (
                f"{attack.id}: a known gap needs a real rationale — what the "
                "boundary is, why it is where it is, and what bounds it"
            )


def test_the_suite_contains_expected_blocked_attacks():
    """A suite that only finds holes is cherry-picking. The blocked-expected
    attacks are what make the gaps believable."""
    blocked_expected = [a for a in ATTACKS if not a.known_gap]
    gaps = [a for a in ATTACKS if a.known_gap]
    assert len(blocked_expected) > len(gaps)
    surfaces = {a.surface for a in blocked_expected}
    assert surfaces == {a.surface for a in ATTACKS}, (
        "some surface is represented only by its gaps — every surface needs at "
        "least one attack the defense stops"
    )


def test_the_prose_quotes_the_suite_s_real_numbers():
    """The counts in the docs are copied by hand, and one of them rotted: the
    threat model claimed `30 of 40` and `four of the ten gaps` for three days
    after the board moved to 32/43 with eleven. `make evals` regenerates
    `SCOREBOARD.md`; nothing regenerated the prose that quotes it. On a project
    whose claim is that the number cannot rot silently, a rotted number is the
    worst thing a sceptical reader can find, so the prose is now checked against
    the live suite rather than against the last time somebody remembered.

    Only the counts are checked. Prose that argues about the gaps is not
    something a test can hold, which is why the fix for a failure here is to
    re-read the section, not to bump a digit.
    """
    results = run_suite(ATTACKS)
    total = len(results)
    blocked = sum(1 for r in results if r.verdict is Verdict.BLOCKED and r.as_expected)
    gaps = sum(1 for r in results if r.attack.known_gap and r.as_expected)
    allowed = {"attacks": total, "gaps": gaps}

    stale: list[str] = []
    for path in _PROSE:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            where = f"{path.relative_to(_ROOT)}:{lineno}"
            for claimed_blocked, claimed_total in _N_OF_M.findall(line):
                if (int(claimed_blocked), int(claimed_total)) != (blocked, total):
                    stale.append(
                        f"{where}: says {claimed_blocked} of {claimed_total} "
                        f"attacks blocked; the suite reports {blocked} of {total}"
                    )
            for token, noun in _BEFORE_NOUN.findall(line):
                count = (
                    int(token) if token.isdigit() else _NUMBER_WORDS.get(token.lower())
                )
                if count is not None and count != allowed[noun.lower()]:
                    stale.append(
                        f"{where}: says {token} {noun}; there are "
                        f"{allowed[noun.lower()]}"
                    )
    assert not stale, "\n".join(
        ["prose quotes numbers the suite no longer reports:", *stale]
    )


def test_no_attack_is_credited_for_a_broken_control():
    """Restates the scorer's contract on the live suite: a failed control is an
    ERROR, never a BLOCKED."""
    for result in run_suite(ATTACKS):
        if result.control_ok is False:
            assert result.verdict is Verdict.ERROR
