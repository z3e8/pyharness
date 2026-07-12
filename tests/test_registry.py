"""Tool discovery: ranked, capped, non-resolving search + describe (design §6)."""

from __future__ import annotations

from types import ModuleType

import pytest

from pyharness.tools.registry import Registry


def _tool(name: str, summary: str, *funcs):
    """Build a throwaway tool module exposing the given functions."""
    module = ModuleType(name)
    module.__doc__ = summary
    for func in funcs:
        func.__module__ = name  # so _public_functions discovers it
        setattr(module, func.__name__, func)
    return module


def send(channel: str, text: str):  # noqa: D401
    """Post a message to a channel."""
    return f"{channel}:{text}"


def test_empty_query_lists_only_featured():
    reg = Registry()
    reg.register(_tool("shown", "a common tool", send), source="installed", featured=True)
    reg.register(_tool("hidden", "a long-tail tool", send), source="installed")
    listing = reg.search("")
    assert "# shown" in listing
    assert "# hidden" not in listing  # empty query shows only featured


def test_include_all_surfaces_the_long_tail():
    reg = Registry()
    reg.register(_tool("shown", "a common tool", send), source="installed", featured=True)
    reg.register(_tool("hidden", "a long-tail tool", send), source="installed")
    listing = reg.search("", include_all=True)
    assert "# shown" in listing and "# hidden" in listing


def test_keyword_alias_matches_intent_word():
    reg = Registry()
    reg.register(_tool("slack", "Slack integration", send),
                 source="installed", category="chat", keywords=("message", "im"))
    # "message" appears in neither the name nor the summary — only the keywords.
    listing = reg.search("message")
    assert "# slack" in listing


def test_search_returns_headers_not_signatures():
    reg = Registry()
    reg.register(_tool("slack", "Slack integration", send), source="installed")
    listing = reg.search("slack")
    assert "# slack" in listing
    assert "send(" not in listing and "channel: str" not in listing


def test_describe_expands_one_tool():
    reg = Registry()
    reg.register(_tool("slack", "Slack integration", send), source="installed")
    details = reg.describe("slack")
    assert "send(channel" in details and "text" in details  # full signature
    assert "Post a message to a channel." in details  # and its docstring


def test_results_are_capped_with_a_more_hint():
    reg = Registry()
    for i in range(15):
        reg.register(_tool(f"svc{i}", "widget service", send), source="installed")
    listing = reg.search("widget", limit=5)
    assert listing.count("# svc") == 5
    assert "more" in listing


def test_exact_name_outranks_summary_match():
    reg = Registry()
    reg.register(_tool("orders", "place orders", send), source="installed")
    reg.register(_tool("crm", "manage orders for customers", send), source="installed")
    listing = reg.search("orders")
    assert listing.index("# orders") < listing.index("# crm")


def test_no_match_falls_back_to_common_tools():
    reg = Registry()
    reg.register(_tool("common", "a featured tool", send), source="installed", featured=True)
    listing = reg.search("nonexistent-xyz")
    assert "no tool matched" in listing
    assert "# common" in listing  # featured set shown instead of a dead end


def test_empty_browse_with_nothing_featured_points_at_the_catalog():
    reg = Registry()
    reg.register(_tool("hidden", "a long-tail tool", send), source="installed")
    listing = reg.search("")  # nothing featured -> guide to the full catalog
    assert "no featured tools" in listing
    assert 'search_tools("*")' in listing


def test_search_never_resolves_a_lazy_tool():
    reg = Registry()
    calls = []

    def loader():
        calls.append(1)
        raise RuntimeError("should not be called by search")

    reg.register_lazy("flaky", loader, summary="a lazy server", keywords=("flaky",))
    listing = reg.search("flaky")
    assert "# flaky" in listing and "not loaded" in listing
    assert calls == []  # browsing the catalog connected nothing

    # describe() is what actually triggers the (here failing) connection.
    details = reg.describe("flaky")
    assert calls == [1]
    assert "unavailable" in details
