"""`pyharness-watch` — the live session viewer.

A small local web page that tails `trace.jsonl` and renders the session as it
happens: the current turn, each code cell and its output, in-flight actions
with elapsed time (an `action_start` without its `action_end` *is* the thing
running or stuck), pending approvals, errors, and running spend. The durable
JSONL record is written synchronously by the session, so this view is
real-time by construction — no collector, no container, no refresh.

Stdlib only. The server binds 127.0.0.1 and serves two routes: `/` (the page)
and `/events` (Server-Sent Events: each trace entry as one JSON `data:` line,
tagged with the session it came from). Watch one session (a dir containing
`trace.jsonl`) or a container of sessions (e.g. `.sessions/`). It follows a whole
session *tree* — the root session plus any sub-agents it spawns
(`{root}-spawn-*`) — and switches the top-level view only when a genuinely new
*root* session starts, so a running sub-agent never steals the view.

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


class _Handler(BaseHTTPRequestHandler):
    server: WatchServer

    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        pass  # request logging is noise for a local single-user viewer

    def do_GET(self):  # noqa: N802 — stdlib naming
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/events":
            self._serve_events()
        elif self.path.startswith("/media/"):
            self._serve_media(self.path[len("/media/") :])
        else:
            self.send_error(404)

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
        body = path.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        tail = Tail(self.server.target)
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
    args = parser.parse_args()
    target = Path(args.target).expanduser()
    if args.static:
        from .static import build_site, discover_sessions

        sessions = discover_sessions(target)
        if not sessions:
            raise SystemExit(f"no sessions with a trace.jsonl under {target}")
        written = build_site(sessions, args.static, title=args.title)
        print(f"wrote {len(written)} files to {args.static}")
        return
    server = WatchServer(target, port=args.port)
    print(f"watching {args.target} → {server.url}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# The page template. Self-contained (no external assets); renders an event feed
# into a turn-by-turn view. Each event is tagged with the session it came from: root
# events land in the main log, a spawned sub-agent's events land in a nested,
# collapsible panel opened at its spawn point. Sticky top region holds the
# session/spend header, the filter+search toolbar, a pending-approval banner, and
# the "now" bar for in-flight work. All trace text lands via textContent, never
# innerHTML — trace content is untrusted.
_PAGE_TEMPLATE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>@@TITLE@@</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #101216; color: #d6dae2;
         font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; }
  #top { position: sticky; top: 0; z-index: 3; background: #101216;
         border-bottom: 1px solid #262c37; }
  header { padding: 10px 18px 8px; display: flex; gap: 16px;
           align-items: baseline; flex-wrap: wrap; }
  header .name { font-weight: 600; color: #fff; }
  header .spend { color: #8fd18f; font-variant-numeric: tabular-nums; }
  #toolbar { padding: 0 18px 8px; display: flex; gap: 14px; align-items: center;
             flex-wrap: wrap; font-size: 12px; color: #9aa3b2; }
  #toolbar label { cursor: pointer; user-select: none; }
  #toolbar input[type=search] { background: #171b22; border: 1px solid #2c3548;
             color: #d6dae2; border-radius: 5px; padding: 3px 8px; width: 200px;
             font: inherit; }
  #toolbar button { background: #1d2330; border: 1px solid #2c3548; color: #b9c1cf;
             border-radius: 5px; padding: 2px 9px; cursor: pointer; font: inherit; }
  #toolbar button:hover { background: #262d3d; }
  #banner:empty, #now:empty { display: none; }
  #banner { padding: 4px 18px 8px; }
  #banner .approval { background: #3a2d10; border: 1px solid #8a6d1a; color: #ffd977;
             border-radius: 6px; padding: 8px 12px; display: flex; gap: 10px;
             align-items: baseline; }
  #banner .elapsed { margin-left: auto; font-variant-numeric: tabular-nums; }
  #now { padding: 0 18px 6px; }
  #now .item { background: #1d2330; border: 1px solid #2c3548; border-radius: 6px;
               padding: 6px 12px; margin: 4px 0; display: flex; gap: 10px;
               align-items: baseline; }
  #now .elapsed { margin-left: auto; color: #7d8697; font-variant-numeric: tabular-nums; }
  #now .spinner { color: #6fa8ff; }
  main { max-width: 980px; margin: 0 auto; padding: 12px 18px 80px; }
  .task { margin: 22px 0 10px; padding: 10px 14px; background: #1a2130;
          border-left: 3px solid #6fa8ff; border-radius: 4px; color: #eef2f8;
          white-space: pre-wrap; }
  .agent { margin: 10px 0; white-space: pre-wrap; }
  .meta { color: #7d8697; font-size: 12px; margin-left: 8px; }
  details.cell, details.prompt { margin: 8px 0; }
  details.cell > summary, details.prompt > summary { cursor: pointer; color: #7d8697;
          font: 12px ui-monospace, monospace; padding: 2px 0; list-style: none;
          display: flex; gap: 8px; align-items: center; }
  details.cell > summary::-webkit-details-marker,
  details.prompt > summary::-webkit-details-marker { display: none; }
  summary .tag { color: #5f6b7e; }
  summary .copy { margin-left: auto; border: 1px solid #2c3548; background: #171b22;
          color: #9aa3b2; border-radius: 4px; padding: 0 6px; cursor: pointer; font: inherit; }
  pre { background: #171b22; border: 1px solid #232a35; border-radius: 6px;
        padding: 10px 12px; overflow-x: auto; margin: 4px 0;
        font: 12.5px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; }
  pre.code { border-left: 3px solid #3f679f; }
  pre.output { color: #9aa3b2; max-height: 340px; overflow-y: auto; }
  .prompt pre { max-height: 260px; overflow-y: auto; }
  .prompt .role { color: #6fa8ff; font: 11px ui-monospace, monospace; margin: 8px 0 2px; }
  .prompt .sys { color: #c8a6ff; }
  .action { font: 12px ui-monospace, monospace; color: #7d8697; margin: 2px 0 2px 12px; }
  .action.err { color: #ff8484; }
  .row.error { color: #ff8484; white-space: pre-wrap; margin: 8px 0; }
  .row.notify { color: #ffd977; margin: 8px 0; }
  .row.small { color: #7d8697; font-size: 12px; margin: 6px 0; }
  .answer { margin: 14px 0; padding: 10px 14px; background: #16241a;
            border-left: 3px solid #58b368; border-radius: 4px; white-space: pre-wrap; }
  img.shot { max-width: 100%; border: 1px solid #232a35; border-radius: 6px; margin: 4px 0; }
  .subagent { margin: 12px 0; border: 1px solid #34405a; border-radius: 8px;
              background: #12161f; overflow: hidden; }
  .subagent > summary { cursor: pointer; padding: 8px 12px; list-style: none;
              display: flex; gap: 10px; align-items: baseline; background: #172033;
              border-left: 3px solid #8a6dff; }
  .subagent > summary::-webkit-details-marker { display: none; }
  .subagent .sub-name { color: #c3b6ff; font-weight: 600; }
  .subagent .sub-task { color: #9aa3b2; overflow: hidden; text-overflow: ellipsis;
              white-space: nowrap; max-width: 40ch; }
  .subagent .sub-stat { margin-left: auto; color: #7d8697; font-variant-numeric: tabular-nums; }
  .subagent .dot { width: 8px; height: 8px; border-radius: 50%; background: #6fa8ff; }
  .subagent .dot.done { background: #58b368; }
  .subagent .dot.err { background: #ff8484; }
  .subagent .sub-body { padding: 4px 14px 10px; }
  details.think > summary { color: #8a7fb0; }
  .think pre { color: #9a92b8; font-style: italic; border-left: 3px solid #4a3f6e;
               max-height: 260px; overflow-y: auto; }
  body.hide-code .k-code, body.hide-output .k-output,
  body.hide-action .k-action, body.hide-prompt .k-prompt,
  body.hide-think .k-think { display: none; }
  .search-hide { display: none !important; }
</style>
</head>
<body>
<div id="top">
  <header>
    <span class="name" id="session">waiting for a session…</span>
    <span class="spend" id="spend"></span>
    <span class="meta" id="steps"></span>
    <span class="meta" id="clock"></span>
    <span class="meta" id="status">connecting…</span>
  </header>
  <div id="toolbar">
    <label><input type="checkbox" data-f="code" checked> code</label>
    <label><input type="checkbox" data-f="output" checked> output</label>
    <label><input type="checkbox" data-f="action" checked> actions</label>
    <label><input type="checkbox" data-f="prompt" checked> prompts</label>
    <label><input type="checkbox" data-f="think" checked> thinking</label>
    <input type="search" id="search" placeholder="filter text…">
    <span id="searchcount"></span>
    <button id="jump-err">next error</button>
    <button id="jump-top">top</button>
    <button id="jump-bot">bottom</button>
  </div>
  <div id="banner"></div>
  <div id="now"></div>
</div>
<main id="log"></main>
<script>
const rootLog = document.getElementById('log');
const nowEl = document.getElementById('now');
const bannerEl = document.getElementById('banner');
const active = new Map();     // laneKey -> {label, kind, t0, lane}
const approvals = new Map();  // key -> {label, t0}
const lanes = new Map();      // session name -> lane
let rootName = null;
let followTail = true;
let sessionStartMs = 0;

addEventListener('scroll', () => {
  followTail = (innerHeight + scrollY) >= document.body.scrollHeight - 60;
});
function scrollTail() { if (followTail) scrollTo(0, document.body.scrollHeight); }

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
function fmtUsd(x) { return '$' + Number(x).toFixed(2); }

// ---- lanes: the root session's log plus one nested panel per sub-agent -------
function makeLane(name, logEl, isRoot, head) {
  const lane = { name, logEl, isRoot, head, spend: 0 };
  lanes.set(name, lane);
  return lane;
}
function laneFor(name) {
  if (!name || name === rootName) return lanes.get(rootName);
  return lanes.get(name) || openSubLane(name, null, lanes.get(rootName));
}
function openSubLane(name, spawn, parent) {
  if (lanes.get(name)) return lanes.get(name);
  const panel = el('details', 'subagent');
  const sum = el('summary');
  const dot = el('span', 'dot');
  sum.appendChild(dot);
  sum.appendChild(el('span', 'sub-name', name.replace(/^.*-spawn-/, 'spawn-')));
  if (spawn) {
    if (spawn.tier) sum.appendChild(el('span', 'tag', spawn.tier));
    sum.appendChild(el('span', 'sub-task', spawn.text || ''));
  }
  const stat = el('span', 'sub-stat', 'running…');
  sum.appendChild(stat);
  panel.appendChild(sum);
  const body = el('div', 'sub-body');
  panel.appendChild(body);
  (parent ? parent.logEl : rootLog).appendChild(panel);
  scrollTail();
  const lane = makeLane(name, body, false, { dot, stat, budget: spawn && spawn.budget_usd });
  return lane;
}
function laneAdd(lane, node, kindClass) {
  node.classList.add('item-row');
  if (kindClass) node.classList.add(kindClass);
  lane.logEl.appendChild(node);
  applyRowVisibility(node);
  scrollTail();
}

// ---- spend rollup across every lane ------------------------------------------
function updateSpend() {
  let total = 0;
  for (const l of lanes.values()) total += l.spend;
  document.getElementById('spend').textContent = fmtUsd(total);
  let steps = 0;
  for (const r of rootLog.querySelectorAll('.k-code')) steps++;
  document.getElementById('steps').textContent = steps + ' steps';
}
function laneSpent(lane, usd, cumulative) {
  if (usd == null) return;
  lane.spend = cumulative ? Math.max(lane.spend, usd) : lane.spend + usd;
  if (lane.head) lane.head.stat.textContent = fmtUsd(lane.spend)
      + (lane.head.budget ? ' / ' + fmtUsd(lane.head.budget) : '');
  updateSpend();
}
function laneDone(lane, cls, label) {
  if (!lane.head) return;
  lane.head.dot.className = 'dot ' + cls;
  const base = lane.head.stat.textContent.split(' — ')[0];
  lane.head.stat.textContent = base + ' — ' + label;
}

// ---- in-flight "now" bar + approval banner -----------------------------------
function renderNow() {
  nowEl.replaceChildren();
  for (const [, a] of active) {
    const item = el('div', 'item');
    item.appendChild(el('span', 'spinner', '●'));
    const lbl = (a.lane && !a.lane.isRoot ? '[' + a.lane.name.replace(/^.*-spawn-/, 'spawn-') + '] ' : '') + a.label;
    item.appendChild(el('span', '', lbl));
    item.appendChild(el('span', 'elapsed', ''));
    item.dataset.t0 = a.t0;
    nowEl.appendChild(item);
  }
}
function renderBanner() {
  bannerEl.replaceChildren();
  for (const [, a] of approvals) {
    const item = el('div', 'approval');
    item.appendChild(el('span', '', '⚠'));
    item.appendChild(el('span', '', a.label));
    item.appendChild(el('span', 'elapsed', ''));
    item.dataset.t0 = a.t0;
    bannerEl.appendChild(item);
  }
}
const ticker = setInterval(() => {
  for (const item of [...nowEl.children, ...bannerEl.children]) {
    const s = (Date.now() - Number(item.dataset.t0)) / 1000;
    item.querySelector('.elapsed').textContent = s.toFixed(0) + 's';
  }
  if (sessionStartMs) {
    const s = (Date.now() - sessionStartMs) / 1000;
    const m = Math.floor(s / 60);
    document.getElementById('clock').textContent = m + 'm' + String(Math.floor(s % 60)).padStart(2, '0') + 's';
  }
}, 500);
function startActive(lane, sub, label) {
  active.set(lane.name + '\x00' + sub, { label, t0: Date.now(), lane });
  renderNow();
}
function endActive(lane, sub) { active.delete(lane.name + '\x00' + sub); renderNow(); }

// ---- collapsible code/output cell with a copy button -------------------------
function cell(kind, text) {
  const d = el('details', 'cell k-' + kind); d.open = true;
  const sum = el('summary');
  sum.appendChild(el('span', 'tag', kind));
  const first = (text || '').split('\n', 1)[0].slice(0, 80);
  sum.appendChild(el('span', '', first));
  const chars = (text || '').length;
  sum.appendChild(el('span', 'tag', chars + ' chars'));
  const copy = el('button', 'copy', 'copy');
  copy.onclick = (ev) => { ev.preventDefault(); ev.stopPropagation();
    navigator.clipboard && navigator.clipboard.writeText(text || ''); };
  sum.appendChild(copy);
  d.appendChild(sum);
  d.appendChild(el('pre', kind, text));
  return d;
}

// ---- the collapsed "full prompt" view (system + every message this pass) -----
function promptView(e) {
  const d = el('details', 'prompt k-prompt');
  const sum = el('summary');
  const n = (e.messages || []).length;
  sum.appendChild(el('span', 'tag', 'prompt'));
  sum.appendChild(el('span', '', n + ' msgs'
    + (e.input_tokens ? ' · ' + e.input_tokens + ' tok in' : '')
    + (e.model ? ' · ' + e.model : '')));
  d.appendChild(sum);
  let built = false;
  d.addEventListener('toggle', () => {  // build lazily on first expand
    if (!d.open || built) return;
    built = true;
    if (e.system) {
      d.appendChild(el('div', 'role sys', 'system'));
      d.appendChild(el('pre', '', e.system));
    }
    for (const m of e.messages || []) {
      d.appendChild(el('div', 'role', m.role));
      if (typeof m.text === 'string') { d.appendChild(el('pre', '', m.text)); continue; }
      for (const b of m.content || []) {
        if (b.type === 'text') d.appendChild(el('pre', '', b.text || ''));
        else if (b.type === 'tool_use') d.appendChild(el('pre', '', '→ ' + (b.name || '')
             + '\n' + ((b.input && b.input.code) || JSON.stringify(b.input || {}))));
        else if (b.type === 'tool_result') d.appendChild(el('pre', '', '⟵ result\n'
             + (typeof b.content === 'string' ? b.content : JSON.stringify(b.content))));
        else if (b.type === 'image') d.appendChild(el('pre', '', '[image]'));
        else d.appendChild(el('pre', '', JSON.stringify(b)));
      }
    }
  });
  return d;
}

// ---- filters, search, jump ---------------------------------------------------
let searchTerm = '';
function applyRowVisibility(node) {
  if (searchTerm && !node.textContent.toLowerCase().includes(searchTerm))
    node.classList.add('search-hide');
  else node.classList.remove('search-hide');
}
for (const cb of document.querySelectorAll('#toolbar input[data-f]')) {
  cb.onchange = () => document.body.classList.toggle('hide-' + cb.dataset.f, !cb.checked);
}
document.getElementById('search').oninput = (ev) => {
  searchTerm = ev.target.value.toLowerCase();
  let hits = 0;
  for (const r of rootLog.querySelectorAll('.item-row')) {
    applyRowVisibility(r);
    if (searchTerm && !r.classList.contains('search-hide')) hits++;
  }
  document.getElementById('searchcount').textContent = searchTerm ? hits + ' match' : '';
};
let errIdx = -1;
document.getElementById('jump-err').onclick = () => {
  const errs = [...rootLog.querySelectorAll('.row.error, .action.err')];
  if (!errs.length) return;
  errIdx = (errIdx + 1) % errs.length;
  errs[errIdx].scrollIntoView({ block: 'center' });
};
document.getElementById('jump-top').onclick = () => { followTail = false; scrollTo(0, 0); };
document.getElementById('jump-bot').onclick = () => { followTail = true; scrollTail(); };

// ---- event dispatch ----------------------------------------------------------
function resetAll(name) {
  rootLog.replaceChildren(); nowEl.replaceChildren(); bannerEl.replaceChildren();
  active.clear(); approvals.clear(); lanes.clear();
  rootName = name; sessionStartMs = 0;
  makeLane(name, rootLog, true, null);
  document.getElementById('session').textContent = name;
  updateSpend();
}

function handle(e) {
  const k = e.kind;
  if (k === 'watch_session') { resetAll(e.session); return; }
  if (rootName === null) resetAll(e.session || 'session');
  if (!sessionStartMs) sessionStartMs = Date.now();
  const lane = laneFor(e.session);

  if (k === 'spawn') {
    openSubLane(e.child, e, lane);
  } else if (k === 'spawned_by') {
    // child->parent link; the panel already carries the relationship
  } else if (k === 'session_start') {
    laneAdd(lane, el('div', 'row small', 'session started — ' + (e.root || '')));
  } else if (k === 'task') {
    laneAdd(lane, el('div', 'task', e.text));
  } else if (k === 'llm_start') {
    startActive(lane, 'llm', 'thinking (' + (e.tier || '?') + ')…');
  } else if (k === 'llm_thinking') {
    // Summarized thinking, streamed. Collapsed by default; the summary row is
    // the expand button. One block per completion — llm_call closes it.
    if (!lane.think) {
      const d = el('details', 'cell think');
      const sum = el('summary');
      sum.appendChild(el('span', 'tag', 'thinking'));
      const meta = el('span', 'tag', '');
      sum.appendChild(meta);
      d.appendChild(sum);
      const pre = el('pre', '', '');
      d.appendChild(pre);
      lane.think = { pre, meta, chars: 0 };
      laneAdd(lane, d, 'k-think');
    }
    lane.think.pre.textContent += e.text || '';
    lane.think.chars += (e.text || '').length;
    lane.think.meta.textContent = lane.think.chars + ' chars';
  } else if (k === 'llm_call') {
    endActive(lane, 'llm');
    lane.think = null;
    if (e.text) {
      const n = el('div', 'agent', e.text);
      const bits = [];
      if (e.cost_usd) bits.push(fmtUsd(e.cost_usd));
      if (e.latency_s) bits.push(e.latency_s + 's');
      if (e.input_tokens) bits.push(e.input_tokens + ' in / ' + (e.output_tokens || 0) + ' out');
      if (bits.length) n.appendChild(el('span', 'meta', ' · ' + bits.join(' · ')));
      laneAdd(lane, n);
    }
    laneAdd(lane, promptView(e), 'k-prompt');
    laneSpent(lane, e.cost_usd, false);
  } else if (k === 'code') {
    laneAdd(lane, cell('code', e.text), 'k-code'); updateSpend();
  } else if (k === 'output') {
    laneAdd(lane, cell('output', e.text), 'k-output');
  } else if (k === 'media') {
    const img = el('img', 'shot'); img.src = e.src || ''; img.alt = e.text || 'image';
    laneAdd(lane, img, 'k-output');
  } else if (k === 'action_start') {
    startActive(lane, 'a:' + e.text, e.text + (e.args ? '(' + e.args + ')' : ''));
  } else if (k === 'action_end') {
    endActive(lane, 'a:' + e.text);
    const ok = e.ok !== false;
    const label = (ok ? '✓ ' : '✗ ') + e.text + ' ' + (e.elapsed_s || 0) + 's'
                  + (e.error ? ' — ' + e.error : '') + (e.decision === 'deny' ? ' — denied by policy' : '');
    laneAdd(lane, el('div', 'action' + (ok ? '' : ' err'), label), 'k-action');
  } else if (k === 'approval_pending') {
    const who = lane.isRoot ? '' : '[' + lane.name.replace(/^.*-spawn-/, 'spawn-') + '] ';
    approvals.set(e.text, { label: who + '[' + (e.category || '') + '] ' + e.text
      + ' — ' + (e.summary || '') + ' (answer in the terminal)', t0: Date.now() });
    renderBanner();
  } else if (k === 'approval_resolved') {
    approvals.delete(e.text); renderBanner();
    laneAdd(lane, el('div', 'row small', 'approval ' + e.outcome + ': ' + e.text));
  } else if (k === 'notify') {
    laneAdd(lane, el('div', 'row notify', '[agent note] ' + e.text));
  } else if (k === 'error') {
    endActive(lane, 'llm');
    lane.think = null;
    laneAdd(lane, el('div', 'row error', e.text));
    if (!lane.isRoot) laneDone(lane, 'err', 'error');
  } else if (k === 'answer') {
    laneAdd(lane, el('div', 'answer', e.text));
    if (!lane.isRoot) laneDone(lane, 'done', 'answered');
  } else if (k === 'budget') {
    laneSpent(lane, e.spent_usd, true);
  } else if (k === 'session_end') {
    endActive(lane, 'llm');
    laneSpent(lane, e.spent_usd, true);
    laneAdd(lane, el('div', 'row small', 'session ended — spent ' + fmtUsd(e.spent_usd || 0)
           + ' over ' + (e.calls || 0) + ' calls'));
    if (!lane.isRoot && lane.head && lane.head.dot.className === 'dot')
      laneDone(lane, 'done', 'done');
  } else if (k === 'skill_use') {
    laneAdd(lane, el('div', 'row small', 'skill ' + (e.skill || '') + ': ' + (e.outcome || '') + ' ' + (e.note || '')));
  } else if (k === 'reflection') {
    laneAdd(lane, el('div', 'row small', 'reflection: ' + (e.text || '')));
  } else if (k === 'worker') {
    // llm()/map_llm progress heartbeat — the broker's action spinner shows the
    // call is live, this shows fan-out progress within it.
    laneAdd(lane, el('div', 'row small', e.text || ''));
  } else if (k === 'llm_attempt') {
    // Orchestrator-call retry record: a stalling-and-retrying completion
    // reads as attempts, not as a silent gap.
    laneAdd(lane, el('div', 'row small', 'llm ' + (e.text || '')));
  } else if (k === 'note') {
    // preamble text duplicated by the llm_call event — skip
  } else {
    // Unknown kind. A viewer that silently drops what it does not recognize is
    // worse than one that renders it plainly: a new trace kind (or an old
    // session replayed by a newer viewer) would simply not exist on screen,
    // with nothing to notice. Render the kind, its text, and whatever other
    // fields it carried, so the event is visible and legible without the
    // viewer having to know what it means.
    const rest = Object.keys(e)
      .filter((f) => !['kind', 'ts', 'text', 'session'].includes(f))
      .map((f) => f + '=' + (typeof e[f] === 'object' ? JSON.stringify(e[f]) : e[f]));
    laneAdd(lane, el('div', 'row small', k
      + (e.text ? ': ' + e.text : '')
      + (rest.length ? ' · ' + rest.join(' ') : '')));
  }
}

/* @@FEED@@ */
</script>
</body>
</html>
"""

# The live feed: events arrive over SSE as the session writes them. The static
# builder (obs/static.py) substitutes its own feed here — a baked array replayed
# through the same `handle()` — so both views are the *same page* rendering the
# same events, and a change to the renderer cannot drift between them.
LIVE_FEED = r"""
const source = new EventSource('/events');
source.onopen = () => { document.getElementById('status').textContent = 'live'; };
source.onerror = () => { document.getElementById('status').textContent = 'reconnecting…'; };
source.onmessage = (m) => { try { handle(JSON.parse(m.data)); } catch (err) {} };
"""

_FEED_SLOT = "/* @@FEED@@ */"
_TITLE_SLOT = "@@TITLE@@"


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
