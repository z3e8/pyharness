"""Lessons: small cross-session facts distilled from experience.

A lesson is one sentence of operational knowledge that isn't a procedure
("greenhouse boards paginate at 100", "the corp VPN blocks PyPI") — the seed of
the facts memory (gap G6). The store is deliberately boring: one JSON file of
entries with stable ids and observation counters, ACE-style, updated by delta —
an existing lesson is counted up, never rewritten, so accumulated knowledge
can't be collapsed by a bad regeneration.

One run is not evidence (a lesson may be an artifact of that session), so a
lesson is only *established* — surfaced ambiently in the session preamble —
once it has been observed in `ESTABLISHED_AT` distinct sessions. Candidates
below that bar sit in the file, waiting to recur or rot.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .util import ensure_private_dir

DEFAULT_PATH = "~/.pyharness/lessons.json"

ESTABLISHED_AT = 2  # distinct sessions that must observe a lesson before it surfaces
_MAX_LESSONS = 200  # hard cap; oldest-seen candidates are dropped first past it


def _normalize(text: str) -> str:
    return re.sub(r"\W+", " ", text).strip().lower()


def load(path: str | Path) -> list[dict]:
    """All lessons (established and candidates), oldest first. Unreadable or
    absent files are an empty store — the file is advisory, never load-bearing."""
    path = Path(path).expanduser()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return list(data.get("lessons", []))
    except (json.JSONDecodeError, OSError):
        return []


def _save(path: Path, lessons: list[dict]) -> None:
    if len(lessons) > _MAX_LESSONS:
        lessons = sorted(lessons, key=lambda e: (-e["count"], -e["last_at"]))[
            :_MAX_LESSONS
        ]
        lessons.sort(key=lambda e: e["first_at"])
    ensure_private_dir(path.parent)  # ~/.pyharness (0700)
    path.write_text(json.dumps({"lessons": lessons}, indent=2) + "\n")


def record(path: str | Path, text: str, *, session: str = "") -> dict:
    """Record one observation of a lesson. A lesson matching an existing one
    (normalized text) increments its counter — delta update, never a rewrite;
    a new one enters as a candidate with count 1. Returns the entry."""
    text = " ".join(str(text).split())
    if not text:
        raise ValueError("empty lesson")
    path = Path(path).expanduser()
    lessons = load(path)
    now = time.time()
    key = _normalize(text)
    for entry in lessons:
        if _normalize(entry["text"]) == key:
            # A session re-observing its own lesson is one observation, not two.
            if session and session in entry.get("sessions", []):
                return entry
            entry["count"] += 1
            entry["last_at"] = now
            if session:
                entry.setdefault("sessions", []).append(session)
            _save(path, lessons)
            return entry
    entry = {
        "id": f"L{int(now * 1000):x}",
        "text": text,
        "count": 1,
        "first_at": now,
        "last_at": now,
        "sessions": [session] if session else [],
    }
    lessons.append(entry)
    _save(path, lessons)
    return entry


def established(path: str | Path) -> list[dict]:
    """Lessons observed in enough distinct sessions to trust ambiently."""
    return [e for e in load(path) if e.get("count", 0) >= ESTABLISHED_AT]
