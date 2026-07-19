from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

_GENESIS = ""
_ANCHOR_SUFFIX = ".anchor"


class AuditLog:
    """Append-only, tamper-evident JSONL record of every capability call.

    This is both the safety record and the primary debugging trail. Secrets are
    never arguments to capabilities (they are referenced by name and injected in
    the parent), so logged arguments are safe to persist.

    Each entry carries a hash chain: ``hash = sha256(prev_hash + entry)`` and
    ``prev`` points at the previous entry's hash. Any edit, deletion, or
    reordering *within* the file breaks the chain, so the record is verifiable
    (see :func:`verify_chain`) — important once this log is shipped off-box.

    A plain hash chain from an empty genesis has one weakness: deleting the last
    N entries (tail truncation) leaves a shorter but still-valid chain, and
    anyone who can rewrite the whole file can re-link a forged chain from
    scratch. To raise that bar, each session also maintains a small **anchor**
    sidecar (``<path>.anchor``) holding the current entry count and head hash;
    :func:`verify_chain` cross-checks it, so a naive truncation or full rewrite
    of the log *alone* is caught (the count/head no longer match). It is not a
    keyed MAC: an attacker who can rewrite *both* the log and its anchor can
    still forge a consistent pair — a real cryptographic root needs a key store
    this harness does not have. See docs/explanation/security-and-audit.md.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev, self._count = _scan(self.path)
        # One chain serves the whole session tree, and spawned children run in
        # parent-side threads — the read-hash/append/advance sequence must be
        # atomic or concurrent writers fork the chain.
        self._lock = threading.Lock()

    def record(self, **fields: object) -> None:
        # Plain append (no fsync/rename): a crash mid-write can leave a torn
        # final line. That is tolerated, not prevented — `verify_chain` treats a
        # broken/torn final line as a broken chain (returns the bad index) rather
        # than raising, and `_last_hash`/`tail` skip an unparseable line, so a
        # reopened log continues cleanly from the last intact entry.
        with self._lock:
            entry = {"ts": time.time(), **fields}
            payload = json.dumps(entry, default=str, sort_keys=True)
            entry["prev"] = self._prev
            entry["hash"] = hashlib.sha256((self._prev + payload).encode()).hexdigest()
            with self.path.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
            self._prev = entry["hash"]
            self._count += 1
            _write_anchor(self.path, self._prev, self._count)

    def tail(self, limit: int = 20, action: str | None = None) -> list[dict]:
        """The most recent audited calls (oldest first), so the agent can reflect
        on what it did — what it sent, where, whether it was allowed. The internal
        chain fields (`hash`/`prev`) are dropped, as are the `phase: "start"`
        intent records (each call writes two chained records; the agent reads
        outcomes, so only completed calls appear here); `action` filters by
        prefix (`"http"` for every HTTP call). Arguments are already the
        log-safe summary (secrets are referenced by name), so entries are safe
        to hand back."""
        if not self.path.exists():
            return []
        entries: list[dict] = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry.pop("hash", None)
            entry.pop("prev", None)
            if entry.pop("phase", None) == "start":
                continue
            if action and not str(entry.get("action", "")).startswith(action):
                continue
            entries.append(entry)
        return entries[-limit:]


def _scan(path: Path) -> tuple[str, int]:
    """The hash of the final intact entry and the number of parseable entries, so
    a reopened log continues the same chain *and* the anchor's count stays in
    step. A torn final line (a crash mid-append) is skipped so the next record
    chains off the last *parseable* entry rather than silently restarting the
    chain from genesis, and is not counted (it isn't a committed entry)."""
    if not path.exists():
        return _GENESIS, 0
    last = _GENESIS
    count = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn/corrupt line: not a committed entry
        count += 1
        last = entry.get("hash", last)
    return last, count


def _anchor_path(path: Path) -> Path:
    return path.with_name(path.name + _ANCHOR_SUFFIX)


def _write_anchor(path: Path, head: str, count: int) -> None:
    """Persist the trust root for `path`: the current head hash and entry count,
    written atomically so a crash never leaves a torn anchor (which would read as
    tampering). Best-effort — an anchor the OS refuses to write must not break
    auditing itself, so a write error is swallowed (verify falls back to the
    chain-only verdict)."""
    anchor = _anchor_path(path)
    tmp = anchor.with_name(anchor.name + ".tmp")
    try:
        tmp.write_text(json.dumps({"count": count, "head": head}))
        os.replace(tmp, anchor)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _read_anchor(path: Path) -> dict | None:
    """The anchor for `path`, or None when absent/unreadable. A missing anchor is
    treated as "no trust root" (legacy logs predate it, and an anchor an attacker
    deletes can't be told apart from one that never existed) — the anchor raises
    the bar on naive tampering, it is not a keyed guarantee."""
    anchor = _anchor_path(path)
    if not anchor.exists():
        return None
    try:
        data = json.loads(anchor.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def verify_chain(path: str | Path) -> tuple[bool, int]:
    """Verify a log's hash chain. Returns ``(ok, bad_line)`` — ``bad_line`` is the
    0-based index of the first tampered/broken entry, or -1 when intact. An
    unparseable line — including a torn final line from a crash mid-append — is
    itself a broken chain: it returns ``(False, index)`` rather than raising.

    After the in-file chain checks out, the anchor sidecar (``<path>.anchor``, if
    present) is cross-checked: its recorded entry count and head hash must match
    what the file actually contains. This catches **tail truncation** (deleting
    the last N entries leaves a shorter-but-valid chain the count no longer
    matches) and a **naive full rewrite** (a forged chain won't match the
    anchor's head), which the chain alone cannot detect. A missing anchor (legacy
    logs, or one an attacker deleted) falls back to the chain-only verdict."""
    path = Path(path)
    prev = _GENESIS
    index = -1
    count = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        index += 1
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return False, index
        recorded_hash = entry.pop("hash", None)
        recorded_prev = entry.pop("prev", None)
        payload = json.dumps(entry, default=str, sort_keys=True)
        expected = hashlib.sha256(
            ((recorded_prev or "") + payload).encode()
        ).hexdigest()
        if recorded_prev != prev or recorded_hash != expected:
            return False, index
        prev = recorded_hash
        count += 1
    anchor = _read_anchor(path)
    if anchor is not None:
        exp_count = anchor.get("count")
        exp_head = anchor.get("head")
        if exp_count != count:
            # Truncated (count < exp_count) or appended past the anchor: report
            # the first index that diverges from the anchored length.
            return False, min(count, exp_count) if isinstance(exp_count, int) else count
        if exp_head != prev:
            return False, max(count - 1, 0)  # length matches but the head was forged
    return True, -1
