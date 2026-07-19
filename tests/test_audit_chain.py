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
    del lines[1]  # remove the torn line
    p.write_text("\n".join(lines) + "\n")
    ok, bad = verify_chain(p)
    assert ok and bad == -1
