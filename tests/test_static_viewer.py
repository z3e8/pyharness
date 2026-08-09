"""The static export: the same page as the live viewer, fed a baked array.

The point of the split is that there is only one renderer. These tests hold
that line — the export must not become a second implementation — and cover the
three things a published copy has to do that a live one does not: inline its
media, redact absolute paths, and stop claiming to be live.
"""

import base64
import getpass
import json
import pathlib
import re

import pytest

from pyharness.obs.static import (
    build_index,
    build_page,
    build_site,
    discover_sessions,
    session_events,
)
from pyharness.obs.watch import LIVE_FEED, PAGE, render_page


def _write(path, *entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _session(root, name="sess"):
    """A minimal but complete session dir: a task, a code cell, an answer."""
    d = root / name
    _write(
        d / "trace.jsonl",
        {"ts": 1000.0, "kind": "session_start", "text": "", "root": str(d)},
        {"ts": 1000.5, "kind": "task", "text": "do the thing"},
        {"ts": 1001.0, "kind": "code", "text": "print('hi')"},
        {"ts": 1002.0, "kind": "output", "text": "hi"},
        {"ts": 1030.0, "kind": "answer", "text": "done"},
        {
            "ts": 1030.1,
            "kind": "session_end",
            "text": "",
            "spent_usd": 0.02,
            "calls": 2,
        },
    )
    return d


def _baked_events(html):
    """The event array the page will replay, decoded the way the browser does."""
    m = re.search(r'const EVENTS = JSON\.parse\((".*?")\);\n', html, re.S)
    assert m, "the static page carries no baked event array"
    return json.loads(json.loads(m.group(1)))


def test_static_page_is_the_live_page_with_a_different_feed(tmp_path):
    """One renderer, two feeds. If the export ever grows its own copy of
    `handle()`, the published page starts drifting from the live one and
    nothing catches it — the published page is the one nobody re-runs."""
    html = build_page(_session(tmp_path))

    # Everything from the first line of the page down to the feed is shared
    # verbatim: the CSS, the DOM, and every render function including handle().
    shared = PAGE[: PAGE.index(LIVE_FEED)]
    assert html.startswith(shared[: shared.index("<title>")])
    assert shared[shared.index("function handle(") :] in html

    assert "EventSource" not in html  # no live feed in a file:// page
    assert "EventSource" in PAGE


def test_render_page_refuses_a_template_with_no_slot(monkeypatch):
    """A missing slot must raise, not silently produce a page with no feed —
    that renders an empty log, which is indistinguishable from a session that
    did nothing."""
    monkeypatch.setattr("pyharness.obs.watch._PAGE_TEMPLATE", "<html>no slots</html>")
    with pytest.raises(RuntimeError, match="slot"):
        render_page("handle({});")


def test_baked_events_survive_script_escaping(tmp_path):
    """Trace text is arbitrary: a `</script>` in a code cell would end the
    block early and break the page, and a lone `<` is enough to worry about."""
    d = tmp_path / "sess"
    _write(
        d / "trace.jsonl",
        {"ts": 1.0, "kind": "task", "text": "x"},
        {"ts": 2.0, "kind": "code", "text": "html = '</script><b>1 < 2</b>'"},
    )
    html = build_page(d)
    events = _baked_events(html)
    assert events[-1]["text"] == "html = '</script><b>1 < 2</b>'"
    # The literal sequence must not appear before the page's own closing tag.
    script = html[html.index("const EVENTS") :]
    assert "</script>" not in script[: script.index("\n</script>")]


def test_media_is_inlined_as_a_data_uri(tmp_path):
    d = tmp_path / "sess"
    (d / "media").mkdir(parents=True)
    (d / "media" / "shot.jpg").write_bytes(b"\xff\xd8jpegbytes")
    _write(
        d / "trace.jsonl",
        {"ts": 1.0, "kind": "task", "text": "x"},
        {"ts": 2.0, "kind": "media", "src": "/media/sess/shot.jpg", "text": "a look"},
    )
    (event,) = [e for e in session_events(d) if e["kind"] == "media"]
    head, _, payload = event["src"].partition(",")
    assert head == "data:image/jpeg;base64"
    assert base64.b64decode(payload) == b"\xff\xd8jpegbytes"
    assert "/media/" not in build_page(d)


def test_a_missing_media_file_does_not_lose_the_page(tmp_path):
    """One unreadable screenshot must cost the screenshot, not the session."""
    d = tmp_path / "sess"
    _write(
        d / "trace.jsonl",
        {"ts": 1.0, "kind": "task", "text": "x"},
        {"ts": 2.0, "kind": "media", "src": "/media/sess/gone.jpg", "text": "a look"},
    )
    (event,) = [e for e in session_events(d) if e["kind"] == "media"]
    assert event["src"] == ""
    # The page still builds, still carries the rest of the session, and still
    # shows that an image was there (the media event keeps its alt text).
    events = _baked_events(build_page(d))
    assert [e["kind"] for e in events] == ["watch_session", "task", "media"]
    assert events[-1]["text"] == "a look"


def test_media_src_cannot_escape_the_session_container(tmp_path):
    d = tmp_path / "sess"
    (tmp_path / "secret.txt").write_bytes(b"private")
    _write(
        d / "trace.jsonl",
        {"ts": 1.0, "kind": "media", "src": "/media/../secret.txt", "text": ""},
        {"ts": 2.0, "kind": "media", "src": "/media/sess/../../secret.txt", "text": ""},
    )
    assert all(e["src"] == "" for e in session_events(d) if e["kind"] == "media")


def test_absolute_paths_are_redacted(tmp_path, monkeypatch):
    """A published page must not carry the operator's home directory. The trace
    records the session root, and the model's preamble names the workspace."""
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    d = _session(tmp_path)
    _write(
        d / "trace.jsonl",
        {"ts": 1003.0, "kind": "output", "text": f"wrote {d}/workspace/out.txt"},
    )
    html = build_page(d)
    assert str(d) not in html
    assert str(tmp_path) not in html
    texts = [e.get("text", "") for e in _baked_events(html)]
    assert any("<session>/workspace/out.txt" in t for t in texts)


def test_the_private_temp_root_is_redacted(tmp_path, monkeypatch):
    """The regression that motivated this: macOS puts each user's temp under
    `/var/folders/<xx>/<id>/T`, which is under neither `$HOME` nor the session
    root, so the two prefixes missed it. It reached a published page through a
    pip notice naming the sandbox venv's interpreter, and re-appeared on every
    re-bake because the pages are built from gitignored `.sessions/`."""
    private_tmp = tmp_path / "var" / "folders" / "rq" / "n0tar3alid3ntifier" / "T"
    private_tmp.mkdir(parents=True)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(private_tmp))
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))
    d = _session(tmp_path)
    _write(
        d / "trace.jsonl",
        {
            "ts": 1003.0,
            "kind": "output",
            "text": f"run: {private_tmp}/pyharness-sb-ab12/venv/bin/python3.12",
        },
    )
    html = build_page(d)
    assert str(private_tmp) not in html
    assert "n0tar3alid3ntifier" not in html
    texts = [e.get("text", "") for e in _baked_events(html)]
    # The shape survives: a reader still sees a temp path was involved.
    assert any("<tmp>/pyharness-sb-ab12/venv" in t for t in texts)


def test_a_foreign_temp_id_is_redacted_by_shape(tmp_path, monkeypatch):
    """A session recorded under a `TMPDIR` this process no longer has — a bake on
    another machine, or after the root rotated — carries an id the prefix map
    never saw. Matching the shape is what makes the scrub survive that."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: "/tmp")
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))
    d = _session(tmp_path)
    _write(
        d / "trace.jsonl",
        {
            "ts": 1003.0,
            "kind": "output",
            "text": "at /private/var/folders/aa/s0me0therh0st/T/x.log",
        },
    )
    html = build_page(d)
    assert "s0me0therh0st" not in html
    texts = [e.get("text", "") for e in _baked_events(html)]
    assert any("<tmp>/x.log" in t for t in texts)


def test_a_shared_temp_root_is_left_alone(tmp_path, monkeypatch):
    """`/tmp` is the same path for every user on the host, so redacting it would
    add noise and remove no information. Only the per-user roots are secrets."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: "/tmp")
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))
    d = _session(tmp_path)
    _write(
        d / "trace.jsonl",
        {"ts": 1003.0, "kind": "output", "text": "wrote /tmp/out.txt"},
    )
    texts = [e.get("text", "") for e in _baked_events(build_page(d))]
    assert any("/tmp/out.txt" in t for t in texts)


def test_the_bare_login_name_is_redacted(tmp_path, monkeypatch):
    """`ls -la` prints an owner column, which is a username in no path at all —
    the second leak found the same day as the temp root, and the reason the
    scrub cannot be only about paths."""
    monkeypatch.setattr(
        "pathlib.Path.home", classmethod(lambda cls: tmp_path / "chandlerbing")
    )
    d = _session(tmp_path)
    _write(
        d / "trace.jsonl",
        {
            "ts": 1003.0,
            "kind": "output",
            "text": "drwxr-xr-x@ 32 chandlerbing  staff  1024 Aug  4 22:45 .",
        },
    )
    html = build_page(d)
    assert "chandlerbing" not in html
    texts = [e.get("text", "") for e in _baked_events(html)]
    assert any("<user>  staff" in t for t in texts)


def test_a_short_login_name_is_left_alone(tmp_path, monkeypatch):
    """A three-character login is as likely to be an ordinary word in captured
    output as an identifier. Corrupting a transcript is the worse failure."""
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "cat"))
    d = _session(tmp_path)
    _write(
        d / "trace.jsonl",
        {"ts": 1003.0, "kind": "output", "text": "cat the file, then concatenate"},
    )
    texts = [e.get("text", "") for e in _baked_events(build_page(d))]
    assert any("cat the file, then concatenate" in t for t in texts)


def test_doc_pages_are_scrubbed_like_session_pages(tmp_path, monkeypatch):
    """`--doc` takes arbitrary markdown, and the throughput site points it at a
    control arm's transcript read straight out of gitignored `.sessions/`. That
    path bypassed the scrub entirely until a re-bake put a username back into a
    committed page."""
    monkeypatch.setattr(
        "pathlib.Path.home", classmethod(lambda cls: tmp_path / "chandlerbing")
    )
    doc = tmp_path / "transcript.md"
    doc.write_text("# Arm\n\n```\n-rw-r--r-- 1 chandlerbing staff 12 out.gz\n```\n")
    out = tmp_path / "site"
    build_site([_session(tmp_path)], out, docs=[("Control arm", doc)])
    page = (out / "control-arm.html").read_text()
    assert "chandlerbing" not in page
    # `<` is escaped on the way into the script block, as it is for a session.
    assert r"\u003cuser> staff" in page


# Account names generic enough to appear in ordinary prose ("the test runner",
# "run as root"). Checking them against a page would fail on wording, not on a
# leak, and a gate that cries wolf gets deleted.
_GENERIC_LOGINS = frozenset(
    {"runner", "root", "ubuntu", "user", "admin", "build", "test", "agent", "docker"}
)

_HOST_PATTERNS = {
    "a home directory": re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+"),
    "a private temp root": re.compile(
        r"/var/folders/[A-Za-z0-9_+=-]+/[A-Za-z0-9_+=-]+"
    ),
}


def _committed_pages():
    root = pathlib.Path(__file__).resolve().parents[1]
    return sorted(root.glob("evals/*/site/*.html"))


def test_committed_pages_carry_no_host_identifiers():
    """A gate on the artifacts, not on the code that writes them.

    `obs/static.py` scrubs at the bake, which is the fix. This is the check that
    a leak arriving by some *other* route — a hand-edit, a new `--doc` source, a
    page baked before the scrub existed — does not reach a published page
    unnoticed. It is worth having separately because such a leak is invisible in
    review: a baked page is one enormous JSON line, so `git diff` reports one
    changed line and shows nothing legible.

    Two leaks motivated this, both found by accident on 2026-08-09: a macOS
    temp-dir id restored by a re-bake, and a bare login name in an `ls -la`
    owner column inside a `--doc` transcript."""
    pages = _committed_pages()
    assert pages, "no committed site pages found — has the layout moved?"

    offenders = []
    for page in pages:
        text = page.read_text(encoding="utf-8")
        for what, pattern in _HOST_PATTERNS.items():
            for hit in set(pattern.findall(text)):
                offenders.append(f"{page.name}: {what} — {hit}")
        login = getpass.getuser()
        if login.lower() not in _GENERIC_LOGINS and re.search(
            rf"\b{re.escape(login)}\b", text
        ):
            offenders.append(f"{page.name}: this machine's login — {login}")
    assert not offenders, "committed pages carry host identifiers:\n" + "\n".join(
        sorted(offenders)
    )


def test_the_page_says_it_is_a_record_not_a_live_view(tmp_path):
    """Replayed, every event lands in the same millisecond. A ticking clock
    would count from when the page opened and claim a 30-second session had run
    for hours."""
    html = build_page(_session(tmp_path))
    feed = html[html.index("const EVENTS") :]
    assert "clearInterval(ticker)" in feed
    assert "archived" in feed
    assert "const secs = 30.1" in feed  # the real span, from the trace
    assert "unfinished at end of record" in feed  # nothing spins forever


def test_build_site_writes_a_page_per_session_and_an_index(tmp_path):
    a = _session(tmp_path, "alpha")
    b = _session(tmp_path, "beta")
    out = tmp_path / "site"
    written = build_site([a, b], out)

    assert {p.name for p in written} == {"alpha.html", "beta.html", "index.html"}
    index = (out / "index.html").read_text()
    assert 'href="alpha.html"' in index and 'href="beta.html"' in index
    # The digest also carries absolute session/trace/audit/workspace paths.
    assert str(tmp_path) not in index


def test_index_reports_the_outcome_vocabulary(tmp_path):
    from pyharness.obs.transcript import session_digest

    digests = [session_digest(_session(tmp_path, "alpha"))]
    index = build_index(digests, title="t")
    assert "answered" in index and "alpha" in index


def test_discover_sessions_skips_spawn_children(tmp_path):
    """A sub-agent renders inside its parent's page; a page of its own would
    publish the same events twice."""
    _session(tmp_path, "root-a")
    _session(tmp_path, "root-a-spawn-01")
    _session(tmp_path, "root-b")
    assert [d.name for d in discover_sessions(tmp_path)] == ["root-a", "root-b"]


def test_discover_sessions_accepts_a_single_session_dir(tmp_path):
    d = _session(tmp_path)
    assert discover_sessions(d) == [d]


def test_every_session_page_carries_the_switcher(tmp_path):
    """A baked page has no server to ask for the session list, so the list
    travels with it — otherwise the archive is N pages with no way between
    them but the back button."""
    a = _session(tmp_path, "alpha")
    _session(tmp_path, "beta")
    out = tmp_path / "site"
    build_site([a, tmp_path / "beta"], out)

    page = (out / "alpha.html").read_text()
    m = re.search(r"window\.SESSIONS = JSON\.parse\((\".*?\")\);", page)
    assert m, "the baked page carries no session list"
    listed = json.loads(json.loads(m.group(1)))
    assert [s["name"] for s in listed] == ["alpha", "beta"]
    assert [s["href"] for s in listed] == ["alpha.html", "beta.html"]
    assert 'currentSession = "alpha"' in page
    # Summary fields only — the same rule the index follows.
    assert str(tmp_path) not in page


def test_live_only_controls_are_removed_from_a_record(tmp_path):
    """ "Follow" has no stream to follow and "jump to latest" has no latest."""
    page = build_page(_session(tmp_path))
    feed = page[page.index("const EVENTS") :]
    assert ".rail-label .follow, #jump" in feed


def test_build_site_renders_the_eval_boards_as_pages(tmp_path):
    board = tmp_path / "SCOREBOARD.md"
    board.write_text("# Board\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    out = tmp_path / "site"
    written = build_site(
        [_session(tmp_path, "alpha")], out, docs=[("Adversarial suite", board)]
    )

    assert (out / "adversarial-suite.html") in written
    doc = (out / "adversarial-suite.html").read_text()
    index = (out / "index.html").read_text()
    # The board is linked from the nav on every page in the site, not stranded.
    assert 'href="adversarial-suite.html"' in index
    assert 'href="index.html"' in doc
    # The markdown is data the shared renderer parses, never markup pasted in.
    assert "<h1>Board</h1>" not in doc
    assert "renderMarkdown(" in doc


def test_a_board_is_never_injected_as_markup(tmp_path):
    """A doc page embeds its source as a JSON string for the DOM-building
    renderer. Markdown that contains HTML must stay inert text, and a
    `</script>` in it must not end the block early."""
    board = tmp_path / "b.md"
    source = "# T\n\n<script>alert(1)</script>\n\n<img onerror=x>\n"
    board.write_text(source)
    out = tmp_path / "site"
    build_site([_session(tmp_path, "alpha")], out, docs=[("B", board)])
    doc = (out / "b.html").read_text()

    m = re.search(r"renderMarkdown\((\".*?\")\)", doc, re.S)
    assert m, "the doc page carries no markdown payload"
    # Not one `<` from the source survives as a character the HTML parser sees,
    # so nothing in a board can close the script block or become an element…
    assert "<" not in m.group(1)
    # …and it is still exactly the document, byte for byte, to `JSON.parse`.
    assert json.loads(m.group(1)) == source


def test_a_spawn_child_is_baked_into_its_parents_page(tmp_path):
    parent = _session(tmp_path, "root")
    _write(
        parent / "trace.jsonl",
        {"ts": 1004.0, "kind": "spawn", "text": "", "child": "root-spawn-01"},
    )
    _session(tmp_path, "root-spawn-01")
    sessions = {e.get("session") for e in session_events(parent)}
    assert sessions == {"root", "root-spawn-01"}
