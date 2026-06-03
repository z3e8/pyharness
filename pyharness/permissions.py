from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch


class PermissionDenied(Exception):
    """Raised when the policy denies a capability request."""


@dataclass
class RulePolicy:
    """Fine-grained allow/deny over `action:resource` keys, matched by glob.

    A request is denied unless it matches an `allow` pattern; any matching
    `deny` pattern overrides allow. Example keys:
        bash:ls -la
        file.write:/Users/me/proj/workspace/main.py
        http.get:https://example.com
        llm:smart
    """

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)

    @classmethod
    def allow_all(cls) -> "RulePolicy":
        return cls(allow=["*"])

    def check(self, action: str, resource: str) -> None:
        key = f"{action}:{resource}"
        if any(fnmatch(key, p) for p in self.deny):
            raise PermissionDenied(f"denied by policy: {key}")
        if not any(fnmatch(key, p) for p in self.allow):
            raise PermissionDenied(f"not permitted: {key}")
