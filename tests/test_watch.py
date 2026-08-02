"""The live viewer: Tail follows the right trace (and switches to a newer
session under a container dir), and the SSE server streams appended entries
in real time."""

import json
import threading

import httpx
import pytest

from pyharness.obs.watch import PAGE, Tail, WatchServer, _pick_trace, start_in_thread


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
    _write(
        tmp_path / "run-1-spawn-01" / "trace.jsonl", {"kind": "task", "text": "child"}
    )
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


def test_serves_media_files(server):
    srv, tmp_path = server
    media = tmp_path / "run-1" / "media"
    media.mkdir(parents=True)
    (media / "turn001-0.png").write_bytes(b"\x89PNGdata")

    resp = httpx.get(srv.url + "/media/run-1/turn001-0.png")
    assert resp.status_code == 200
    assert resp.content == b"\x89PNGdata"
    assert resp.headers["content-type"] == "image/png"


def test_media_route_rejects_path_traversal(server):
    srv, tmp_path = server
    (tmp_path / "secret.txt").write_text("nope")
    # Escaping the container (../secret.txt) must 404, not read the file.
    assert httpx.get(srv.url + "/media/..%2f/secret.txt").status_code == 404
    assert httpx.get(srv.url + "/media/run-1/missing.png").status_code == 404


def test_sessions_route_lists_roots_newest_first(server):
    """The switcher's list. Spawn children render inside their parent's lane, so
    listing them as siblings would offer a second way into the same events."""
    srv, tmp_path = server
    for name in ("run-a", "run-a-spawn-01", "run-b"):
        _write(
            tmp_path / name / "trace.jsonl",
            {"ts": 1.0, "kind": "task", "text": "t-" + name},
            {"ts": 2.0, "kind": "code", "text": "print(1)"},
            {"ts": 3.0, "kind": "answer", "text": "done"},
        )
    listed = httpx.get(srv.url + "/sessions").json()

    assert [s["name"] for s in listed] == ["run-b", "run-a"]
    assert listed[0]["outcome"] == "answered" and listed[0]["steps"] == 1
    # Summary fields only. `session_digest` also carries absolute session/trace/
    # audit/workspace paths, and this response is rendered into the page.
    assert set(listed[0]) == {"name", "outcome", "steps", "cost_usd", "denials", "task"}
    assert str(tmp_path) not in httpx.get(srv.url + "/sessions").text


def test_events_can_be_pinned_to_one_session(server):
    """`?session=` is how the sidebar opens an older run while a newer one is
    still writing — without it, the stream always jumps to the newest root."""
    srv, tmp_path = server
    _write(tmp_path / "old" / "trace.jsonl", {"kind": "task", "text": "the old one"})
    _write(tmp_path / "new" / "trace.jsonl", {"kind": "task", "text": "the new one"})

    got = []
    with httpx.stream("GET", srv.url + "/events?session=old", timeout=10) as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                got.append(json.loads(line[6:]))
            if len(got) == 2:
                break
    assert [e.get("text") for e in got] == [None, "the old one"]


def test_events_rejects_a_session_outside_the_container(server):
    """The name comes straight off a query string; it gets the media route's
    rules for the same reason."""
    srv, tmp_path = server
    _write(tmp_path / "run" / "trace.jsonl", {"kind": "task"})
    assert httpx.get(srv.url + "/events?session=nope", timeout=5).status_code == 404
    assert httpx.get(srv.url + "/events?session=..%2f..", timeout=5).status_code == 404


def _handle_source(page: str) -> str:
    """The body of the page's `handle(e)` event dispatcher, brace-matched out of
    the inline script. Fails loudly if the function moves or is renamed — better
    than a check that silently starts matching nothing."""
    marker = "function handle(e) {"
    start = page.index(marker)
    depth = 0
    for i in range(start + len(marker) - 1, len(page)):
        if page[i] == "{":
            depth += 1
        elif page[i] == "}":
            depth -= 1
            if depth == 0:
                return page[start : i + 1]
    raise AssertionError("handle() in the viewer page has unbalanced braces")


def _chain_else_clauses(source: str) -> list[str]:
    """Every `else` belonging to `handle()`'s own top-level if/else-if chain
    (brace depth 1 inside the function body), as either 'else if' or 'else'.
    Depth-aware so an `else` nested inside a branch is never mistaken for the
    end of the chain."""
    body = source[source.index("{") :]
    out, depth, i = [], 0, 0
    while i < len(body):
        ch = body[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif (
            depth == 1
            and body.startswith("else", i)
            and not (body[i - 1].isalnum() or body[i - 1] == "_")
            and not body[i + 4].isalnum()
        ):
            out.append("else if" if body[i + 4 :].lstrip().startswith("if") else "else")
            i += 4
            continue
        i += 1
    return out


def test_viewer_renders_unrecognized_event_kinds(tmp_path):
    """A trace kind the viewer does not know must render plainly, not vanish.

    `handle()` is one long if/else-if chain over `e.kind`; without a terminal
    bare `else` an unknown kind produces *nothing* on screen, with nothing to
    notice — `grant_revoked` already lands there, and so does every kind added
    later. The static published viewer inherits this page, where a silently
    dropped event is worse still. This asserts the chain ends in a real
    fallback that renders the kind and appends it to the lane."""
    source = _handle_source(PAGE)
    clauses = _chain_else_clauses(source)
    assert clauses, "handle() has no if/else chain — the page changed shape"
    assert clauses[-1] == "else", (
        "handle()'s kind dispatch does not end in a bare `else` — an "
        "unrecognized trace kind would render as nothing at all. Keep the "
        "fallback branch last."
    )

    fallback = source[source.rindex("} else {") :]
    assert "laneAdd(" in fallback, "the fallback branch renders nothing"
    assert "innerHTML" not in fallback, "trace text is untrusted; use textContent"


def test_the_page_never_builds_dom_from_a_string():
    """Everything this page renders is untrusted — model prose, tool output, a
    page the agent fetched, an eval board. Markdown made that a live question:
    the renderer turns input into headings, tables and links, and the one rule
    that keeps it safe is that structure comes from `createElement` and text
    from `textContent`. A single `innerHTML` (or `insertAdjacentHTML`, or a
    `document.write`) anywhere in the shared assets breaks it."""
    from pyharness.obs.page import asset

    def code_only(source: str) -> str:
        """Comment lines dropped — both files *discuss* this rule in prose."""
        return "\n".join(
            line
            for line in source.splitlines()
            if not line.lstrip().startswith(("*", "//", "/*"))
        )

    for name in ("viewer.js", "markdown.js"):
        source = code_only(asset(name))
        for banned in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
        ):
            assert banned not in source, f"{name} builds DOM from a string ({banned})"
    # The only attribute ever set from input is href, and only behind a scheme
    # allowlist — `javascript:` in a link is the whole attack.
    assert "SAFE_SCHEME" in asset("markdown.js")


def test_start_in_thread_falls_back_to_an_ephemeral_port(tmp_path):
    url1 = start_in_thread(tmp_path, port=0)
    assert url1 is not None
    taken = int(url1.rsplit(":", 1)[1])
    url2 = start_in_thread(tmp_path, port=taken)  # busy → ephemeral fallback
    assert url2 is not None and url2 != url1
    assert httpx.get(url2 + "/").status_code == 200
