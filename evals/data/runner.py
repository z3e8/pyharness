"""The brokered arm: the real harness, pointed at the corpus.

Nothing here is special-cased for the suite. It builds a `Session` the way any
caller does, seeds the workspace, and reads the answer out of the session's own
digest — so a change that breaks the harness breaks this arm, which is the only
reason its result is worth anything.

The answer key is written to the session **root**, never into `workspace/`.
`Workspace.path` refuses any path that escapes the workspace directory, so the
agent cannot reach it even by trying; the isolation is the harness's own, not a
convention this file is trusting itself to keep.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .gen import Truth, generate
from .tasks import BUDGET_USD, MAX_STEPS, Verdict, judge, prompt


@dataclass
class ArmRun:
    """One arm's result, in the shape the board reads.

    Shared by the brokered and baseline arms so the renderer never has to ask
    which arm it is holding — the arms differ in what they can do, not in what
    is recorded about them.
    """

    arm: str
    answer: str = ""
    verdict: Verdict | None = None
    steps: int = 0
    cost_usd: float = 0.0
    errors: int = 0
    broke: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def score(self) -> int:
        return self.verdict.score if self.verdict else 0


def stage_corpus(root: Path, *, rows_per_day: int, seed: int = 20260601) -> Truth:
    """Generate the corpus once, into a staging directory no arm runs against.

    Arms get their own copy (`give_corpus`) rather than sharing this one. The
    `shell` arm can write files, and a scratch file left behind by one arm and
    read by the next would silently turn a comparison into a relay — an arm
    would be scored on work another arm did.

    **The answer key is never written while an arm is running.** It is returned,
    held in memory, and persisted by `save_truth` only once every arm has
    finished. The first draft put it in the session root on the theory that
    `Workspace.path` would keep the agent inside `workspace/` — it does not.
    That guard governs the *broker's* file capability; code in the kernel calls
    `open()` directly and reads whatever the process can, so `../../truth.json`
    read the key and the isolation this suite's scores depend on was decorative.
    Not writing it is the only version of this that does not rest on an
    assumption about someone else's sandbox.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    return generate(root / "corpus", seed=seed, rows_per_day=rows_per_day)


def save_truth(root: Path, truth: Truth) -> Path:
    """Persist the key, once the arms are done, so a run can be re-scored by
    hand without regenerating 50MB. Never call this before an arm runs."""
    path = Path(root) / "truth.json"
    path.write_text(json.dumps(truth.as_dict(), indent=2))
    return path


def give_corpus(root: Path, dest: Path) -> Path:
    """Hand one arm a private copy of the staged corpus."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(Path(root) / "corpus", dest)
    return dest


def run_brokered(
    root: Path,
    truth: Truth,
    *,
    llm=None,
    tier: str = "mid",
) -> ArmRun:
    """Run the task through the real harness.

    `llm=None` builds a real client and costs money; the tests pass a scripted
    double, which is how everything here except the model call is exercised
    offline.
    """
    from pyharness.budget import Budget
    from pyharness.core.session import Session
    from pyharness.obs.transcript import session_digest

    run = ArmRun(arm="brokered")
    session = Session(
        root,
        llm=llm,
        budget=Budget(limit_usd=BUDGET_USD),
        tier=tier,
        max_steps=MAX_STEPS,
        # Hermetic, for the same reason the demo suite is: a result that depends
        # on the operator's home directory is not a result.
        skills_dir=Path(root) / "skills",
        index_db=None,
        mcp_config=None,
    )
    try:
        session.run(prompt())
    except Exception as exc:  # noqa: BLE001 — recorded, then scored
        run.broke = f"{type(exc).__name__}: {exc}"
    finally:
        session.close()

    digest = session_digest(root)
    run.answer = str(digest.get("answer") or "")
    run.steps = int(digest.get("steps") or 0)
    run.cost_usd = float(digest.get("cost_usd") or 0.0)
    run.errors = int(digest.get("errors") or 0)
    run.verdict = judge(run.answer, truth)
    return run
