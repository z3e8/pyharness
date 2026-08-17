from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class GrantScope:
    """What a grant covers: one harness-defined action class on one concrete
    target. Built only by capabilities from the structured call (via their
    `scope()` hook) — never from agent- or page-supplied text. Matching is exact
    equality on every field; there are no wildcards, so a scope can only ever be
    minted from, and matched against, a concrete real target."""

    action_class: str  # capability-defined constant, e.g. "http" | "browser" | "mcp"
    target: str  # concrete target: a normalized lowercase hostname (http/browser)
    # or an MCP "server.tool" pair ("mcp") — always harness-derived
    pin: str | None = None
    # An opaque digest of the *description the human was shown*, for targets whose
    # meaning is supplied by something outside the harness. An MCP server declares
    # its own tool names and annotations and can change them mid-session, so the
    # mcp scope pins them: re-declare, and the pin no longer matches, and the human
    # is asked again rather than silently covered by an approval of the old thing.
    # None for targets the harness derives entirely on its own (a hostname).


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
    nothing on disk to protect or invalidate.

    These are the raw, *unaudited* ledger primitives. Withdrawing a grant is a
    security-relevant state change, so the sanctioned way to do it is
    `Broker.revoke_grant`, which mutates this ledger and records the withdrawal
    in the session's hash chain."""

    def __init__(self) -> None:
        self._grants: dict[str, Grant] = {}
        # Every method here mutates — the reads prune expired grants as they go —
        # and revocation is now callable from a thread other than the one
        # dispatching (an embedder holding the Session while the agent runs;
        # spawned children drive parent-side threads). Unlocked, a prune racing a
        # pop of the same id raises KeyError inside a security check. No method
        # calls another, so a plain lock suffices.
        self._lock = threading.Lock()

    def add(self, scope: GrantScope, *, ttl_s: float | None = None) -> Grant:
        now = time.time()
        grant = Grant(
            id=uuid4().hex[:12],
            scope=scope,
            issued_at=now,
            expires_at=(now + ttl_s) if ttl_s else None,
        )
        with self._lock:
            self._grants[grant.id] = grant
        return grant

    def find(self, scope: GrantScope) -> Grant | None:
        """Return a live grant whose scope matches exactly, or None. Prunes any
        expired grants encountered along the way."""
        now = time.time()
        with self._lock:
            for grant in list(self._grants.values()):
                if grant.expires_at is not None and grant.expires_at <= now:
                    self._grants.pop(grant.id, None)
                    continue
                if grant.scope == scope:
                    return grant
        return None

    def revoke(self, grant_id: str) -> Grant | None:
        """Drop one grant by id, returning it (or None if it wasn't live). Raw
        and unaudited — call `Broker.revoke_grant` instead."""
        with self._lock:
            return self._grants.pop(grant_id, None)

    def clear(self) -> None:
        """Drop every grant. Raw and unaudited; see `revoke`."""
        with self._lock:
            self._grants.clear()

    def active(self) -> list[Grant]:
        """Live (unexpired) grants, pruning expired ones."""
        now = time.time()
        with self._lock:
            for grant in list(self._grants.values()):
                if grant.expires_at is not None and grant.expires_at <= now:
                    self._grants.pop(grant.id, None)
            return list(self._grants.values())
