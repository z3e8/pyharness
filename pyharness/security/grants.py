from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class GrantScope:
    """What a grant covers: one harness-defined action class on one concrete
    target host. Built only by capabilities from the structured call (via their
    `scope()` hook) — never from agent- or page-supplied text. Matching is exact
    equality on both fields; there are no wildcards, so a scope can only ever be
    minted from, and matched against, a concrete real host."""

    action_class: str  # capability-defined constant, e.g. "http" | "browser"
    target: str  # normalized lowercase hostname, e.g. "boards.greenhouse.com"


@dataclass(frozen=True)
class Grant:
    """A human's standing approval for one `GrantScope`, minted at an approval
    prompt. `expires_at is None` means it lives until the Session (and its
    Broker) ends; grants are never persisted across sessions."""

    id: str
    scope: GrantScope
    issued_at: float
    expires_at: float | None


class GrantLedger:
    """The live set of scoped approval grants, owned by the Broker and consulted
    on the APPROVE path before the human is prompted. In-memory only: it dies
    with the Session, so "allow for this session" is literally true and there is
    nothing on disk to protect or invalidate."""

    def __init__(self) -> None:
        self._grants: dict[str, Grant] = {}

    def add(self, scope: GrantScope, *, ttl_s: float | None = None) -> Grant:
        now = time.time()
        grant = Grant(
            id=uuid4().hex[:12],
            scope=scope,
            issued_at=now,
            expires_at=(now + ttl_s) if ttl_s else None,
        )
        self._grants[grant.id] = grant
        return grant

    def find(self, scope: GrantScope) -> Grant | None:
        """Return a live grant whose scope matches exactly, or None. Prunes any
        expired grants encountered along the way."""
        now = time.time()
        for grant in list(self._grants.values()):
            if grant.expires_at is not None and grant.expires_at <= now:
                del self._grants[grant.id]
                continue
            if grant.scope == scope:
                return grant
        return None

    def revoke(self, grant_id: str) -> Grant | None:
        return self._grants.pop(grant_id, None)

    def clear(self) -> None:
        self._grants.clear()

    def active(self) -> list[Grant]:
        """Live (unexpired) grants, pruning expired ones."""
        now = time.time()
        for grant in list(self._grants.values()):
            if grant.expires_at is not None and grant.expires_at <= now:
                del self._grants[grant.id]
        return list(self._grants.values())
