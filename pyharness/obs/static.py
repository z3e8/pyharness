"""Bake a finished session into a self-contained HTML page.

The live viewer (`watch.py`) and this share one renderer: `render_page` takes a
feed, and the only difference between the two views is where events come from —
an SSE stream there, a baked array here, both replayed through the same
`handle()`. That is the whole point of the split. A viewer whose static export
was a separate implementation would drift from the live one the first time
either changed, and the published page is the one nobody re-runs to check.

What the bake has to do that the live view does not:

- **Inline the media.** The live page fetches `/media/<session>/<file>` from the
  server that is tailing the session. A file:// or GitHub Pages copy has no such
  server, so screenshots become data: URIs.
- **Redact absolute paths.** A trace records the session root, and the model's
  preamble names the workspace — both absolute, both carrying the operator's home
  directory. Publishing them leaks a username and a machine layout to anyone who
  reads the page.
- **Stop the clock.** The page ticks elapsed time off `Date.now()`. Replayed, every
  event lands in the same millisecond, so a live clock would count up from the
  moment the page opened and claim a five-second session had been running for
  hours. The bake freezes it at the real duration read from the trace.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from .transcript import session_digest
from .watch import Tail, render_page

_MEDIA_PREFIX = "/media/"


def _redaction_map(session_dir: Path) -> dict[str, str]:
    """Absolute prefixes to strip from a page, longest first.

    The session root is replaced with a stable placeholder rather than deleted so
    a reader can still see *that* paths were involved and how they nested; the
    home directory falls back to `~` for anything the root doesn't cover (the
    preamble's workspace line, a traceback from an installed package)."""
    return {
        str(session_dir.resolve()): "<session>",
        str(Path.home()): "~",
    }


def _redact(text: str, mapping: dict[str, str]) -> str:
    for needle, replacement in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(needle, replacement)
    return text


def _inline_media(container: Path, src: str) -> str:
    """A `/media/<session>/<file>` url as a data: URI. An unreadable or missing
    file yields an empty src rather than raising — one lost screenshot must not
    cost the whole page, and the alt text still says an image was there."""
    if not src.startswith(_MEDIA_PREFIX):
        return src
    parts = src[len(_MEDIA_PREFIX) :].split("/")
    if len(parts) != 2 or not all(p and p not in ("..", ".") for p in parts):
        return ""
    path = (container / parts[0] / "media" / parts[1]).resolve()
    if not path.is_relative_to(container.resolve()) or not path.is_file():
        return ""
    mime = mimetypes.guess_type(parts[1])[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def session_events(session_dir: str | Path) -> list[dict]:
    """Every trace entry for a session *tree* (the root plus its spawn children),
    in the order the live viewer would have received them, with media inlined and
    absolute paths redacted.

    Reuses `Tail` rather than re-reading the files: the ordering rules (root
    before children, the synthetic `watch_session`, partial trailing lines held
    back) are the live viewer's, and duplicating them here is how the two views
    would start disagreeing about what a session contained."""
    session_dir = Path(session_dir)
    events = Tail(session_dir).poll()
    container = session_dir.parent
    mapping = _redaction_map(session_dir)
    out = []
    for event in events:
        if event.get("kind") == "media":
            event = {**event, "src": _inline_media(container, event.get("src", ""))}
            out.append(event)
            continue
        # Redact over the serialized form so every string field is covered at
        # once, however deeply nested (prompt snapshots carry whole message
        # histories, and the workspace path shows up inside them).
        out.append(json.loads(_redact(json.dumps(event, default=str), mapping)))
    return out


def _span(events: list[dict]) -> tuple[float, float]:
    stamps = [e["ts"] for e in events if isinstance(e.get("ts"), (int, float))]
    return (min(stamps), max(stamps)) if stamps else (0.0, 0.0)


def _static_feed(events: list[dict], started: float, ended: float) -> str:
    """The baked feed: replay, then freeze. Everything after the loop exists
    because this page is a record, not a live view — the ticking clock, the
    spinner on an in-flight action, and the "connecting…" status are all
    statements about *now* that would be false on an archived page."""
    payload = json.dumps(events, default=str, separators=(",", ":"))
    # `</script>` inside trace text would end the block early; `<` is the
    # same string to JSON.parse and inert to the HTML parser.
    payload = payload.replace("<", "\\u003c")
    when = (
        datetime.fromtimestamp(ended, UTC).strftime("%Y-%m-%d %H:%M UTC")
        if ended
        else "unknown"
    )
    duration = max(0.0, ended - started)
    return f"""
const EVENTS = JSON.parse({json.dumps(payload)});
for (const e of EVENTS) {{
  try {{ handle(e); }} catch (err) {{ console.error('replay', err, e); }}
}}
clearInterval(ticker);
followTail = false;
// Anything still in `active` was in flight when the record ends — a budget or
// step wall lands mid-action. Say so instead of leaving a spinner that will
// never resolve.
for (const item of [...nowEl.children, ...bannerEl.children]) {{
  item.querySelector('.elapsed').textContent = 'unfinished at end of record';
}}
const secs = {duration:.1f};
document.getElementById('clock').textContent =
  Math.floor(secs / 60) + 'm' + String(Math.floor(secs % 60)).padStart(2, '0') + 's';
document.getElementById('status').textContent = 'archived · {when}';
scrollTo(0, 0);
"""


def build_page(session_dir: str | Path, *, title: str | None = None) -> str:
    """One finished session as a single self-contained HTML page."""
    session_dir = Path(session_dir)
    events = session_events(session_dir)
    started, ended = _span(events)
    return render_page(
        _static_feed(events, started, ended),
        title=title or f"pyharness — {session_dir.name}",
    )


def _fmt_usd(x: float) -> str:
    return f"${x:.4f}" if 0 < x < 0.01 else f"${x:.2f}"


def build_index(digests: list[dict], *, title: str) -> str:
    """The landing page: one row per session, linking its baked page.

    Deliberately built from `session_digest`'s *summary* fields only. The digest
    also carries absolute `session`/`trace`/`audit`/`workspace` paths, which are
    useful locally and must never reach a published page."""
    rows = []
    for d in digests:
        outcome = d["outcome"]
        cls = "ok" if outcome == "answered" else "warn"
        rows.append(
            "<tr>"
            f'<td><a href="{escape(d["name"])}.html">{escape(d["name"])}</a></td>'
            f'<td><span class="pill {cls}">{escape(outcome)}</span></td>'
            f'<td class="num">{d["steps"]}</td>'
            f'<td class="num">{d["actions"]}</td>'
            f'<td class="num">{d["denials"]}</td>'
            f'<td class="num">{_fmt_usd(d["cost_usd"])}</td>'
            f'<td class="task">{escape((d.get("task") or "")[:180])}</td>'
            "</tr>"
        )
    total = sum(d["cost_usd"] for d in digests)
    denials = sum(d["denials"] for d in digests)
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ background: #0f1115; color: #d8dee9; margin: 0;
         font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }}
  main {{ max-width: 1100px; margin: 0 auto; padding: 32px 20px 64px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: #8b93a7; margin: 0 0 24px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #242833;
           vertical-align: top; }}
  th {{ color: #8b93a7; font-weight: normal; font-size: 12px;
       text-transform: uppercase; letter-spacing: .06em; }}
  td.num {{ text-align: right; white-space: nowrap; }}
  td.task {{ color: #8b93a7; }}
  a {{ color: #7fb3ff; }}
  .pill {{ border-radius: 3px; padding: 1px 6px; font-size: 12px; }}
  .pill.ok {{ background: #16301f; color: #7ee2a8; }}
  .pill.warn {{ background: #3a2a16; color: #f0c274; }}
  footer {{ color: #8b93a7; margin-top: 28px; font-size: 12px; }}
</style>
</head>
<body>
<main>
<h1>{escape(title)}</h1>
<p class="sub">{len(digests)} sessions · {denials} actions refused by policy ·
{_fmt_usd(total)} total. Each page is the live viewer's own renderer replaying
that session's recorded trace — the same view the operator saw, frozen.</p>
<table>
<thead><tr><th>session</th><th>outcome</th><th>steps</th><th>actions</th>
<th>refused</th><th>cost</th><th>task</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
<footer>Generated by <code>pyharness-watch --static</code>.</footer>
</main>
</body>
</html>
"""


def build_site(
    sessions: list[str | Path],
    out_dir: str | Path,
    *,
    title: str = "pyharness sessions",
) -> list[Path]:
    """Write one page per session plus an `index.html`, and return what was
    written. Sessions keep their directory names, so the index can link them
    without a manifest."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    digests = []
    for session in sessions:
        session = Path(session)
        page = out_dir / f"{session.name}.html"
        page.write_text(build_page(session), encoding="utf-8")
        written.append(page)
        digests.append(session_digest(session))
    index = out_dir / "index.html"
    index.write_text(build_index(digests, title=title), encoding="utf-8")
    written.append(index)
    return written


def discover_sessions(target: str | Path) -> list[Path]:
    """Session dirs under `target`, newest last. `target` itself counts when it
    holds a trace.jsonl. Spawn children are excluded: they are rendered inside
    their parent's page, so a page of their own would double-publish them."""
    target = Path(target)
    if (target / "trace.jsonl").exists():
        return [target]
    from .watch import _SPAWN_RE

    dirs = [
        p.parent
        for p in target.glob("*/trace.jsonl")
        if p.is_file() and not _SPAWN_RE.search(p.parent.name)
    ]
    return sorted(dirs, key=lambda d: (d / "trace.jsonl").stat().st_mtime)
