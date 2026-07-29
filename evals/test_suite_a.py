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

from evals.attacks import ATTACKS
from evals.scoreboard import Verdict, regressions, render, run_suite


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


def test_no_attack_is_credited_for_a_broken_control():
    """Restates the scorer's contract on the live suite: a failed control is an
    ERROR, never a BLOCKED."""
    for result in run_suite(ATTACKS):
        if result.control_ok is False:
            assert result.verdict is Verdict.ERROR
