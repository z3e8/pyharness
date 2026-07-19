"""Exa web search — a raw ranked result list to fan out over, not a digested answer.

Unlike `page.py` (pure stdlib parsing), this module does network I/O: it POSTs to
Exa's `/search` endpoint. It runs **parent-side only**, reached through the broker
boundary like every other side effect; the Exa key is held by the parent and never
crosses into agent code (see `PROVIDER_SECRET_ENV` in `llm/client.py`). Kept thin
so `web.py` stays composition-only.
"""

from __future__ import annotations

from importlib import import_module

_ENDPOINT = "https://api.exa.ai/search"
_MAX_RESULTS = 100  # Exa's own ceiling
_SNIPPET_CAP = 500  # a highlight is a triage excerpt, not the page body


def search(
    query: str, *, api_key: str, num_results: int = 10, type_: str = "auto"
) -> list[dict]:
    """POST one query to Exa and return a small structured list — each item
    `{title, url, snippet, published_date, author, score}`. `snippet` is Exa's
    first LLM-picked highlight (bounded); `score` is the document relevance the
    response carries, or None. `num_results` is clamped to Exa's 1–100 range.

    Requests `contents.highlights` only — not full `text` (that's what `web.fetch`
    /`http` are for) and not `summary` (fetch the ranked URLs to read the body).
    Raises RuntimeError on a non-2xx response, carrying the status and a short body
    snippet only — never the key or request headers. httpx is imported lazily and
    `Client` is constructed here (not `httpx.post`) so tests can monkeypatch it;
    `follow_redirects` stays at its default False so the `x-api-key` header — which
    httpx preserves across redirects — can't be resent to another host."""
    n = max(1, min(num_results, _MAX_RESULTS))
    httpx = import_module("httpx")
    client = httpx.Client(timeout=30)
    try:
        resp = client.request(
            "POST",
            _ENDPOINT,
            headers={"x-api-key": api_key},
            json={
                "query": query,
                "numResults": n,
                "type": type_,
                "contents": {"highlights": True},
            },
        )
    finally:
        client.close()
    if resp.status_code // 100 != 2:
        raise RuntimeError(f"Exa search failed ({resp.status_code}): {resp.text[:200]}")
    return [_result(r) for r in resp.json().get("results", [])]


def _result(r: dict) -> dict:
    highlights = r.get("highlights") or []
    snippet = (highlights[0] if highlights else "")[:_SNIPPET_CAP]
    return {
        "title": r.get("title"),
        "url": r.get("url"),
        "snippet": snippet,
        "published_date": r.get("publishedDate"),
        "author": r.get("author"),
        "score": r.get("score"),
    }
