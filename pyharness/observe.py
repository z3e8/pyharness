"""A read-only web UI over a session's durable record.

It merges `trace.jsonl` (task/notes/cells/outputs/llm_calls/answer) and
`audit.jsonl` (every capability call) into one time-ordered timeline and serves
a live-refreshing page. Stdlib only; the page polls for updates so a running
session streams in.

    pyharness-observe [root] [--port 7117] [--no-browser]

`root` is either a single session directory (one with trace.jsonl/audit.jsonl)
or a parent holding several; both are discovered.
"""

from __future__ import annotations

import argparse
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_SIGNALS = ("trace.jsonl", "audit.jsonl")


def _is_session(d: Path) -> bool:
    return d.is_dir() and any((d / s).exists() for s in _SIGNALS)


def session_dirs(root: Path) -> dict[str, Path]:
    """Map display name -> session dir."""
    if _is_session(root):
        return {root.name or ".": root}
    found = {p.name: p for p in sorted(root.iterdir()) if _is_session(p)} if root.is_dir() else {}
    return found


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _session_meta(session_dir: Path) -> dict:
    rows = _read_jsonl(session_dir / "trace.jsonl")
    task = next((r.get("text", "") for r in rows if r.get("kind") == "task"), "")
    llm_calls = [r for r in rows if r.get("kind") == "llm_call"]
    total_cost = sum(r.get("cost_usd", 0) for r in llm_calls)
    return {
        "task": task[:100],
        "llm_calls": len(llm_calls),
        "cost": round(total_cost, 5),
    }


def build_timeline(session_dir: Path) -> dict:
    events: list[dict] = []
    for row in _read_jsonl(session_dir / "trace.jsonl"):
        events.append({"source": "trace", **row})
    for row in _read_jsonl(session_dir / "audit.jsonl"):
        events.append({"source": "audit", "kind": "call", **row})
    events.sort(key=lambda e: e.get("ts", 0))

    # Collapse llm_partial events: keep only the latest one if it's still live
    # (i.e., no llm_call has arrived after it). Once the full llm_call lands,
    # all preceding llm_partial entries are noise.
    last_llm_call_ts = max(
        (e.get("ts", 0) for e in events if e.get("kind") == "llm_call"), default=0
    )
    last_partial = max(
        (e for e in events if e.get("kind") == "llm_partial"),
        key=lambda e: e.get("ts", 0),
        default=None,
    )
    events = [
        e for e in events
        if e.get("kind") != "llm_partial"
        or (e is last_partial and e.get("ts", 0) > last_llm_call_ts)
    ]

    llm_calls = [e for e in events if e.get("kind") == "llm_call"]
    audit_calls = [e for e in events if e.get("source") == "audit"]
    budget_events = [e for e in events if e.get("kind") == "budget"]

    total_cost = sum(e.get("cost_usd", 0) for e in llm_calls)
    avg_latency = (
        sum(e.get("latency_s", 0) for e in llm_calls) / len(llm_calls) if llm_calls else 0
    )

    summary = {
        "tasks": sum(1 for e in events if e.get("kind") == "task"),
        "llm_calls": len(llm_calls),
        "total_cost": round(total_cost, 5),
        "avg_latency_s": round(avg_latency, 3),
        "calls": len(audit_calls),
        "denied": sum(1 for e in audit_calls if e.get("decision") == "deny"),
        "budget": budget_events[-1] if budget_events else None,
    }
    return {"events": events, "summary": summary}


class _Handler(BaseHTTPRequestHandler):
    root: Path

    def log_message(self, *args) -> None:
        pass

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: object) -> None:
        self._send(json.dumps(obj).encode(), "application/json")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(_PAGE.encode(), "text/html; charset=utf-8")
        elif parsed.path == "/api/sessions":
            dirs = session_dirs(self.root)
            result = []
            for name, d in dirs.items():
                meta = _session_meta(d)
                result.append({"name": name, "mtime": d.stat().st_mtime, **meta})
            self._json(result)
        elif parsed.path == "/api/session":
            name = parse_qs(parsed.query).get("name", [""])[0]
            d = session_dirs(self.root).get(name)
            if d is None:
                self.send_error(404, "no such session")
                return
            self._json(build_timeline(d))
        else:
            self.send_error(404)


def serve(root: Path, port: int) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"root": root})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Observability UI for pyharness sessions.")
    parser.add_argument("root", nargs="?", default=".sessions", help="session dir or parent of sessions")
    parser.add_argument("--port", type=int, default=7117)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    httpd = serve(root, args.port)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"pyharness-observe → {url}  (root: {root})\nCtrl-C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.shutdown()


_PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>pyharness — observe</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
         background: #0e1116; color: #d7dde5; display: flex; height: 100vh; overflow: hidden; }

  /* Sidebar */
  #sidebar { width: 260px; flex: none; border-right: 1px solid #232a33; overflow-y: auto; display: flex; flex-direction: column; }
  #sidebar h1 { font-size: 11px; padding: 12px 14px 10px; margin: 0; color: #6e7681;
                letter-spacing: .1em; text-transform: uppercase; border-bottom: 1px solid #232a33; flex: none; }
  #sessions { flex: 1; overflow-y: auto; }
  .session { padding: 10px 14px; cursor: pointer; border-bottom: 1px solid #161b22; }
  .session:hover { background: #161b22; }
  .session.active { background: #1a2230; border-left: 2px solid #58a6ff; padding-left: 12px; }
  .session .s-name { font-weight: 600; color: #d7dde5; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .session.active .s-name { color: #79c0ff; }
  .session .s-task { color: #6e7681; font-size: 11px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .session .s-meta { color: #484f58; font-size: 10px; margin-top: 4px; display: flex; gap: 10px; }
  .session .s-meta span { color: #8b949e; }

  /* Main */
  #main { flex: 1; overflow-y: auto; padding: 16px 20px; }

  /* Summary bar */
  #summary { color: #8b949e; margin-bottom: 16px; display: flex; gap: 20px; flex-wrap: wrap;
             padding: 10px 14px; background: #161b22; border-radius: 6px; border: 1px solid #21262d; }
  #summary .stat { display: flex; flex-direction: column; gap: 1px; }
  #summary .stat-label { font-size: 10px; text-transform: uppercase; letter-spacing: .08em; color: #6e7681; }
  #summary .stat-val { color: #d7dde5; font-size: 13px; }
  #summary .stat-val.cost { color: #3fb950; }
  #summary .stat-val.denied { color: #f85149; }

  /* Events */
  .ev { margin: 0 0 8px; border-left: 3px solid #30363d; padding: 6px 0 6px 12px; }
  .ev .tag { display: inline-block; font-size: 10px; letter-spacing: .07em;
             text-transform: uppercase; color: #6e7681; }
  .ev pre { margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; }

  .task   { border-left-color: #58a6ff; } .task .tag { color: #58a6ff; }
  .answer { border-left-color: #3fb950; } .answer .tag { color: #3fb950; }
  .answer pre { color: #56d364; }
  .note   { border-left-color: #6e7681; color: #9aa4af; font-style: italic; }
  .code   { border-left-color: #d29922; } .code .tag { color: #e3b341; } .code pre { color: #e3b341; }
  .output { border-left-color: #30363d; } .output pre { color: #9aa4af; }
  .output.is-error { border-left-color: #f85149; } .output.is-error .tag { color: #f85149; } .output.is-error pre { color: #ffa198; }
  .error { border-left-color: #f85149; background: #1a0a0a; } .error .tag { color: #f85149; } .error pre { color: #ffa198; }
  .budget { border-left-color: #3fb950; color: #8b949e; }

  /* Audit call events */
  .call   { border-left-color: #8957e5; } .call .tag { color: #a371f7; }
  .call.deny { border-left-color: #f85149; } .call.deny .tag { color: #f85149; }
  .call .args { color: #8b949e; font-size: 11px; margin-top: 3px; }

  /* Streaming in-progress */
  .llm-streaming { border-left-color: #8957e5; padding: 8px 0 8px 12px; opacity: 0.8; }
  .llm-streaming .tag { color: #a371f7; }
  .llm-streaming .stream-text { color: #d7dde5; white-space: pre-wrap; word-break: break-word; font-size: 12px; margin-top: 6px; }
  .llm-streaming .pulse { display: inline-block; animation: pulse 1s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }

  /* LLM call events */
  .llm-call { border-left-color: #8957e5; padding: 8px 0 8px 12px; }
  .llm-call .ev-header { display: flex; align-items: baseline; gap: 10px; }
  .llm-call .tag { color: #a371f7; }
  .llm-call .ev-meta { color: #8b949e; font-size: 11px; }
  .llm-call .ev-meta .cost { color: #3fb950; }
  .llm-call .ev-meta .latency { color: #6e7681; }
  .llm-call .ev-meta .model { color: #79c0ff; }

  /* Collapsible prompt */
  .prompt-wrap { margin-top: 6px; }
  .prompt-wrap summary { cursor: pointer; color: #6e7681; font-size: 11px; padding: 2px 0;
                         list-style: none; display: flex; align-items: center; gap: 4px; }
  .prompt-wrap summary::-webkit-details-marker { display: none; }
  .prompt-wrap summary::before { content: "▶"; font-size: 9px; transition: transform .15s; }
  .prompt-wrap[open] summary::before { transform: rotate(90deg); }
  .prompt-wrap summary:hover { color: #8b949e; }
  .prompt-body { margin-top: 8px; border: 1px solid #21262d; border-radius: 4px; overflow: hidden; }
  .prompt-msg { border-bottom: 1px solid #21262d; padding: 8px 10px; }
  .prompt-msg:last-child { border-bottom: none; }
  .prompt-msg.system { background: #12171f; }
  .prompt-msg.user { background: #0e1116; }
  .prompt-msg.assistant { background: #111820; }
  .msg-role { font-size: 10px; letter-spacing: .08em; text-transform: uppercase; margin-bottom: 4px; display: block; }
  .prompt-msg.system .msg-role { color: #8957e5; }
  .prompt-msg.user .msg-role { color: #58a6ff; }
  .prompt-msg.assistant .msg-role { color: #3fb950; }
  .prompt-msg pre { margin: 0; color: #9aa4af; font-size: 11px; white-space: pre-wrap; word-break: break-word; }
  .prompt-block { margin: 4px 0; }
  .prompt-block.tool-use { background: #161b22; border-radius: 3px; padding: 5px 8px; }
  .prompt-block.tool-result { background: #0d1117; border-radius: 3px; padding: 5px 8px; color: #6e7681; }
  .prompt-block.thinking { background: #1a1a2e; border-radius: 3px; }
  .prompt-block.thinking summary { color: #6e40c9; font-size: 11px; padding: 4px 8px; }
  .tool-use-name { font-size: 10px; text-transform: uppercase; letter-spacing: .06em; color: #e3b341; display: block; margin-bottom: 3px; }

  /* LLM response section */
  .llm-response { margin-top: 8px; }
  .llm-response .resp-label { font-size: 10px; text-transform: uppercase; letter-spacing: .07em; color: #6e7681; margin-bottom: 4px; }
  .llm-response .resp-text { color: #d7dde5; white-space: pre-wrap; word-break: break-word; font-size: 12px; }
  .llm-tc { margin-top: 6px; background: #161b22; border-radius: 4px; padding: 6px 10px; }
  .llm-tc-name { font-size: 10px; text-transform: uppercase; letter-spacing: .06em; color: #e3b341; display: block; margin-bottom: 4px; }
  .llm-tc pre { margin: 0; color: #e3b341; font-size: 11px; }

  #empty { color: #6e7681; margin-top: 60px; text-align: center; font-size: 14px; }
</style>
</head>
<body>
  <div id="sidebar">
    <h1>Sessions</h1>
    <div id="sessions"></div>
  </div>
  <div id="main">
    <div id="summary"></div>
    <div id="timeline"></div>
  </div>
<script>
let active = null;
const el = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt$ = (v) => v != null ? `$${Number(v).toFixed(5)}` : "";
const fmtMs = (s) => s != null ? `${Math.round(s * 1000)}ms` : "";

async function loadSessions() {
  const sessions = await (await fetch("/api/sessions")).json();
  sessions.sort((a, b) => b.mtime - a.mtime);
  if (active === null && sessions.length) active = sessions[0].name;
  el("#sessions").innerHTML = sessions.map(s => {
    const when = new Date(s.mtime * 1000).toLocaleString();
    const costStr = s.cost ? ` · ${fmt$(s.cost)}` : "";
    const callsStr = s.llm_calls ? ` · ${s.llm_calls} calls` : "";
    return `<div class="session ${s.name === active ? 'active' : ''}" data-name="${esc(s.name)}">
      <div class="s-name">${esc(s.name)}</div>
      ${s.task ? `<div class="s-task">${esc(s.task)}</div>` : ""}
      <div class="s-meta">${when}<span>${callsStr}${costStr}</span></div>
    </div>`;
  }).join("") || '<div class="session" style="color:#484f58">no sessions yet</div>';
  document.querySelectorAll(".session[data-name]").forEach(d =>
    d.onclick = () => { active = d.dataset.name; loadTimeline(); loadSessions(); });
}

function renderPromptMsg(m) {
  const role = m.role || "unknown";
  let body = "";
  if (m.text != null) {
    body = `<pre>${esc(m.text)}</pre>`;
  } else if (Array.isArray(m.content)) {
    for (const block of m.content) {
      if (!block) continue;
      if (block.type === "text") {
        body += `<pre>${esc(block.text || "")}</pre>`;
      } else if (block.type === "tool_use") {
        const code = block.input && block.input.code;
        body += `<div class="prompt-block tool-use">
          <span class="tool-use-name">${esc(block.name || "tool_use")}</span>
          ${code ? `<pre>${esc(code)}</pre>` : ""}
        </div>`;
      } else if (block.type === "tool_result") {
        const content = typeof block.content === "string" ? block.content : JSON.stringify(block.content);
        body += `<div class="prompt-block tool-result"><pre>${esc(content.slice(0, 500))}${content.length > 500 ? "\\n…" : ""}</pre></div>`;
      } else if (block.type === "thinking") {
        body += `<details class="prompt-block thinking"><summary>thinking</summary><pre>${esc((block.thinking || "").slice(0, 500))}</pre></details>`;
      } else {
        body += `<pre>${esc(JSON.stringify(block))}</pre>`;
      }
    }
  } else {
    body = `<pre>${esc(String(m.content || ""))}</pre>`;
  }
  return `<div class="prompt-msg ${esc(role)}"><span class="msg-role">${esc(role)}</span>${body}</div>`;
}

function renderEvent(e) {
  const ts = e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : "";
  const tsSpan = ts ? `<span style="color:#484f58;font-size:10px;float:right">${ts}</span>` : "";

  if (e.source === "audit") {
    const deny = e.decision === "deny" || e.ok === false;
    const detail = e.error ? esc(e.error) : esc(e.args || "");
    const dec = e.decision ? ` [${esc(e.decision)}]` : "";
    return `<div class="ev call ${deny ? 'deny' : ''}">
      ${tsSpan}<span class="tag">${esc(e.action || 'call')}${dec}</span>
      <div class="args">${detail}</div>
    </div>`;
  }

  if (e.kind === "llm_partial") {
    return `<div class="ev llm-streaming">
      ${tsSpan}<span class="tag">streaming <span class="pulse">▌</span></span>
      ${e.text ? `<div class="stream-text">${esc(e.text)}</div>` : ""}
    </div>`;
  }

  if (e.kind === "llm_call") {
    const model = e.model || e.tier || "";
    const msgCount = (e.messages || []).length;
    const sysLen = e.system ? e.system.length : 0;

    // Build collapsible prompt
    let promptHtml = "";
    if (e.system) {
      promptHtml += `<div class="prompt-msg system"><span class="msg-role">system</span><pre>${esc(e.system.slice(0, 2000))}${e.system.length > 2000 ? "\\n…" : ""}</pre></div>`;
    }
    for (const m of (e.messages || [])) {
      promptHtml += renderPromptMsg(m);
    }

    // Build response
    let responseHtml = "";
    if (e.text) {
      responseHtml += `<div class="resp-label">response</div><div class="resp-text">${esc(e.text)}</div>`;
    }
    for (const tc of (e.tool_calls || [])) {
      const code = tc.input && tc.input.code;
      responseHtml += `<div class="llm-tc">
        <span class="llm-tc-name">${esc(tc.name)}</span>
        ${code ? `<pre>${esc(code)}</pre>` : ""}
      </div>`;
    }

    return `<div class="ev llm-call">
      <div class="ev-header">
        ${tsSpan}
        <span class="tag">llm</span>
        <span class="ev-meta">
          <span class="model">${esc(model)}</span>
          ${e.cost_usd != null ? ` · <span class="cost">${fmt$(e.cost_usd)}</span>` : ""}
          ${e.latency_s != null ? ` · <span class="latency">${fmtMs(e.latency_s)}</span>` : ""}
        </span>
      </div>
      <details class="prompt-wrap" data-ts="${e.ts || ""}">
        <summary>${msgCount} message${msgCount !== 1 ? "s" : ""} in context · ${sysLen} char system prompt</summary>
        <div class="prompt-body">${promptHtml}</div>
      </details>
      ${responseHtml ? `<div class="llm-response">${responseHtml}</div>` : ""}
    </div>`;
  }

  if (e.kind === "budget") {
    const models = Object.entries(e.by_model || {}).map(([m, c]) => `${esc(m)} $${Number(c).toFixed(5)}`).join(", ");
    return `<div class="ev budget">${tsSpan}<span class="tag">budget</span>
      <pre>spent $${(e.spent_usd || 0).toFixed(5)} over ${e.calls || 0} LLM calls${models ? " — " + models : ""}</pre></div>`;
  }

  if (e.kind === "output") {
    const text = e.text || "";
    const isError = text.includes("Traceback") || /^\\w*Error:/m.test(text);
    return `<div class="ev output ${isError ? 'is-error' : ''}">
      ${tsSpan}<span class="tag">${isError ? "error" : "output"}</span>
      <pre>${esc(text)}</pre></div>`;
  }

  return `<div class="ev ${esc(e.kind || 'unknown')}">
    ${tsSpan}<span class="tag">${esc(e.kind)}</span>
    <pre>${esc(e.text)}</pre></div>`;
}

async function loadTimeline() {
  if (active === null) { el("#timeline").innerHTML = '<div id="empty">select a session</div>'; return; }
  const data = await (await fetch("/api/session?name=" + encodeURIComponent(active))).json();
  const s = data.summary;

  const stats = [
    {label: "tasks", val: s.tasks, cls: ""},
    {label: "llm calls", val: s.llm_calls, cls: ""},
    {label: "total cost", val: fmt$(s.total_cost), cls: "cost"},
    {label: "avg latency", val: fmtMs(s.avg_latency_s), cls: ""},
    {label: "cap calls", val: s.calls, cls: ""},
    ...(s.denied ? [{label: "denied", val: s.denied, cls: "denied"}] : []),
  ];
  el("#summary").innerHTML = stats.map(st =>
    `<div class="stat"><span class="stat-label">${st.label}</span><span class="stat-val ${st.cls}">${esc(String(st.val))}</span></div>`
  ).join("");

  // Preserve open state of prompt details across re-renders
  const openTs = new Set(
    [...document.querySelectorAll(".prompt-wrap[open]")].map(d => d.dataset.ts).filter(Boolean)
  );

  el("#timeline").innerHTML = data.events.length
    ? data.events.map(renderEvent).join("")
    : '<div id="empty">no events yet</div>';

  // Restore open state
  if (openTs.size) {
    document.querySelectorAll(".prompt-wrap[data-ts]").forEach(d => {
      if (openTs.has(d.dataset.ts)) d.open = true;
    });
  }
}

async function tick() { await loadSessions(); await loadTimeline(); }
tick();
setInterval(tick, 1500);
</script>
</body>
</html>
"""
