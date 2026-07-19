import json

from pyharness.audit import AuditLog, verify_chain


def test_chain_intact(tmp_path):
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p)
    a.record(action="files.read", ok=True, args="'x'")
    a.record(action="shell.bash", decision="deny", ok=False)
    ok, bad = verify_chain(p)
    assert ok and bad == -1


def test_chain_continues_across_reopen(tmp_path):
    p = tmp_path / "audit.jsonl"
    AuditLog(p).record(action="a", ok=True)
    AuditLog(p).record(action="b", ok=True)  # fresh handle keeps the same chain
    ok, _ = verify_chain(p)
    assert ok


def test_detects_edited_entry(tmp_path):
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p)
    a.record(action="files.read", ok=True)
    a.record(action="shell.bash", ok=True)

    lines = p.read_text().splitlines()
    entry = json.loads(lines[0])
    entry["ok"] = False  # flip a recorded outcome, keep its hash
    lines[0] = json.dumps(entry)
    p.write_text("\n".join(lines) + "\n")

    ok, bad = verify_chain(p)
    assert not ok and bad == 0


def test_detects_deleted_entry(tmp_path):
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p)
    a.record(action="a", ok=True)
    a.record(action="b", ok=True)
    a.record(action="c", ok=True)

    lines = p.read_text().splitlines()
    del lines[1]  # drop the middle entry
    p.write_text("\n".join(lines) + "\n")

    ok, bad = verify_chain(p)
    assert not ok and bad == 1


def test_anchor_detects_tail_truncation(tmp_path):
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p)
    a.record(action="a", ok=True)
    a.record(action="b", ok=True)
    a.record(action="c", ok=True)

    # Drop the last entry: the remaining a->b chain is internally valid, so the
    # hash chain alone can't tell. The anchor (count=3, head=hash(c)) can.
    lines = p.read_text().splitlines()
    del lines[-1]
    p.write_text("\n".join(lines) + "\n")

    ok, bad = verify_chain(p)
    assert not ok and bad == 2  # the missing entry's index


def test_anchor_detects_full_rewrite(tmp_path):
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p)
    a.record(action="a", ok=True)
    a.record(action="b", ok=True)

    # Forge a fresh, internally-consistent chain of the same length but different
    # content. The chain verifies against itself; the anchor's head does not.
    forged = tmp_path / "forged.jsonl"
    f = AuditLog(forged)
    f.record(action="forged-a", ok=True)
    f.record(action="forged-b", ok=True)
    p.write_text(forged.read_text())  # overwrite the log, leave the real anchor

    ok, bad = verify_chain(p)
    assert not ok and bad == 1  # same length, so the head (last entry) is flagged


def test_intact_log_passes_with_anchor(tmp_path):
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p)
    a.record(action="a", ok=True)
    a.record(action="b", ok=True)
    # The anchor written alongside must agree with the untouched log.
    assert (p.with_name(p.name + ".anchor")).exists()
    ok, bad = verify_chain(p)
    assert ok and bad == -1


def test_torn_final_line_is_a_broken_chain_not_a_crash(tmp_path):
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p)
    a.record(action="a", ok=True)
    a.record(action="b", ok=True)

    # Simulate a crash mid-append: a partial final line with no newline.
    with p.open("a") as f:
        f.write('{"ts": 1, "action": "c", "ok": tr')

    ok, bad = verify_chain(p)  # must not raise
    assert not ok and bad == 2  # the torn line is the first broken entry


def test_corrupt_middle_line_is_a_broken_chain(tmp_path):
    p = tmp_path / "audit.jsonl"
    a = AuditLog(p)
    a.record(action="a", ok=True)
    a.record(action="b", ok=True)

    lines = p.read_text().splitlines()
    lines[0] = "}{ not json"
    p.write_text("\n".join(lines) + "\n")

    ok, bad = verify_chain(p)
    assert not ok and bad == 0


def test_reopen_continues_cleanly_past_a_torn_tail(tmp_path):
    p = tmp_path / "audit.jsonl"
    AuditLog(p).record(action="a", ok=True)
    with p.open("a") as f:
        f.write('{"ts": 1, "action": "torn')  # partial line from a crash

    # A fresh handle must resume from the last *intact* entry, not the torn one:
    # record "b" chains off "a" (skipping the torn line), so once the torn line
    # is dropped the a->b chain is sound.
    AuditLog(p).record(action="b", ok=True)
    lines = [line for line in p.read_text().splitlines() if line.strip()]
    del lines[1]  # remove the torn line (which merged with "b"'s append)
    p.write_text("\n".join(lines) + "\n")
    # Surgically rebuilding the log offline invalidates its anchor sidecar (the
    # count no longer matches); drop it so verify falls back to the chain-only
    # verdict, which is what this torn-tail recovery test is about.
    p.with_name(p.name + ".anchor").unlink(missing_ok=True)
    ok, bad = verify_chain(p)
    assert ok and bad == -1
