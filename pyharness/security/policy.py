from __future__ import annotations

from enum import Enum


class Decision(Enum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVE = "approve"


class Policy:
    """Decides whether a capability call may proceed.

    The only place a side effect is judged. An action is identified as
    "<capability>.<operation>" (e.g. "files.write", "shell.bash"). Rules match by
    prefix, so "files" gates every file operation. Default is allow; tighten by
    listing actions under `deny` or `require_approval`.
    """

    def __init__(
        self,
        *,
        deny: set[str] | None = None,
        require_approval: set[str] | None = None,
    ):
        self.deny = deny or set()
        self.require_approval = require_approval or set()

    @staticmethod
    def _matches(action: str, rules: set[str]) -> bool:
        return any(action == r or action.startswith(r + ".") or action.split(".")[0] == r for r in rules)

    def decide(self, action: str, args: tuple, kwargs: dict) -> Decision:
        if self._matches(action, self.deny):
            return Decision.DENY
        if self._matches(action, self.require_approval):
            return Decision.APPROVE
        return Decision.ALLOW
