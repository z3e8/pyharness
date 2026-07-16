"""`pyharness-watch` — the live session viewer.

A small local web page that tails `trace.jsonl` and renders the session as it
happens: the current turn, each code cell and its output, in-flight actions
with elapsed time (an `action_start` without its `action_end` *is* the thing
running or stuck), pending approvals, errors, and running spend. The durable
JSONL record is written synchronously by the session, so this view is
real-time by construction — no collector, no container, no refresh.

Stdlib only. The server binds 127.0.0.1 and serves two routes: `/` (the page)
and `/events` (Server-Sent Events: each trace entry as one JSON `data:` line).
Watch one session (a dir containing `trace.jsonl`) or a container of sessions
(e.g. `.sessions/`), where it follows the most recently active session and
switches automatically when a new one starts.

Two entry points: `main()` (the `pyharness-watch` console script) and
`start_in_thread()` (the CLI embeds the viewer in the agent process — fail-open,
a dead port never blocks a session).
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .transcript import latest_session

log = logging.getLogger("pyharness.watch")

_POLL_S = 0.25
_KEEPALIVE_POLLS = 20  # one SSE comment every ~5s so dead clients are noticed


def _pick_trace(target: Path) -> Path | None:
    """The trace file to follow: `target/trace.jsonl` when target is itself a
    session root, else the most recently modified `*/trace.jsonl` under it."""
    session = latest_session(target)
    return None if session is None else session / "trace.jsonl"


class Tail:
    """Incremental, non-blocking reader over the followed trace. Each `poll()`
    returns the trace entries appended since the last call — plus a synthetic
    `watch_session` event whenever the followed file changes (a new session
    started under a container target)."""

    def __init__(self, target: Path):
        self.target = Path(target)
        self.path: Path | None = None
        self.offset = 0

    def poll(self) -> list[dict]:
        events: list[dict] = []
        current = _pick_trace(self.target)
        if current != self.path:
            self.path = current
            self.offset = 0
            if current is not None:
                events.append({"kind": "watch_session", "session": current.parent.name})
        if self.path is None:
            return events
        try:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return events
        end = chunk.rfind(b"\n")
        if end == -1:
            return events  # no complete new line yet
        for raw in chunk[: end + 1].splitlines():
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(entry, dict):
                events.append(entry)
        self.offset += end + 1
        return events


class _Handler(BaseHTTPRequestHandler):
    server: "WatchServer"

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
        else:
            self.send_error(404)

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
    args = parser.parse_args()
    server = WatchServer(Path(args.target).expanduser(), port=args.port)
    print(f"watching {args.target} → {server.url}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# The page. Self-contained (no external assets); renders the SSE stream into a
# turn-by-turn view with a sticky "now" bar for in-flight work. All trace text
# lands via textContent, never innerHTML — trace content is untrusted.
PAGE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>pyharness — live</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #101216; color: #d6dae2;
         font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; }
  header { position: sticky; top: 0; z-index: 2; background: #161a21;
           border-bottom: 1px solid #262c37; padding: 10px 18px;
           display: flex; gap: 16px; align-items: baseline; flex-wrap: wrap; }
  header .name { font-weight: 600; color: #fff; }
  header .spend { color: #8fd18f; font-variant-numeric: tabular-nums; }
  #now { position: sticky; top: 45px; z-index: 2; padding: 0 18px; }
  #now .item { background: #1d2330; border: 1px solid #2c3548; border-radius: 6px;
               padding: 6px 12px; margin: 6px 0; display: flex; gap: 10px;
               align-items: baseline; }
  #now .item.approval { background: #3a2d10; border-color: #8a6d1a; color: #ffd977; }
  #now .elapsed { margin-left: auto; color: #7d8697; font-variant-numeric: tabular-nums; }
  #now .spinner { color: #6fa8ff; }
  main { max-width: 980px; margin: 0 auto; padding: 12px 18px 80px; }
  .task { margin: 26px 0 10px; padding: 10px 14px; background: #1a2130;
          border-left: 3px solid #6fa8ff; border-radius: 4px; color: #eef2f8;
          white-space: pre-wrap; }
  .agent { margin: 10px 0; white-space: pre-wrap; }
  .meta { color: #7d8697; font-size: 12px; margin-left: 8px; }
  pre { background: #171b22; border: 1px solid #232a35; border-radius: 6px;
        padding: 10px 12px; overflow-x: auto; margin: 8px 0;
        font: 12.5px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; }
  pre.code { border-left: 3px solid #3f679f; }
  pre.output { color: #9aa3b2; max-height: 320px; overflow-y: auto; }
  .action { font: 12px ui-monospace, monospace; color: #7d8697; margin: 2px 0 2px 12px; }
  .action.err { color: #ff8484; }
  .row.error { color: #ff8484; white-space: pre-wrap; margin: 8px 0; }
  .row.notify { color: #ffd977; margin: 8px 0; }
  .row.small { color: #7d8697; font-size: 12px; margin: 6px 0; }
  .answer { margin: 14px 0; padding: 10px 14px; background: #16241a;
            border-left: 3px solid #58b368; border-radius: 4px; white-space: pre-wrap; }
</style>
</head>
<body>
<header>
  <span class="name" id="session">waiting for a session…</span>
  <span class="spend" id="spend"></span>
  <span class="meta" id="status">connecting…</span>
</header>
<div id="now"></div>
<main id="log"></main>
<script>
const logEl = document.getElementById('log');
const nowEl = document.getElementById('now');
const active = new Map();  // key -> {label, kind, t0}
let followTail = true;
let spend = 0;

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
function add(node) { logEl.appendChild(node); scrollTail(); }

function renderNow() {
  nowEl.replaceChildren();
  for (const [, a] of active) {
    const item = el('div', 'item' + (a.kind === 'approval' ? ' approval' : ''));
    item.appendChild(el('span', a.kind === 'approval' ? '' : 'spinner',
                        a.kind === 'approval' ? '⚠' : '●'));
    item.appendChild(el('span', '', a.label));
    item.appendChild(el('span', 'elapsed', ''));
    item.dataset.t0 = a.t0;
    nowEl.appendChild(item);
  }
}
setInterval(() => {
  for (const item of nowEl.children) {
    const s = (Date.now() - Number(item.dataset.t0)) / 1000;
    item.querySelector('.elapsed').textContent = s.toFixed(0) + 's';
  }
}, 500);

function startActive(key, label, kind) {
  active.set(key, { label, kind, t0: Date.now() });
  renderNow();
}
function endActive(key) { active.delete(key); renderNow(); }

function fmtUsd(x) { return '$' + Number(x).toFixed(2); }

function handle(e) {
  const k = e.kind;
  if (k === 'watch_session') {
    document.getElementById('session').textContent = e.session;
    logEl.replaceChildren(); active.clear(); renderNow(); spend = 0;
  } else if (k === 'session_start') {
    add(el('div', 'row small', 'session started — ' + (e.root || '')));
  } else if (k === 'task') {
    add(el('div', 'task', e.text));
  } else if (k === 'llm_start') {
    startActive('llm', 'thinking (' + (e.tier || '?') + ')…', 'llm');
  } else if (k === 'llm_call') {
    endActive('llm');
    if (e.text) {
      const n = el('div', 'agent', e.text);
      const bits = [];
      if (e.cost_usd) bits.push(fmtUsd(e.cost_usd));
      if (e.latency_s) bits.push(e.latency_s + 's');
      if (e.input_tokens) bits.push(e.input_tokens + ' in / ' + (e.output_tokens || 0) + ' out');
      if (bits.length) n.appendChild(el('span', 'meta', ' · ' + bits.join(' · ')));
      add(n);
    }
    if (e.cost_usd) { spend += e.cost_usd; document.getElementById('spend').textContent = fmtUsd(spend); }
  } else if (k === 'code') {
    add(el('pre', 'code', e.text));
  } else if (k === 'output') {
    add(el('pre', 'output', e.text));
  } else if (k === 'action_start') {
    startActive('a:' + e.text, e.text + (e.args ? '(' + e.args + ')' : ''), 'action');
  } else if (k === 'action_end') {
    endActive('a:' + e.text);
    const ok = e.ok !== false;
    const label = (ok ? '✓ ' : '✗ ') + e.text + ' ' + (e.elapsed_s || 0) + 's'
                  + (e.error ? ' — ' + e.error : '') + (e.decision === 'deny' ? ' — denied by policy' : '');
    add(el('div', 'action' + (ok ? '' : ' err'), label));
  } else if (k === 'approval_pending') {
    startActive('ap:' + e.text, 'waiting for approval [' + (e.category || '') + '] ' + e.text
                + ' — ' + (e.summary || '') + ' (answer in the terminal)', 'approval');
  } else if (k === 'approval_resolved') {
    endActive('ap:' + e.text);
    add(el('div', 'row small', 'approval ' + e.outcome + ': ' + e.text));
  } else if (k === 'notify') {
    add(el('div', 'row notify', '[agent note] ' + e.text));
  } else if (k === 'error') {
    endActive('llm');
    add(el('div', 'row error', e.text));
  } else if (k === 'answer') {
    add(el('div', 'answer', e.text));
  } else if (k === 'budget') {
    spend = e.spent_usd || spend;
    document.getElementById('spend').textContent = fmtUsd(spend) + ' · ' + (e.calls || 0) + ' calls';
  } else if (k === 'session_end') {
    active.clear(); renderNow();
    add(el('div', 'row small', 'session ended — spent ' + fmtUsd(e.spent_usd || 0)
           + ' over ' + (e.calls || 0) + ' calls'));
  } else if (k === 'skill_use') {
    add(el('div', 'row small', 'skill ' + (e.skill || '') + ': ' + (e.outcome || '') + ' ' + (e.note || '')));
  } else if (k === 'reflection') {
    add(el('div', 'row small', 'reflection: ' + (e.text || '')));
  } else if (k === 'note') {
    // preamble text duplicated by the llm_call event — skip
  }
}

const source = new EventSource('/events');
source.onopen = () => { document.getElementById('status').textContent = 'live'; };
source.onerror = () => { document.getElementById('status').textContent = 'reconnecting…'; };
source.onmessage = (m) => { try { handle(JSON.parse(m.data)); } catch (err) {} };
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
