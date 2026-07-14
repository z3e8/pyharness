"""Turn an HTML body into something an agent can actually navigate.

Three layers, each degrading independently so a fetch never fails on bad markup:

- `html_to_text` — the stdlib fallback reducer (visible text only). Used when the
  richer content pass declines.
- `extract_content` — trafilatura reduces the body to clean readable markdown.
  Extraction *only*: trafilatura's own fetching/network helpers are never touched,
  so this stays inside the broker/audit/redaction boundary like every other side
  effect.
- `parse_affordances` — an lxml pass that hands back the page title plus the two
  things the old text-only reducer stripped and left the agent blind to: the
  links it could navigate and the forms it could fill (with each field's
  name/type/label/required, hidden CSRF values, and select options).

`render_page_map` composes those into one readable markdown document for
`web.fetch`. The structured link/form lists ride back on `http.request`'s result
dict unchanged, so an agent driving HTTP directly parses them without re-reading
the markdown.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from importlib import import_module
from urllib.parse import urldefrag, urljoin

# Tags whose text is markup noise, not content, and block-level tags that mark a
# line boundary — used by the stdlib fallback reducer below.
_HTML_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head"})
_HTML_BLOCK_TAGS = frozenset(
    {"p", "div", "section", "article", "header", "footer", "nav", "main", "aside",
     "li", "ul", "ol", "table", "tr", "br", "hr", "h1", "h2", "h3", "h4", "h5",
     "h6", "blockquote", "pre", "figure", "figcaption"}
)

# Display caps. The full deduped link list rides back on the result dict (big data
# belongs in a kernel variable); only the human-readable page map is capped, to
# keep a link-heavy page from swamping the agent's context.
_MAX_DISPLAY_LINKS = 100
_MAX_SELECT_OPTIONS = 30


class _TextExtractor(HTMLParser):
    """Collect visible text from an HTML document, dropping script/style/head
    noise and inserting newlines at block boundaries so structure survives."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _HTML_SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _HTML_BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in _HTML_SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _HTML_BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        # A newline inside text content is just whitespace in HTML, so collapse
        # every run to a single space here; the only real line breaks are the
        # "\n" sentinels emitted at block boundaries above.
        if self._skip_depth == 0:
            self._chunks.append(re.sub(r"\s+", " ", data))

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_text(html: str) -> str:
    """Reduce an HTML document to readable text: drop script/style/head noise,
    keep block boundaries as newlines, and collapse whitespace. Stdlib only — the
    fallback for when `extract_content` declines. Falls back to the raw input if
    parsing fails."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup must not fail the fetch
        return html
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", parser.text())  # trim spaces around breaks
    text = re.sub(r"\n{3,}", "\n\n", text)  # squeeze blank-line runs to one
    return text.strip()


def extract_content(html: str) -> str | None:
    """Reduce an HTML body to clean readable markdown via trafilatura, or None
    when it can't (too little main content, parse failure) — the caller then falls
    back to `html_to_text`.

    `include_links` is deliberately off: it is flagged experimental upstream and
    mangles content when combined with markdown formatting, and the lxml
    `parse_affordances` pass already surfaces every link comprehensively, so the
    reading layer stays clean and the link channel stays reliable. Extraction only
    — trafilatura's fetch/download helpers are never called."""
    try:
        trafilatura = import_module("trafilatura")
        return trafilatura.extract(html, output_format="markdown")
    except Exception:  # noqa: BLE001 - a bad body must fall back, not fail the fetch
        return None


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def parse_affordances(html: str, base_url: str) -> tuple[str | None, list[dict], list[dict]]:
    """Parse the page's title, links, and forms from static HTML with lxml.

    Returns `(title, links, forms)`. Links are deduped by resolved target and
    exclude javascript:/fragment-only anchors; hrefs and form actions are resolved
    to absolute URLs against `<base href>` if present, else `base_url` (the final,
    post-redirect response url). Forms carry each field's name/type/label/required,
    hidden field values (CSRF tokens — page data the agent needs to POST), and
    capped select options. JS-rendered links and forms are not in the static HTML
    and so do not appear — that is the browser capability's job."""
    try:
        lxml_html = import_module("lxml.html")
        doc = lxml_html.fromstring(html)
    except Exception:  # noqa: BLE001 - unparseable markup yields no affordances, not an error
        return None, [], []

    base = base_url
    base_els = doc.xpath("//base[@href]")
    if base_els:
        base = urljoin(base_url, base_els[0].get("href", "").strip())

    title = None
    title_els = doc.xpath("//title")
    if title_els is not None and title_els and title_els[0].text:
        title = _clean(title_els[0].text)

    return title, _extract_links(doc, base), _extract_forms(doc, base)


def _extract_links(doc, base: str) -> list[dict]:
    seen: set[str] = set()
    links: list[dict] = []
    for a in doc.xpath("//a[@href]"):
        href = (a.get("href") or "").strip()
        if not href or href.lower().startswith("javascript:") or href.startswith("#"):
            continue
        target = urldefrag(urljoin(base, href)).url
        if not target or target in seen:
            continue
        seen.add(target)
        links.append({"text": _clean(a.text_content())[:120], "href": target})
    return links


def _extract_forms(doc, base: str) -> list[dict]:
    forms: list[dict] = []
    for form in doc.xpath("//form"):
        action = form.get("action")
        labels = {
            lab.get("for"): _clean(lab.text_content())
            for lab in form.xpath(".//label[@for]")
        }
        fields: list[dict] = []
        submit = None
        for el in form.xpath(".//input | .//textarea | .//select | .//button"):
            itype = _field_type(el)
            if itype in ("submit", "button", "image"):
                submit = _clean(el.get("value") or el.text_content()) or submit
                continue
            name = el.get("name")
            if not name:
                continue
            field: dict = {"name": name, "type": itype}
            label = labels.get(el.get("id"))
            if label:
                field["label"] = label
            if el.get("required") is not None:
                field["required"] = True
            # Surface an explicit value: a hidden CSRF token, a radio/checkbox
            # option value (which choice the field submits), or a prefilled
            # default — all things a form-filler needs and none a secret.
            if el.get("value") is not None:
                field["value"] = el.get("value")
            if el.tag == "select":
                options = [
                    o.get("value") if o.get("value") is not None else _clean(o.text_content())
                    for o in el.xpath(".//option")
                ]
                field["options"] = options[:_MAX_SELECT_OPTIONS]
                if len(options) > _MAX_SELECT_OPTIONS:
                    field["options_truncated"] = len(options) - _MAX_SELECT_OPTIONS
            fields.append(field)
        forms.append(
            {
                "action": urljoin(base, action.strip()) if action else base,
                "method": (form.get("method") or "GET").upper(),
                "enctype": form.get("enctype") or "",
                "fields": fields,
                "submit": submit,
            }
        )
    return forms


def _field_type(el) -> str:
    if el.tag == "select":
        return "select"
    if el.tag == "textarea":
        return "textarea"
    if el.tag == "button":
        return (el.get("type") or "submit").lower()  # a bare <button> submits
    return (el.get("type") or "text").lower()


def render_page_map(
    content: str, links: list[dict], forms: list[dict], title: str | None = None
) -> str:
    """Compose the readable markdown `web.fetch` returns: the page title, the
    clean content, then FORMS and LINKS appendices. Empty sections are omitted, so
    a plain article reads as plain prose and only an interactive page grows the
    extra structure."""
    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
    if content:
        parts.append(content.strip())
    if forms:
        parts.append(_render_forms(forms))
    if links:
        parts.append(_render_links(links))
    return "\n\n".join(p for p in parts if p).strip()


def _render_forms(forms: list[dict]) -> str:
    lines = ["## FORMS"]
    for i, form in enumerate(forms, 1):
        enctype = f" enctype={form['enctype']}" if form["enctype"] else ""
        lines.append(f"### [F{i}] {form['method']} {form['action']}{enctype}")
        for field in form["fields"]:
            lines.append(f"- {_render_field(field)}")
        if form["submit"]:
            lines.append(f"- submit: {form['submit']!r}")
    return "\n".join(lines)


def _render_field(field: dict) -> str:
    parts = [f"{field['name']} ({field['type']})"]
    if field.get("label"):
        parts.append(f"label={field['label']!r}")
    if field.get("required"):
        parts.append("required")
    if "value" in field:
        parts.append(f"value={field['value']!r}")
    if "options" in field:
        shown = ", ".join(map(str, field["options"]))
        more = f" … (+{field['options_truncated']} more)" if field.get("options_truncated") else ""
        parts.append(f"options: {shown}{more}")
    return "  ".join(parts)


def _render_links(links: list[dict]) -> str:
    lines = ["## LINKS"]
    for link in links[:_MAX_DISPLAY_LINKS]:
        text = link["text"] or link["href"]
        lines.append(f"- [{text}]({link['href']})")
    if len(links) > _MAX_DISPLAY_LINKS:
        lines.append(f"_… and {len(links) - _MAX_DISPLAY_LINKS} more links_")
    return "\n".join(lines)
