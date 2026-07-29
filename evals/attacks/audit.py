"""Attacks on the record of what happened.

The audit log is the project's load-bearing claim: whatever else an agent gets
up to, there is a tamper-evident account of every effect it had. These attacks
are the ones an attacker who has already reached the log file would try — edit
one entry, delete the incriminating tail, or rewrite the whole thing into a
consistent story.

The gap is the one every local-only integrity scheme has: the trust root lives
next to the thing it is protecting.
"""

from __future__ import annotations

import hashlib
import json

from ..scoreboard import Attack, Verdict
from ..support import judge, must, scratch


def _log_with_history(root, entries: int = 6):
    """A real audit log with a plausible session's worth of records in it."""
    from pyharness.audit import AuditLog

    log = AuditLog(root / "audit.jsonl")
    for index in range(entries):
        log.record(action="http.request", phase="start", args=f"GET /page/{index}")
        log.record(
            action="http.request", phase="end", ok=True, args=f"GET /page/{index}"
        )
    return log


def _verify(path) -> tuple[bool, int]:
    from pyharness.audit import verify_chain

    return verify_chain(path)


def _intact_first(path) -> None:
    ok, bad = _verify(path)
    must(ok, f"the log did not verify before tampering (broken at {bad})")


def _fresh_log_verifies() -> None:
    """Control for every tampering attack: an untouched log verifies. Otherwise
    'verification failed' would be the answer to every question."""
    with scratch("audit-ctl") as root:
        _log_with_history(root)
        ok, bad = _verify(root / "audit.jsonl")
        must(ok, f"an untouched audit log does not verify (broken at {bad})")


# ---------------------------------------------------------------------------


def _edit_one_entry() -> Verdict:
    """Change what one recorded action says it did."""
    with scratch("audit-edit") as root:
        _log_with_history(root)
        path = root / "audit.jsonl"
        _intact_first(path)
        lines = path.read_text().splitlines()
        target = 5
        entry = json.loads(lines[target])
        entry["args"] = "GET /an-innocent-page"
        lines[target] = json.dumps(entry)
        path.write_text("\n".join(lines) + "\n")
        ok, _ = _verify(path)
        return judge(
            attacker_won=ok,
            # The record really says something different now: the doctored text
            # is present and one of the two mentions of the real page is gone.
            ran="GET /an-innocent-page" in path.read_text()
            and path.read_text().count("GET /page/2") == 1,
            ran_evidence="the edit did not land in the file at all",
        )


def _delete_the_tail() -> Verdict:
    """Drop the last few entries — the ones recording the damage — leaving a
    shorter chain that is still internally consistent."""
    with scratch("audit-truncate") as root:
        _log_with_history(root)
        path = root / "audit.jsonl"
        _intact_first(path)
        lines = path.read_text().splitlines()
        kept = lines[:-4]
        path.write_text("\n".join(kept) + "\n")
        ok, _ = _verify(path)
        return judge(
            attacker_won=ok,
            ran=len(kept) < len(lines) and len(kept) > 0,
            ran_evidence="nothing was actually removed from the log",
        )


def _rewrite_the_whole_story() -> Verdict:
    """Replace the log with a different, internally perfect chain — the same
    number of entries, correctly linked from genesis, telling a harmless story.
    Only something outside the file can notice."""
    with scratch("audit-forge") as root:
        _log_with_history(root)
        path = root / "audit.jsonl"
        _intact_first(path)
        count = len(path.read_text().splitlines())

        forged: list[str] = []
        prev = ""
        for index in range(count):
            entry = {"ts": 0.0, "action": "files.read", "args": f"notes-{index}.md"}
            payload = json.dumps(entry, default=str, sort_keys=True)
            entry["prev"] = prev
            entry["hash"] = hashlib.sha256((prev + payload).encode()).hexdigest()
            prev = entry["hash"]
            forged.append(json.dumps(entry, default=str))
        path.write_text("\n".join(forged) + "\n")

        ok, _ = _verify(path)
        # An independent check that the forgery is competent: re-link it as the
        # verifier does and confirm every internal link holds. If the forgery
        # were malformed, "detected" would mean nothing.
        prev = ""
        internally_valid = True
        for line in forged:
            entry = json.loads(line)
            recorded_hash = entry.pop("hash")
            recorded_prev = entry.pop("prev")
            payload = json.dumps(entry, default=str, sort_keys=True)
            expected = hashlib.sha256((recorded_prev + payload).encode()).hexdigest()
            internally_valid &= recorded_prev == prev and recorded_hash == expected
            prev = recorded_hash
        return judge(
            attacker_won=ok,
            ran=internally_valid,
            ran_evidence=(
                "the forged chain does not even link to itself, so catching it "
                "shows nothing about detecting a competent rewrite"
            ),
        )


def _delete_the_tail_and_the_anchor() -> Verdict:
    """The same truncation, but also remove the sidecar that records how long
    the log should be."""
    with scratch("audit-anchor") as root:
        _log_with_history(root)
        path = root / "audit.jsonl"
        anchor = path.with_name(path.name + ".anchor")
        _intact_first(path)
        must(anchor.exists(), "no anchor sidecar was written; nothing to remove")
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines[:-4]) + "\n")
        anchor.unlink()
        ok, _ = _verify(path)
        return judge(
            attacker_won=ok,
            ran=not anchor.exists(),
            ran_evidence="the anchor is still there; this is the truncation attack",
        )


ATTACKS = [
    Attack(
        id="audit-edit-one-entry",
        surface="audit",
        description="change what one recorded action says it did",
        property=(
            "Altering the account of what the agent did is detectable by anyone "
            "holding the file, without needing to have watched it being written."
        ),
        run=_edit_one_entry,
        control=_fresh_log_verifies,
    ),
    Attack(
        id="audit-delete-the-tail",
        surface="audit",
        description="delete the last few entries and leave a valid shorter chain",
        property=(
            "Removing the end of the record — where the damage would be — is "
            "detectable, not just editing the middle."
        ),
        run=_delete_the_tail,
        control=_fresh_log_verifies,
    ),
    Attack(
        id="audit-forge-the-whole-log",
        surface="audit",
        description="replace the log with a flawless chain telling a harmless story",
        property=(
            "Replacing the entire record with an internally consistent fiction is "
            "detectable; consistency is not the same as authenticity."
        ),
        run=_rewrite_the_whole_story,
        control=_fresh_log_verifies,
    ),
    Attack(
        id="audit-remove-the-anchor",
        surface="audit",
        description="truncate the log and delete its length/head sidecar too",
        property=(
            "Deleting the end of the record stays detectable however much of the "
            "surrounding session state the attacker can also reach."
        ),
        run=_delete_the_tail_and_the_anchor,
        control=_fresh_log_verifies,
        known_gap=(
            "The anchor is a plain sidecar, not a keyed MAC, and a missing anchor "
            "is treated as 'no trust root' rather than as evidence — because a "
            "log that never had one (any log predating the anchor) is "
            "indistinguishable from one whose anchor was removed. A hash chain "
            "proves internal consistency; detecting deletion of its *end* needs a "
            "root the attacker cannot reach, which means either a key store this "
            "harness does not have or shipping the head off-box, and inventing a "
            "local one would only look like a guarantee. The stated position: the "
            "chain is tamper-*evident* against anyone without write access to the "
            "session directory, and that is what it claims. Agent code is such an "
            "attacker by construction — the OS sandbox confines its writes to the "
            "workspace subdirectory, and both audit.jsonl and its anchor live one "
            "level above it — so reaching this gap means already being outside "
            "the box."
        ),
    ),
]
