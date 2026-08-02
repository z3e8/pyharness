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
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from .page import NavItem, head, rail, render_doc_page, scripts
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


def _static_feed(
    events: list[dict],
    started: float,
    ended: float,
    *,
    siblings: list[dict] | None = None,
    nav: list[NavItem] | None = None,
    current: str = "",
) -> str:
    """The baked feed: replay, then freeze. Everything after the loop exists
    because this page is a record, not a live view — the ticking clock, the
    spinner on an in-flight action, and the "connecting…" status are all
    statements about *now* that would be false on an archived page.

    `siblings` is the switcher's list, baked in as links. The live viewer polls
    a server for the same thing; a page opened from a file has no server, so the
    list travels with it and the sidebar works identically either way."""
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
    listing = json.dumps(siblings or [], default=str).replace("<", "\\u003c")
    navitems = json.dumps(
        [{"label": lbl, "href": href, "icon": icon} for lbl, href, icon in nav or []]
    ).replace("<", "\\u003c")
    return f"""
window.SESSIONS = JSON.parse({json.dumps(listing)});
currentSession = {json.dumps(current)};
renderSessionList();
renderNav(JSON.parse({json.dumps(navitems)}));

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
document.getElementById('stat-clock').textContent =
  Math.floor(secs / 60) + 'm' + String(Math.floor(secs % 60)).padStart(2, '0') + 's';
setStatus('archived · {when}', '');
// Live-only controls. "Follow" has no stream to follow and "jump to latest" has
// no latest — on a record they are buttons that promise something untrue.
document.querySelectorAll('.rail-label .follow, #jump').forEach((n) => n.remove());
// A live session follows the tail; a record opens at the top, where the task is.
scrollTo(0, 0);
"""


def build_page(
    session_dir: str | Path,
    *,
    title: str | None = None,
    siblings: list[dict] | None = None,
    nav: list[NavItem] | None = None,
) -> str:
    """One finished session as a single self-contained HTML page."""
    session_dir = Path(session_dir)
    events = session_events(session_dir)
    started, ended = _span(events)
    return render_page(
        _static_feed(
            events,
            started,
            ended,
            siblings=siblings,
            nav=nav,
            current=session_dir.name,
        ),
        title=title or f"pyharness — {session_dir.name}",
    )


def _fmt_usd(x: float) -> str:
    return f"${x:.4f}" if 0 < x < 0.01 else f"${x:.2f}"


def _sidebar(digests: list[dict]) -> list[dict]:
    """The switcher entries baked into every page.

    Summary fields only, and never the digest's absolute `session`/`trace`/
    `audit`/`workspace` paths — this list is copied into all of them."""
    return [
        {
            "name": d["name"],
            "href": f"{d['name']}.html",
            "outcome": d["outcome"],
            "steps": d["steps"],
            "cost_usd": d["cost_usd"],
        }
        for d in digests
    ]


def build_index(
    digests: list[dict],
    *,
    title: str,
    lede: str = "",
    nav: list[NavItem] | None = None,
) -> str:
    """The landing page: one card per session, linking its baked page, over a
    row of the numbers the suite is actually claiming.

    Deliberately built from `session_digest`'s *summary* fields only. The digest
    also carries absolute `session`/`trace`/`audit`/`workspace` paths, which are
    useful locally and must never reach a published page."""
    cards = []
    for d in digests:
        outcome = d["outcome"]
        cls = "ok" if outcome == "answered" else "warn"
        # The outcome word rides in the second row, quiet, because nine rows
        # saying "answered" is not information — the dot carries the state at a
        # glance and the exception is the only one that needs to shout. A
        # refusal count does, so it keeps its own pill whenever it is non-zero.
        refused = (
            f'<span class="pill warn">{d["denials"]} refused</span>'
            if d["denials"]
            else ""
        )
        cards.append(
            f'<a class="card" href="{escape(d["name"])}.html">'
            f'<span class="name"><i class="dot {cls}"></i>{escape(d["name"])}</span>'
            f'<span class="nums">{_fmt_usd(d["cost_usd"])}</span>'
            f'<span class="blurb">{escape((d.get("task") or "")[:220])}</span>'
            f'<span class="tail">{refused}'
            f'<span class="pill {cls}">{escape(outcome)}</span></span>'
            "</a>"
        )
    total = sum(d["cost_usd"] for d in digests)
    denials = sum(d["denials"] for d in digests)
    # No switcher in the rail here: this page *is* the list of sessions, and
    # printing it a second time down the side is duplication, not navigation.
    return f"""{head(escape(title))}
<div class="shell">
{rail(nav=nav, current="index.html", aside_label=None)}
<div class="main">
  <div class="page">
    <header class="hero">
      <div class="eyebrow">Session archive</div>
      <h1>{escape(title)}</h1>
      <p class="lede">{
        escape(lede)
        or '''Each page below is the live viewer's own
      renderer replaying that session's recorded trace — the same view the
      operator saw, frozen.'''
    }</p>
    </header>
    <div class="stats">
      <div class="stat"><div class="v">{
        len(digests)
    }</div><div class="k">sessions</div></div>
      <div class="stat flag"><div class="v">{
        denials
    }</div><div class="k">refused by policy</div></div>
      <div class="stat"><div class="v">{
        _fmt_usd(total)
    }</div><div class="k">total cost</div></div>
    </div>
    <div class="cards">
{chr(10).join(cards)}
    </div>
    <footer class="site">Generated by <code>pyharness-watch --static</code>.</footer>
  </div>
</div>
</div>
{scripts("")}
"""


def build_site(
    sessions: Sequence[str | Path],
    out_dir: str | Path,
    *,
    title: str = "pyharness sessions",
    lede: str = "",
    docs: Sequence[tuple[str, str | Path]] | None = None,
) -> list[Path]:
    """Write one page per session, any markdown `docs` as pages beside them, and
    an `index.html`, and return what was written. Sessions keep their directory
    names, so the index can link them without a manifest.

    `docs` is a list of `(label, path)` — the eval boards. They are part of the
    site rather than links out to a repo because the claim and the evidence for
    it should be one artifact you can open offline."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    doc_pages = [(label, Path(p), _slug(label) + ".html") for label, p in docs or []]
    nav: list[NavItem] = [("Overview", "index.html", "◆")]
    nav += [(label, name, "▤") for label, _, name in doc_pages]

    digests = [session_digest(Path(s)) for s in sessions]
    sidebar = _sidebar(digests)
    for session in sessions:
        session = Path(session)
        page = out_dir / f"{session.name}.html"
        page.write_text(
            build_page(session, siblings=sidebar, nav=nav), encoding="utf-8"
        )
        written.append(page)

    for label, src, name in doc_pages:
        page = out_dir / name
        page.write_text(
            render_doc_page(
                src.read_text(encoding="utf-8"), title=label, nav=nav, current=name
            ),
            encoding="utf-8",
        )
        written.append(page)

    index = out_dir / "index.html"
    index.write_text(
        build_index(digests, title=title, lede=lede, nav=nav), encoding="utf-8"
    )
    written.append(index)
    return written


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "doc"


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
