"""Tests for the scorer itself.

Every number this project reports about its own security passes through
`scoreboard.py`, so a bug here silently corrupts the headline claim. These tests
pin the three classifications that are easy to get wrong and dangerous to get
wrong: a broken attack must never read as a working defense, a documented gap
must not read as a failure, and a gap that got fixed must not stay silently
documented as open.
"""

from __future__ import annotations

import pytest

from evals.scoreboard import (
    Attack,
    AttackError,
    Verdict,
    blocked_by,
    regressions,
    render,
    run_attack,
    run_suite,
)


class Refused(Exception):
    """Stands in for a policy refusal from the harness."""


def _attack(name, run, **kw):
    return Attack(
        id=name,
        surface="test",
        description=f"synthetic {name}",
        property="synthetic property",
        run=run,
        **kw,
    )


def test_expected_refusal_is_blocked():
    def run():
        return blocked_by(lambda: (_ for _ in ()).throw(Refused()), Refused)

    assert run_attack(_attack("a", run)).verdict is Verdict.BLOCKED


def test_exploit_running_to_completion_is_success():
    assert run_attack(
        _attack("a", lambda: blocked_by(lambda: None, Refused))
    ).verdict is (Verdict.SUCCEEDED)


def test_unrelated_exception_is_error_not_blocked():
    """The most important case: a broken attack must not look like a defense."""

    def run():
        # TypeError is not what the defense raises — the attack itself is broken.
        return blocked_by(lambda: "x" + 1, Refused)  # type: ignore[operator]

    result = run_attack(_attack("a", run))
    assert result.verdict is Verdict.ERROR
    assert result.verdict is not Verdict.BLOCKED
    assert "TypeError" in result.detail


def test_blocked_by_rejects_a_catch_all():
    with pytest.raises(AttackError):
        blocked_by(lambda: None)


def test_known_gap_succeeding_is_expected():
    result = run_attack(
        _attack(
            "a", lambda: blocked_by(lambda: None, Refused), known_gap="not fixed yet"
        )
    )
    assert result.verdict is Verdict.SUCCEEDED
    assert result.as_expected
    assert not regressions([result])


def test_closed_gap_is_flagged_so_docs_get_updated():
    """A fixed gap still marked known_gap under-claims — also a regression."""

    def run():
        return blocked_by(lambda: (_ for _ in ()).throw(Refused()), Refused)

    result = run_attack(_attack("a", run, known_gap="stale marker"))
    assert result.verdict is Verdict.BLOCKED
    assert not result.as_expected
    assert regressions([result])
    assert "remove the known_gap marker" in render([result])


def test_failed_control_errors_rather_than_scoring_the_attack():
    """An inert attack must not be credited to the defense."""

    def control():
        raise RuntimeError("mechanism inert")

    result = run_attack(
        _attack("a", lambda: blocked_by(lambda: None, Refused), control=control)
    )
    assert result.verdict is Verdict.ERROR
    assert result.control_ok is False


def test_error_is_never_as_expected():
    result = run_attack(_attack("a", lambda: (_ for _ in ()).throw(ValueError("boom"))))
    assert result.verdict is Verdict.ERROR
    assert not result.as_expected


def test_render_reports_four_buckets_separately():
    def refused():
        return blocked_by(lambda: (_ for _ in ()).throw(Refused()), Refused)

    results = run_suite(
        [
            _attack("blocked", refused),
            _attack("gap", lambda: blocked_by(lambda: None, Refused), known_gap="open"),
            _attack("hole", lambda: blocked_by(lambda: None, Refused)),
            _attack("broken", lambda: (_ for _ in ()).throw(ValueError("x"))),
        ]
    )
    out = render(results)
    assert "1/4 blocked, 1 known gaps, 1 unexpected, 1 errors" in out
    assert "UNEXPECTED SUCCESS" in out
    assert len(regressions(results)) == 2  # the hole and the broken attack
