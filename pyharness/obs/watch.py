"""`pyharness-watch` — the live session viewer.

A small local web page that tails `trace.jsonl` and renders the session as it
happens: the current turn, each code cell and its output, in-flight actions
with elapsed time (an `action_start` without its `action_end` *is* the thing
running or stuck), pending approvals, errors, and running spend. The durable
JSONL record is written synchronously by the session, so this view is
real-time by construction — no collector, no container, no refresh.

Stdlib only. The server binds 127.0.0.1 and serves four routes: `/` (the page),
`/events` (Server-Sent Events: each trace entry as one JSON `data:` line, tagged
with the session it came from), `/sessions` (the switcher's list), and `/media/`.
Watch one session (a dir containing `trace.jsonl`) or a container of sessions
(e.g. `.sessions/`). It follows a whole session *tree* — the root session plus
any sub-agents it spawns (`{root}-spawn-*`) — and switches the top-level view
only when a genuinely new *root* session starts, so a running sub-agent never
steals the view.

`/events?session=NAME` pins the stream to one session instead of following the
newest. That is what the sidebar switcher uses: past runs stay one click away
while a new one is going, and the "follow" toggle decides whether a freshly
started session takes the view.

Two entry points: `main()` (the `pyharness-watch` console script) and
`start_in_thread()` (the CLI embeds the viewer in the agent process — fail-open,
a dead port never blocks a session).
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import re
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .page import FEED_SLOT, TITLE_SLOT, session_template

log = logging.getLogger("pyharness.watch")

_POLL_S = 0.25
_KEEPALIVE_POLLS = 20  # one SSE comment every ~5s so dead clients are noticed

# A spawned child's dir is named `{parent}-spawn-NN` (see Session._spawn_child).
# We match the suffix to tell root sessions from children without opening files.
_SPAWN_RE = re.compile(r"-spawn-\d+")


def _pick_root(target: Path) -> Path | None:
    """The *root* session dir to follow: `target` itself when it holds a
    trace.jsonl, else the most recently modified top-level session under it —
    excluding spawn children so a running sub-agent never steals the top view."""
    target = Path(target)
    if (target / "trace.jsonl").exists():
        return target
    dirs = [p.parent for p in target.glob("*/trace.jsonl") if p.is_file()]
    if not dirs:
        return None
    roots = [d for d in dirs if not _SPAWN_RE.search(d.name)]
    pool = roots or dirs  # a container of only spawn dirs still shows something
    return max(pool, key=lambda d: (d / "trace.jsonl").stat().st_mtime)


def _pick_trace(target: Path) -> Path | None:
    """The trace file for the root session (kept for callers/tests that want the
    single followed file); the live viewer streams the whole tree via `Tail`."""
    root = _pick_root(target)
    return None if root is None else root / "trace.jsonl"


class _FileTail:
    """Incremental, non-blocking reader over one trace.jsonl. Each `poll()`
    returns the complete entries appended since the last call (partial trailing
    lines are held back until their newline arrives)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.offset = 0

    def poll(self) -> list[dict]:
        try:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return []
        end = chunk.rfind(b"\n")
        if end == -1:
            return []  # no complete new line yet
        out: list[dict] = []
        for raw in chunk[: end + 1].splitlines():
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(entry, dict):
                out.append(entry)
        self.offset += end + 1
        return out


class Tail:
    """Follows a whole session *tree* — the root session plus every spawned
    child (`{root}-spawn-*`) — and interleaves their appended entries. Each
    entry is tagged with the `session` (dir name) it came from so the viewer can
    route it to the right lane. A synthetic `watch_session` event is emitted only
    when the *root* changes (a genuinely new top-level session), never when a
    sub-agent starts — that just opens a new lane under the current root."""

    def __init__(self, target: Path):
        self.target = Path(target)
        self.root: Path | None = None
        self.files: dict[str, _FileTail] = {}  # session dir name -> reader

    def _sessions(self) -> list[Path]:
        """The root followed by its spawn descendants, in a stable order (root
        first, children by name) so a parent's `spawn` row is read before the
        child events it introduces."""
        if self.root is None:
            return []
        out = [self.root]
        for p in sorted(self.root.parent.glob(self.root.name + "-spawn-*")):
            if (p / "trace.jsonl").is_file():
                out.append(p)
        return out

    def poll(self) -> list[dict]:
        events: list[dict] = []
        root = _pick_root(self.target)
        if root != self.root:
            self.root = root
            self.files = {}
            if root is not None:
                events.append({"kind": "watch_session", "session": root.name})
        if self.root is None:
            return events
        for d in self._sessions():
            name = d.name
            reader = self.files.get(name)
            if reader is None:
                reader = self.files[name] = _FileTail(d / "trace.jsonl")
            for entry in reader.poll():
                entry.setdefault("session", name)
                events.append(entry)
        return events


def _resolve_session(container: Path, name: str) -> Path | None:
    """The session dir called `name` under `container`, or None.

    Name-only, resolved, and checked back against the container: `/events` takes
    this straight from a query string, and the media route's traversal rules
    apply here for the same reason."""
    if not name or "/" in name or "\\" in name or name in ("..", "."):
        return None
    if (container / "trace.jsonl").exists():
        container = container.parent
    path = (container / name).resolve()
    if (
        not path.is_relative_to(container.resolve())
        or not (path / "trace.jsonl").is_file()
    ):
        return None
    return path


# Digesting a session means reading its whole trace, and the sidebar polls. Keyed
# on the trace's (size, mtime) so a finished session is read once and a running
# one is re-read only when it actually grew.
_DIGEST_CACHE: dict[tuple[str, int, float], dict] = {}
_LIST_LIMIT = 40  # newest N; a long-lived .sessions/ should not stall the poll


def list_sessions(target: Path) -> list[dict]:
    """The switcher's list: root sessions under `target`, newest first, each
    with the summary fields the sidebar shows. Spawn children are omitted —
    they render inside their parent, not as siblings."""
    target = Path(target)
    if (target / "trace.jsonl").exists():
        dirs = [target]
    else:
        dirs = [
            p.parent
            for p in target.glob("*/trace.jsonl")
            if p.is_file() and not _SPAWN_RE.search(p.parent.name)
        ]
    dirs.sort(key=lambda d: (d / "trace.jsonl").stat().st_mtime, reverse=True)

    from .transcript import session_digest

    out = []
    for d in dirs[:_LIST_LIMIT]:
        stat = (d / "trace.jsonl").stat()
        key = (str(d), stat.st_size, stat.st_mtime)
        digest = _DIGEST_CACHE.get(key)
        if digest is None:
            full = session_digest(d)
            # Summary fields only: the digest also carries absolute paths, and
            # this response is what the page renders.
            digest = {
                "name": full["name"],
                "outcome": full["outcome"],
                "steps": full["steps"],
                "cost_usd": full["cost_usd"],
                "denials": full["denials"],
                "task": (full.get("task") or "")[:200],
            }
            if len(_DIGEST_CACHE) > 200:
                _DIGEST_CACHE.clear()
            _DIGEST_CACHE[key] = digest
        out.append(digest)
    return out


class _Handler(BaseHTTPRequestHandler):
    server: WatchServer

    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        pass  # request logging is noise for a local single-user viewer

    def do_GET(self):  # noqa: N802 — stdlib naming
        route = urlparse(self.path)
        if route.path == "/":
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        elif route.path == "/events":
            wanted = parse_qs(route.query).get("session", [None])[0]
            self._serve_events(wanted)
        elif route.path == "/sessions":
            body = json.dumps(list_sessions(self.server.target)).encode()
            self._send(body, "application/json")
        elif route.path.startswith("/media/"):
            self._serve_media(route.path[len("/media/") :])
        else:
            self.send_error(404)

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_media(self, rel: str) -> None:
        """Serve `<session>/media/<file>` from the followed container. Names are
        validated and the resolved path must stay inside the container — a media
        URL can never read outside `.sessions/`."""
        parts = rel.split("/")
        if len(parts) != 2 or not all(p and p not in ("..", ".") for p in parts):
            self.send_error(404)
            return
        session, name = parts
        container = self.server.target
        if (container / "trace.jsonl").exists():
            container = container.parent  # target is a single session dir
        path = (container / session / "media" / name).resolve()
        if not path.is_relative_to(container.resolve()) or not path.is_file():
            self.send_error(404)
            return
        self._send(
            path.read_bytes(),
            mimetypes.guess_type(name)[0] or "application/octet-stream",
        )

    def _serve_events(self, session: str | None = None):
        """Stream one session tree. With no `session`, follow whichever root is
        newest (the default view); with one, pin to it — a request for a name
        that isn't a session directory under the target is a 404, never a path
        the client got to choose."""
        target = self.server.target
        if session is not None:
            root = _resolve_session(target, session)
            if root is None:
                self.send_error(404)
                return
            target = root
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        tail = Tail(target)
        idle = 0
        try:
            while True:
                events = tail.poll()
                if events:
                    idle = 0
                    for entry in events:
                        payload = json.dumps(entry, default=str)
                        self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    continue
                idle += 1
                if idle % _KEEPALIVE_POLLS == 0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                time.sleep(_POLL_S)
        except (BrokenPipeError, ConnectionResetError):
            return


class WatchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, target: Path, port: int = 6061):
        self.target = Path(target)
        super().__init__(("127.0.0.1", port), _Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


def start_in_thread(target: str | Path, port: int = 6061) -> str | None:
    """Serve the viewer from a daemon thread (the CLI's embedded mode). Falls
    back to an ephemeral port when `port` is taken (a second concurrent
    session); returns the URL, or None if the server couldn't start — fail-open,
    the viewer must never block a session."""
    try:
        try:
            server = WatchServer(Path(target), port=port)
        except OSError:
            server = WatchServer(Path(target), port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server.url
    except Exception:  # noqa: BLE001 — observability, never a blocker
        log.debug("watch server failed to start", exc_info=True)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pyharness-watch",
        description="Live web view of a pyharness session (tails trace.jsonl).",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=".sessions",
        help="a session dir (contains trace.jsonl) or a dir of sessions; "
        "with the latter, follows the most recently active session (default: .sessions)",
    )
    parser.add_argument("--port", type=int, default=6061)
    parser.add_argument(
        "--static",
        metavar="OUT",
        help="don't serve: bake the session(s) under `target` into "
        "self-contained HTML in OUT (plus an index) and exit",
    )
    parser.add_argument(
        "--title", default="pyharness sessions", help="index page title with --static"
    )
    parser.add_argument("--lede", default="", help="index page subtitle with --static")
    parser.add_argument(
        "--doc",
        action="append",
        default=[],
        metavar="LABEL=PATH",
        help="with --static: also render a markdown file (an eval board) as a "
        "page in the site, linked from the nav. Repeatable.",
    )
    args = parser.parse_args()
    target = Path(args.target).expanduser()
    if args.static:
        from .static import build_site, discover_sessions

        sessions = discover_sessions(target)
        if not sessions:
            raise SystemExit(f"no sessions with a trace.jsonl under {target}")
        docs = []
        for spec in args.doc:
            label, sep, path = spec.partition("=")
            if not sep or not Path(path).is_file():
                raise SystemExit(f"--doc wants LABEL=PATH with a readable file: {spec}")
            docs.append((label, Path(path)))
        written = build_site(
            sessions, args.static, title=args.title, lede=args.lede, docs=docs
        )
        print(f"wrote {len(written)} files to {args.static}")
        return
    server = WatchServer(target, port=args.port)
    print(f"watching {args.target} → {server.url}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# The page shell, the stylesheet, and the renderer all live in `obs/page.py` and
# `obs/assets/` — shared verbatim with the baked pages and the site. This module
# only fills the two slots.
_PAGE_TEMPLATE = session_template()

# The live feed: events arrive over SSE as the session writes them. The static
# builder (obs/static.py) substitutes its own feed here — a baked array replayed
# through the same `handle()` — so both views are the *same page* rendering the
# same events, and a change to the renderer cannot drift between them.
LIVE_FEED = r"""
let source = new EventSource('/events');
function bind(s) {
  s.onopen = () => setStatus('live', 'live');
  s.onerror = () => setStatus('reconnecting…', 'warn');
  s.onmessage = (m) => {
    try { handle(JSON.parse(m.data)); } catch (err) { /* torn line; the next wins */ }
  };
}
bind(source);

// Switch the stream without reloading: drop the current subscription, clear the
// log, and re-open pinned to the chosen session.
window.switchTo = (name) => {
  if (name === currentSession) return;
  source.close();
  resetAll(name);
  source = new EventSource('/events?session=' + encodeURIComponent(name));
  bind(source);
  renderSessionList();
};

// The sidebar list, polled. `follow` decides whether a session that starts
// while you are reading an old one takes the view.
const pollSessions = () => {
  fetch('/sessions').then((r) => r.json()).then((list) => {
    const follow = document.getElementById('follow');
    if (follow && follow.checked && list.length && list[0].name !== currentSession) {
      window.switchTo(list[0].name);
    }
    renderSessionList(list);
  }).catch(() => { /* the viewer is never the thing that breaks a session */ });
};
pollSessions();
setInterval(pollSessions, 4000);
"""

_FEED_SLOT = FEED_SLOT
_TITLE_SLOT = TITLE_SLOT


def render_page(feed_js: str, *, title: str = "pyharness — live") -> str:
    """The viewer page wired to a feed. `feed_js` is JavaScript that calls
    `handle(event)` — from SSE (live) or from a baked array (static).

    Raises if either slot has gone missing rather than quietly returning a page
    with no feed in it: a viewer that renders an empty log is indistinguishable
    from a session that did nothing."""
    for slot in (_FEED_SLOT, _TITLE_SLOT):
        if slot not in _PAGE_TEMPLATE:
            raise RuntimeError(f"viewer page lost its {slot} slot")
    return _PAGE_TEMPLATE.replace(_FEED_SLOT, feed_js).replace(
        _TITLE_SLOT, escape(title)
    )


PAGE = render_page(LIVE_FEED)


if __name__ == "__main__":
    main()
