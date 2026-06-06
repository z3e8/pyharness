"""Date and time helpers."""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def to_epoch(iso_timestamp: str) -> float:
    """Convert an ISO-8601 timestamp to Unix epoch seconds."""
    return datetime.fromisoformat(iso_timestamp).timestamp()
