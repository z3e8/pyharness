from __future__ import annotations

from ...audit import AuditLog


class HistoryCapability:
    """Lets the agent read its own action record — the reflection half of the
    do -> observe -> revise loop.

    The audit log lives at the session root, outside the workspace the agent's
    `read` is confined to, so the agent cannot reach it with the file builtins;
    this exposes it (read-only) parent-side. It is the record of *side effects* —
    every capability call, its decision (allow/approve/deny), whether it
    succeeded, and a secret-safe summary of what was sent — so the agent can
    check "did that POST land, was the click approved" and revise a skill from
    what actually happened. The agent's own cells and notes are already in its
    context (the trace log), so only the audit is surfaced here."""

    name = "history"

    def __init__(self, audit: AuditLog):
        self.audit = audit

    def exports(self) -> dict:
        return {"history": self.history}

    def history(self, limit: int = 20, action: str | None = None) -> list[dict]:
        """Your most recent actions (oldest first): each is `{ts, action,
        decision?, ok?, args?, error?, ...}`. `action` filters by prefix — pass
        `"http"` for every HTTP call, `"browser.click"` for just clicks. Use it to
        confirm an effect landed or to see why an action was refused."""
        return self.audit.tail(limit, action=action)
