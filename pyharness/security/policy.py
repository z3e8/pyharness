from __future__ import annotations

from enum import Enum
from typing import Callable


class Decision(Enum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVE = "approve"


# A predicate judges a call from its full context, not just its name — so a
# single action ("http.request") can be gated on an argument (the HTTP method).
# Returns True to force approval. Kept general: nothing here is HTTP-specific.
Predicate = Callable[[str, tuple, dict], bool]


class Policy:
    """Decides whether a capability call may proceed.

    The only place a side effect is judged. An action is identified as
    "<capability>.<operation>" (e.g. "files.write", "shell.bash"). Rules match by
    prefix, so "files" gates every file operation. Default is allow; tighten by
    listing actions under `deny` or `require_approval`, or by adding an
    `approve_if` predicate for gates that depend on the arguments.
    """

    def __init__(
        self,
        *,
        deny: set[str] | None = None,
        require_approval: set[str] | None = None,
        approve_if: list[Predicate] | None = None,
    ):
        self.deny = deny or set()
        self.require_approval = require_approval or set()
        self.approve_if = list(approve_if or [])

    @staticmethod
    def _matches(action: str, rules: set[str]) -> bool:
        return any(action == r or action.startswith(r + ".") or action.split(".")[0] == r for r in rules)

    def decide(self, action: str, args: tuple, kwargs: dict) -> Decision:
        if self._matches(action, self.deny):
            return Decision.DENY
        if self._matches(action, self.require_approval) or any(
            pred(action, args, kwargs) for pred in self.approve_if
        ):
            return Decision.APPROVE
        return Decision.ALLOW
