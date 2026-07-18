"""The live viewer: Tail follows the right trace (and switches to a newer
session under a container dir), and the SSE server streams appended entries
in real time."""

import json
import threading

import httpx
import pytest

from pyharness.obs.watch import Tail, WatchServer, _pick_trace, start_in_thread


def _write(path, *entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_pick_trace_prefers_a_direct_session_dir(tmp_path):
    _write(tmp_path / "trace.jsonl", {"kind": "task"})
    assert _pick_trace(tmp_path) == tmp_path / "trace.jsonl"


def test_pick_trace_follows_newest_session_in_a_container(tmp_path):
    old = tmp_path / "cli-old" / "trace.jsonl"
    new = tmp_path / "cli-new" / "trace.jsonl"
    _write(old, {"kind": "task"})
    _write(new, {"kind": "task"})
    import os

    os.utime(old, (1, 1))
    assert _pick_trace(tmp_path) == new
    assert _pick_trace(tmp_path / "nothing-here") is None


def test_tail_streams_appended_entries_and_skips_partial_lines(tmp_path):
    trace = tmp_path / "trace.jsonl"
    _write(trace, {"kind": "task", "text": "t1"})
    tail = Tail(tmp_path)

    first = tail.poll()
    assert [e["kind"] for e in first] == ["watch_session", "task"]

    assert tail.poll() == []  # nothing new

    with trace.open("a") as f:
        f.write(json.dumps({"kind": "output", "text": "hi"}) + "\n")
        f.write('{"kind": "answ')  # incomplete line — must not be consumed
    assert [e["kind"] for e in tail.poll()] == ["output"]

    with trace.open("a") as f:
        f.write('er", "text": "done"}\n')
    assert [e["kind"] for e in tail.poll()] == ["answer"]


def test_tail_switches_to_a_newer_session(tmp_path):
    _write(tmp_path / "a" / "trace.jsonl", {"kind": "task", "text": "old"})
    tail = Tail(tmp_path)
    tail.poll()

    newer = tmp_path / "b" / "trace.jsonl"
    _write(newer, {"kind": "task", "text": "new"})
    import os

    os.utime(tmp_path / "a" / "trace.jsonl", (1, 1))

    events = tail.poll()
    assert events[0] == {"kind": "watch_session", "session": "b"}
    assert events[1]["text"] == "new"


def test_pick_root_ignores_spawn_children(tmp_path):
    import os

    _write(tmp_path / "run-1" / "trace.jsonl", {"kind": "task", "text": "parent"})
    _write(tmp_path / "run-1-spawn-01" / "trace.jsonl", {"kind": "task", "text": "child"})
    # The child is the most recently modified, but the root view must stay on the parent.
    os.utime(tmp_path / "run-1" / "trace.jsonl", (1, 1))
    assert _pick_trace(tmp_path) == tmp_path / "run-1" / "trace.jsonl"


def test_tail_follows_spawn_children_and_tags_events(tmp_path):
    import os

    parent = tmp_path / "run-1" / "trace.jsonl"
    _write(parent, {"kind": "task", "text": "parent"})
    tail = Tail(tmp_path)
    first = tail.poll()
    assert first[0] == {"kind": "watch_session", "session": "run-1"}
    assert first[1]["session"] == "run-1"

    # A sub-agent starts: newest file, but must NOT flip the root view.
    child = tmp_path / "run-1-spawn-01" / "trace.jsonl"
    _write(parent, {"kind": "spawn", "text": "sub task", "child": "run-1-spawn-01"})
    _write(child, {"kind": "task", "text": "sub task"})
    os.utime(parent, (1, 1))

    events = tail.poll()
    assert all(e["kind"] != "watch_session" for e in events)  # no clobber
    kinds = {(e["kind"], e["session"]) for e in events}
    assert ("spawn", "run-1") in kinds
    assert ("task", "run-1-spawn-01") in kinds


@pytest.fixture
def server(tmp_path):
    srv = WatchServer(tmp_path, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, tmp_path
    srv.shutdown()


def test_serves_the_page(server):
    srv, _ = server
    resp = httpx.get(srv.url + "/")
    assert resp.status_code == 200
    assert "pyharness — live" in resp.text
    assert httpx.get(srv.url + "/nope").status_code == 404


def test_sse_streams_live_appends(server):
    srv, tmp_path = server
    _write(tmp_path / "trace.jsonl", {"kind": "task", "text": "do it"})

    got = []
    with httpx.stream("GET", srv.url + "/events", timeout=10) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = resp.iter_lines()
        for line in lines:
            if line.startswith("data: "):
                got.append(json.loads(line[6:]))
            if len(got) == 2:
                # The replay arrived; now append live and expect it streamed.
                _write(tmp_path / "trace.jsonl", {"kind": "answer", "text": "done"})
            if len(got) == 3:
                break
    assert [e["kind"] for e in got] == ["watch_session", "task", "answer"]
    assert got[2]["text"] == "done"


def test_start_in_thread_falls_back_to_an_ephemeral_port(tmp_path):
    url1 = start_in_thread(tmp_path, port=0)
    assert url1 is not None
    taken = int(url1.rsplit(":", 1)[1])
    url2 = start_in_thread(tmp_path, port=taken)  # busy → ephemeral fallback
    assert url2 is not None and url2 != url1
    assert httpx.get(url2 + "/").status_code == 200
