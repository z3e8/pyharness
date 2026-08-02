"""The HTML shell every page in this project shares.

There are four surfaces — the live viewer, a baked session page, the site
index, and an eval board — and exactly one shell, one stylesheet, and one
markdown renderer behind them. The alternative is four looks that drift, and
three of them are artifacts nobody re-renders to check.

Assets are real files under `assets/` rather than string literals in Python:
they get syntax highlighting, they diff readably, and hatchling ships them with
the package. They are inlined at render time because a published page has to
work from `file://` with no server and no network.
"""

from __future__ import annotations

import json
from functools import lru_cache
from html import escape
from pathlib import Path

_ASSETS = Path(__file__).parent / "assets"

# Nav entry: (label, href, icon). `href` is also the identity used to mark the
# current page, so it must match what the caller links to.
NavItem = tuple[str, str, str]


@lru_cache(maxsize=8)
def asset(name: str) -> str:
    """One bundled asset's text, read once per process."""
    return (_ASSETS / name).read_text(encoding="utf-8")


# Runs before first paint so a light-mode reader never sees a dark flash. Kept
# in the head, and deliberately tiny — the rest of the theme logic is in
# viewer.js, which loads at the end of the body.
_THEME_BOOT = """
try {
  var t = localStorage.getItem('ph-theme');
  if (t) document.documentElement.setAttribute('data-theme', t);
} catch (e) {}
"""


def head(title: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script>{_THEME_BOOT}</script>
<style>
{asset("viewer.css")}
</style>
</head>
<body>"""


def _nav(items: list[NavItem], current: str | None) -> str:
    if not items:
        return ""
    links = "\n".join(
        f'<a class="{"on" if href == current else ""}" href="{escape(href)}">'
        f'<span class="ico">{escape(icon)}</span>{escape(label)}</a>'
        for label, href, icon in items
    )
    return f'<nav class="rail-nav">\n{links}\n</nav>'


def rail(
    *,
    nav: list[NavItem] | None = None,
    current: str | None = None,
    aside_label: str = "Sessions",
    aside_id: str = "sessions",
    follow_toggle: bool = False,
) -> str:
    """The left rail: wordmark, theme toggle, site nav, and a switcher list.

    `aside_id` is `sessions` on the pages that switch between sessions and
    `toc` on a board, so one rail serves both without a second layout."""
    follow = (
        '<label class="follow" title="jump to a newly started session">'
        '<input type="checkbox" id="follow" checked> follow</label>'
        if follow_toggle
        else ""
    )
    return f"""<aside id="rail">
  <div class="rail-head">
    <span class="mark"></span>
    <span class="wordmark">pyharness</span>
    <button class="theme" id="theme" aria-label="Toggle theme"></button>
  </div>
  {_nav(nav or [], current)}
  <div class="rail-label">{escape(aside_label)}{follow}</div>
  <div id="{aside_id}"></div>
</aside>"""


def scripts(feed_js: str) -> str:
    """The shared renderer, then whatever feeds it."""
    return f"""<script>
{asset("markdown.js")}
</script>
<script>
{asset("viewer.js")}
</script>
<script>
{feed_js}
</script>
</body>
</html>"""


# --- the session viewer -------------------------------------------------------

FEED_SLOT = "/* @@FEED@@ */"
TITLE_SLOT = "@@TITLE@@"


def session_template() -> str:
    """The session page with its two slots unfilled. `watch.render_page` fills
    them; the live page and every baked page are otherwise byte-identical."""
    return f"""{head(TITLE_SLOT)}
<div class="shell">
{rail(follow_toggle=True)}
<div class="main">
  <div id="topbar">
    <div class="tb-row">
      <span class="tb-title" id="session-title">waiting for a session…</span>
      <span class="tb-stats">
        <span><b id="stat-steps">0</b> steps</span>
        <span class="spend"><b id="stat-cost">$0.00</b></span>
        <span id="stat-clock">0m00s</span>
        <span class="dotstat" id="status">connecting…</span>
      </span>
    </div>
    <div class="tb-tools">
      <label class="search">
        <span>⌕</span>
        <input type="search" id="search" placeholder="Filter this session…  ( / )">
        <span class="count" id="searchcount"></span>
      </label>
      <div class="menu">
        <button class="btn" id="viewmenu">View ▾</button>
        <div class="menu-pop" id="viewpop" hidden>
          <label><input type="checkbox" data-f="code" checked> Code</label>
          <label><input type="checkbox" data-f="output" checked> Output</label>
          <label><input type="checkbox" data-f="action" checked> Actions</label>
          <label><input type="checkbox" data-f="prompt" checked> Prompts</label>
          <label><input type="checkbox" data-f="think" checked> Thinking</label>
        </div>
      </div>
    </div>
    <div id="banner"></div>
    <div id="now"></div>
  </div>
  <main id="log"></main>
</div>
</div>
<button id="jump" hidden>Jump to latest ↓</button>
{scripts(FEED_SLOT)}"""


# --- markdown boards ----------------------------------------------------------

_DOC_FEED = """
var __doc = document.getElementById('doc-body');
__doc.appendChild(renderMarkdown(%s));
// A board opens with its own `# Title`, and the shell already printed one. Let
// the document's title win — it is the more specific of the two — rather than
// stacking two headings that say almost the same thing.
(function () {
  var first = __doc.querySelector('.md > h1');
  if (!first) return;
  document.querySelector('.page-head h1').textContent = first.textContent;
  first.remove();
})();
// The rail's switcher slot becomes a table of contents on a board: same
// layout, and a 350-line scoreboard stops being a single scroll.
(function () {
  var toc = document.getElementById('toc');
  if (!toc) return;
  // Sections always; subsections only while they are still navigation. The
  // scoreboard has one h3 per attack — forty of them is a second document, not
  // a contents list.
  var deep = __doc.querySelectorAll('.md > h3').length < 20;
  var frag = document.createDocumentFragment();
  var n = 0;
  __doc.querySelectorAll(deep ? '.md > h2, .md > h3' : '.md > h2').forEach(function (h) {
    var id = 'h' + (++n);
    h.id = id;
    var a = el('a', 'sitem');
    a.href = '#' + id;
    a.style.paddingLeft = h.tagName === 'H3' ? '22px' : '10px';
    a.appendChild(el('span', 'n', h.textContent));
    frag.appendChild(a);
  });
  toc.replaceChildren(frag);
})();
"""


def render_doc_page(
    markdown: str,
    *,
    title: str,
    nav: list[NavItem] | None = None,
    current: str | None = None,
    lede: str = "",
) -> str:
    """A markdown document as a page in the site — rendered by the same
    `renderMarkdown` the transcripts use, so a table in an eval board and a
    table in a tool's output look like the same product."""
    # `</script>` inside the source would end the block early and hand the rest
    # of the document to the HTML parser as markup; `<` is the same string
    # to JSON.parse and inert to the parser. Same trick the baked feed uses.
    payload = json.dumps(markdown).replace("<", "\\u003c")
    return f"""{head(escape(title))}
<div class="shell">
{rail(nav=nav, current=current, aside_label="On this page", aside_id="toc")}
<div class="main">
  <div class="page">
    <div class="page-head">
      <h1>{escape(title)}</h1>
      {f'<p class="lede">{escape(lede)}</p>' if lede else ""}
    </div>
    <div class="doc-body md" id="doc-body"></div>
    <footer class="site">Rendered from the committed markdown artifact by
    <code>pyharness-watch --static</code>.</footer>
  </div>
</div>
</div>
{scripts(_DOC_FEED % payload)}"""
