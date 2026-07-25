"""Scoring model for the adversarial suite.

The suite exists to produce a *number* — "N of M attacks blocked" — that is
honest enough to put in front of someone who will poke at it. Two failure modes
would make that number a lie, and this module is built around defending against
both.

**1. The tautological test.** An attack derived by reading the implementation
tends to assert what the code currently does rather than what it ought to do; it
passes by construction and proves nothing. The defense is `Attack.property`: a
statement of the security property in terms an outsider could check, written
without reference to how the code achieves it. If the property cannot be stated
that way, the attack is not worth counting.

**2. The accidental pass.** An attack that raises for an unrelated reason (a
typo, a missing fixture, a changed signature) looks from the outside exactly
like an attack the defense stopped. Silently scoring that as BLOCKED inflates
the number in the most dangerous direction — toward believing the system is
safer than it is. So an attack never reports BLOCKED by merely failing: it must
name the exception type that constitutes a legitimate refusal (see `blocked_by`).
Anything else is ERROR, which is reported separately and never counted as a win
for the defense.

`Attack.control` is the strongest available evidence against (1): a callable
that demonstrates the attack's mechanism actually works when the defense is
removed or weakened. Not every attack can have one, but an attack with a passing
control and a BLOCKED verdict is proof the test has teeth.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class Verdict(StrEnum):
    """Outcome of one attack.

    `ERROR` is deliberately not folded into either of the other two: an attack
    that broke is evidence about the *test*, not about the system, and pretending
    otherwise is how a suite drifts into reporting a number nobody should trust.
    """

    BLOCKED = "blocked"  # the defense refused it, in the way the attack expected
    SUCCEEDED = "succeeded"  # the attack got through
    ERROR = "error"  # the attack itself broke; says nothing about the defense


class AttackError(Exception):
    """Raised by an attack whose own machinery failed.

    Distinct from any exception the harness under test raises, so a broken attack
    can never be mistaken for a successful defense.
    """


def blocked_by(fn: Callable[[], object], *expected: type[BaseException]) -> Verdict:
    """Run an exploit attempt and classify the result.

    `expected` names the exception type(s) that constitute a *legitimate refusal*
    by the system under test. The attack author has to make that call explicitly,
    which is the whole point: it forces "how would I know the defense fired,
    versus the test merely fell over" to be answered up front rather than
    inferred from a green checkmark.

    Returns SUCCEEDED if the exploit ran to completion, BLOCKED if it raised one
    of `expected`, and raises AttackError otherwise so the runner records ERROR.
    """
    if not expected:
        raise AttackError(
            "blocked_by() needs at least one expected refusal type; an attack "
            "that treats any exception as a block cannot distinguish a working "
            "defense from a broken test"
        )
    try:
        fn()
    except expected:
        return Verdict.BLOCKED
    except Exception as exc:  # noqa: BLE001 — deliberately broad; see AttackError
        raise AttackError(
            f"attack machinery failed with {type(exc).__name__}: {exc}"
        ) from exc
    return Verdict.SUCCEEDED


@dataclass(frozen=True)
class Attack:
    """One scripted attack against the harness.

    A scripted adversary is stronger evidence than a model-driven red team for
    exactly one reason: the attack is chosen rather than stumbled into. That only
    holds if each attack is a deliberate statement about a property, hence the
    required `property` field.
    """

    id: str  # stable slug, e.g. "egress-redirect-to-blocked-host"
    surface: str  # policy | egress | sandbox | secrets | audit
    description: str  # what the attack does, one line
    property: str  # the security property it defends, stated WITHOUT reference
    # to the implementation. If this reads like a paraphrase of
    # the code, the attack is tautological — rewrite or drop it.
    run: Callable[[], Verdict]
    control: Callable[[], None] | None = None  # must raise if the attack's
    # mechanism is inert; proves the
    # test can actually fail
    known_gap: str | None = None  # set iff this attack is expected to SUCCEED
    # today; the string is the honest reason why


@dataclass(frozen=True)
class Result:
    attack: Attack
    verdict: Verdict
    detail: str = ""
    control_ok: bool | None = None  # None when the attack declares no control

    @property
    def as_expected(self) -> bool:
        """Did this match the documented expectation?

        Regression is bidirectional. An unexpected SUCCEEDED is a new hole. A
        `known_gap` that now reports BLOCKED is *also* a failure to report,
        because it means the gap was closed and the writeup still claims it is
        open — a suite that quietly under-claims stops being a source of truth
        just as surely as one that over-claims.
        """
        if self.verdict is Verdict.ERROR:
            return False
        expected = Verdict.SUCCEEDED if self.attack.known_gap else Verdict.BLOCKED
        return self.verdict is expected


def run_attack(attack: Attack) -> Result:
    """Execute one attack, plus its control if it declares one."""
    control_ok: bool | None = None
    if attack.control is not None:
        try:
            attack.control()
            control_ok = True
        except Exception as exc:  # noqa: BLE001
            # A failed control means the attack may be inert — it could be
            # "passing" because it never exercised anything. Surfaced, not fatal.
            return Result(attack, Verdict.ERROR, f"control failed: {exc}", False)

    try:
        verdict = attack.run()
    except AttackError as exc:
        return Result(attack, Verdict.ERROR, str(exc), control_ok)
    except Exception as exc:  # noqa: BLE001
        return Result(
            attack,
            Verdict.ERROR,
            f"uncaught {type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            control_ok,
        )
    return Result(attack, verdict, control_ok=control_ok)


def run_suite(attacks: list[Attack]) -> list[Result]:
    return [run_attack(a) for a in attacks]


def render(results: list[Result]) -> str:
    """Render the scoreboard.

    Deliberately reports four counts rather than a single pass rate: blocked,
    known gaps, unexpected successes, and errors. Collapsing those into one
    percentage is what makes security numbers untrustworthy — the interesting
    information is entirely in which bucket a failure landed.
    """
    blocked = [r for r in results if r.verdict is Verdict.BLOCKED and r.as_expected]
    gaps = [r for r in results if r.attack.known_gap and r.as_expected]
    holes = [
        r for r in results if r.verdict is Verdict.SUCCEEDED and not r.attack.known_gap
    ]
    errors = [r for r in results if r.verdict is Verdict.ERROR]
    closed = [r for r in results if r.attack.known_gap and r.verdict is Verdict.BLOCKED]

    width = max((len(r.attack.id) for r in results), default=10)
    lines = [
        "",
        f"  adversarial suite — {len(blocked)}/{len(results)} blocked, "
        f"{len(gaps)} known gaps, {len(holes)} unexpected, {len(errors)} errors",
        "",
    ]
    mark = {
        Verdict.BLOCKED: "block",
        Verdict.SUCCEEDED: "PASS ",
        Verdict.ERROR: "ERROR",
    }
    for r in sorted(results, key=lambda r: (r.attack.surface, r.attack.id)):
        flag = " " if r.as_expected else "!"
        ctl = {True: "c", False: "x", None: " "}[r.control_ok]
        lines.append(
            f"  {flag}{ctl} {mark[r.verdict]}  {r.attack.surface:<8} "
            f"{r.attack.id:<{width}}  {r.attack.description}"
        )

    if gaps:
        lines += ["", "  known gaps (attack succeeds; documented):"]
        lines += [f"    {r.attack.id}: {r.attack.known_gap}" for r in gaps]
    if holes:
        lines += ["", "  UNEXPECTED SUCCESS — undocumented hole:"]
        lines += [f"    {r.attack.id}: {r.attack.property}" for r in holes]
    if closed:
        lines += ["", "  gap now closed — remove the known_gap marker and update docs:"]
        lines += [f"    {r.attack.id}" for r in closed]
    if errors:
        lines += ["", "  errors (say nothing about the defense — fix the attack):"]
        lines += [f"    {r.attack.id}: {r.detail.splitlines()[0]}" for r in errors]
    lines.append("")
    return "\n".join(lines)


def regressions(results: list[Result]) -> list[Result]:
    """Results a CI gate should fail on: anything not matching expectation."""
    return [r for r in results if not r.as_expected]
