"""The demo suite — paired benign/hostile twins.

Sits alongside the adversarial suite (`evals/attacks/`) and deliberately does not
share its scoreboard. The adversarial suite scores *scripted* attacks against the
broker with no model involved; this one scores what a real model does when a page
tries to turn it against its operator. Folding the two numbers together would
make "30 of 40 attacks blocked" mean something different depending on whether an
API key was present, and that number is a committed artifact.

Entry points:

    python -m evals.demo.run              # run the suite (needs an API key)
    python -m evals.demo.run capture …    # pin live pages into the corpus

`evals/test_demo.py` exercises everything except the model call itself, offline
and under `make test`.
"""

from .runner import Outcome, TaskRun, TwinResult, run_task, run_twin, score
from .server import CorpusServer
from .tasks import TWINS, Kind, Task, Twin

__all__ = [
    "TWINS",
    "CorpusServer",
    "Kind",
    "Outcome",
    "Task",
    "TaskRun",
    "Twin",
    "TwinResult",
    "run_task",
    "run_twin",
    "score",
]
