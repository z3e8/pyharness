from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import dataclass
from types import ModuleType
from typing import Callable

from . import builtin


@dataclass
class ToolInfo:
    name: str
    summary: str
    module: ModuleType | None = None  # None until a lazy tool is resolved
    source: str = "core"  # core | installed | learned
    loader: Callable[[], ModuleType] | None = None  # set for lazy (e.g. MCP) tools
    error: str | None = None  # last failure, if a lazy load could not connect
    keywords: tuple[str, ...] = ()  # synonyms/aliases, so intent words still match
    category: str | None = None  # intent group (e.g. "chat", "vcs") for grouping
    featured: bool = False  # surfaced by default and ranked first (the common set)
    instructions: str | None = None  # a learned skill's procedure, shown by describe()
    verified: bool = True  # a learned skill starts False, earning trust on a real run
    uses: tuple[dict, ...] = ()  # a learned skill's recent-use log (bounded, oldest first)


class Registry:
    """Index of tools. A tool is a Python module exposing functions; built-in,
    installed, and agent-authored skills all live here.

    Discovery is two-level so a large catalog never floods the agent's context:
    `search()` returns ranked *headers only* (name, summary, source/category) and
    never connects a lazy tool; `describe()` then expands the one chosen tool to
    its full interface (signatures + docstrings), connecting it if needed."""

    def __init__(self):
        self._tools: dict[str, ToolInfo] = {}
        self._mcp_clients: list = []  # closed on teardown
        self._load_package(builtin, source="core")

    def _load_package(self, package: ModuleType, source: str) -> None:
        for mod in pkgutil.iter_modules(package.__path__):
            if mod.name.startswith("_"):
                continue
            module = importlib.import_module(f"{package.__name__}.{mod.name}")
            self.register(module, source=source, name=mod.name)

    def register(
        self,
        module: ModuleType,
        *,
        source: str = "installed",
        name: str | None = None,
        keywords: tuple[str, ...] = (),
        category: str | None = None,
        featured: bool = False,
    ) -> str:
        """Add a tool module to the registry. Built-ins, locally installed
        modules, and MCP-wrapped servers all enter the same index — distinguished
        only by `source`. Returns the name under which it was registered.

        Discovery metadata (`keywords`, `category`, `featured`) may be passed here
        or declared on the module as `__keywords__` / `__category__` /
        `__featured__`; explicit arguments win."""
        name = name or module.__name__.rsplit(".", 1)[-1]
        summary = (module.__doc__ or "").strip().splitlines()[0] if module.__doc__ else ""
        self._tools[name] = ToolInfo(
            name,
            summary,
            module,
            source,
            keywords=tuple(keywords) or tuple(getattr(module, "__keywords__", ())),
            category=category or getattr(module, "__category__", None),
            featured=featured or bool(getattr(module, "__featured__", False)),
        )
        return name

    def add_mcp_server(
        self,
        name: str,
        command: str | None = None,
        args: tuple[str, ...] = (),
        *,
        url: str | None = None,
        env: dict | None = None,
        headers: dict | None = None,
        cwd: str | None = None,
        summary: str | None = None,
        keywords: tuple[str, ...] = (),
        category: str | None = None,
        featured: bool = False,
        timeout: float = 30.0,
    ) -> str:
        """Connect to an MCP server — local (`command`) or remote (`url`) — wrap
        each of its tools as a Python function, and register the result as one
        tool module named `name`. The server's client is closed by `close()`."""
        from .mcp import wrap_mcp_server

        module = wrap_mcp_server(
            name, command, args, url=url, env=env, headers=headers, cwd=cwd,
            summary=summary, timeout=timeout,
        )
        self._mcp_clients.append(module._mcp_client)
        return self.register(
            module, source="installed", name=name,
            keywords=keywords, category=category, featured=featured,
        )

    def register_lazy(
        self,
        name: str,
        loader: Callable[[], ModuleType],
        *,
        source: str = "installed",
        summary: str = "",
        keywords: tuple[str, ...] = (),
        category: str | None = None,
        featured: bool = False,
    ) -> str:
        """Register a tool whose module is built on first use, not now. The
        `loader` is called the first time the tool is *used or described* (never
        by `search`), so a server that is slow or down can neither delay nor abort
        registration, and merely browsing the catalog never connects it."""
        self._tools[name] = ToolInfo(
            name, summary, source=source, loader=loader,
            keywords=tuple(keywords), category=category, featured=featured,
        )
        return name

    def add_skill(
        self,
        name: str,
        description: str,
        instructions: str,
        *,
        loader: Callable[[], ModuleType],
        keywords: tuple[str, ...] = (),
        category: str | None = None,
        verified: bool = False,
        uses: tuple[dict, ...] = (),
    ) -> str:
        """Register a learned skill — markdown instructions plus an optional
        bundled module built on first use (`source="learned"`). It is a tool
        like any other: `search`/`use` treat it the same, while `describe`
        additionally surfaces the instructions. Skills are *not* featured: they
        are found by query, not shown in the default browse, so saved procedures
        don't crowd the common-tools listing.

        Unlike repo code, a skill is agent-authored and starts *unverified*: it
        earns trust only when a real run is logged (`verified`, `uses`), so a
        freshly written procedure can't masquerade as a proven one."""
        self._tools[name] = ToolInfo(
            name, description, source="learned", loader=loader,
            instructions=instructions, keywords=tuple(keywords),
            category=category, verified=verified, uses=tuple(uses),
        )
        return name

    def set_skill_usage(self, name: str, verified: bool, uses: tuple[dict, ...]) -> None:
        """Update a registered skill's trust state in place, so a `record_use`
        this session is reflected in `search`/`describe` without a reload."""
        info = self._tools.get(name)
        if info is not None:
            info.verified, info.uses = verified, tuple(uses)

    def _resolve(self, info: ToolInfo) -> ModuleType | None:
        """Return a tool's module, building it on demand for lazy tools. Returns
        None (and records `info.error`) if a lazy load fails — callers stay up."""
        if info.module is not None or info.loader is None:
            return info.module
        try:
            module = info.loader()
        except Exception as exc:
            info.error = f"{type(exc).__name__}: {exc}"
            return None
        info.module, info.error = module, None
        doc = (module.__doc__ or "").strip().splitlines()
        if doc:
            info.summary = doc[0]
        client = getattr(module, "_mcp_client", None)
        if client is not None:
            self._mcp_clients.append(client)
        return module

    def search(self, query: str = "", *, limit: int = 10, include_all: bool = False) -> str:
        """Return ranked **headers** for matching tools — name, summary, and
        tags (source, category, status) — never function signatures and never
        connecting a lazy tool. Use `describe()` to expand one tool.

        An empty query lists the *featured* tools (the common set); a real query
        ranks the whole catalog with featured tools breaking ties. Results are
        capped at `limit`; `include_all=True` (or query `"*"`) lifts the cap and
        the featured-only default. A query that matches nothing falls back to the
        featured set rather than a dead end."""
        q = query.strip().lower()
        if q == "*":
            q, include_all = "", True
        words = q.split()

        scored: list[tuple[float, ToolInfo]] = []
        for info in self._tools.values():
            score = _score(info, q, words)
            if score <= 0:
                continue
            if not q and not include_all and not info.featured:
                continue  # browse mode shows only the common (featured) tools
            scored.append((score, info))
        scored.sort(key=lambda t: (-t[0], not t[1].featured, t[1].name))

        shown = scored if include_all else scored[:limit]
        if not shown:
            return self._no_match(q)

        lines = [self._header(info) for _, info in shown]
        notes = []
        more = len(scored) - len(shown)
        if more > 0:
            notes.append(
                f"+{more} more — narrow the query or call "
                f'search_tools({query!r}, include_all=True).'
            )
        notes.append("describe_tool(name) shows a tool's functions; use_tool(name) loads it.")
        return "\n".join(lines) + "\n\n" + "\n".join(notes)

    def describe(self, name: str) -> str:
        """Expand one tool to its full interface: each public function's signature
        and first docstring line. Connects a lazy tool if it isn't loaded yet; an
        unreachable one is reported rather than raising."""
        if name not in self._tools:
            raise KeyError(f"tool {name!r} not found; try search_tools()")
        info = self._tools[name]
        module = self._resolve(info)
        lines = [self._header(info)]
        if info.source == "learned":  # lead with the trust signal, before the how-to
            lines += ["", _skill_trust_block(info)]
        if info.instructions:  # a learned skill carries its procedure inline
            lines += ["", info.instructions]
        if module is None:
            if info.instructions:  # instructions stand alone even if the code is down
                lines.append(f"\n(bundled code unavailable: {info.error})")
                return "\n".join(lines)
            return f"# {name} — {info.summary} (unavailable: {info.error})"
        funcs = list(_public_functions(module))
        if funcs and info.instructions:
            lines.append("\nFunctions:")
        for fname, func in funcs:
            doc = (func.__doc__ or "").strip().splitlines()
            first = doc[0] if doc else ""
            lines.append(f"    {fname}{inspect.signature(func)}  # {first}".rstrip())
        return "\n".join(lines)

    def use(self, name: str) -> ModuleType:
        if name not in self._tools:
            raise KeyError(f"tool {name!r} not found; try search_tools()")
        module = self._resolve(self._tools[name])
        if module is None:
            raise RuntimeError(f"tool {name!r} is unavailable: {self._tools[name].error}")
        return module

    def _header(self, info: ToolInfo) -> str:
        tags = [info.source]
        if info.category:
            tags.append(info.category)
        if info.featured:
            tags.append("featured")
        if info.source == "learned" and not info.verified:
            tags.append("unverified")  # never run successfully — a hypothesis, not fact
        if info.uses and info.uses[-1].get("outcome") == "failed":
            tags.append("last-failed")  # its most recent run broke; read the journal
        status = _status(info)
        if status:
            tags.append(status)
        head = f"# {info.name}" + (f" — {info.summary}" if info.summary else "")
        return f"{head}  [{' · '.join(tags)}]"

    def _no_match(self, q: str) -> str:
        if not q:  # empty browse with nothing featured — point at the full catalog
            n = len(self._tools)
            return (
                f"(no featured tools; the catalog has {n}. Search by what you need "
                'e.g. search_tools("web"), or search_tools("*") to list everything.)'
            )
        featured = [i for i in self._tools.values() if i.featured]
        if featured:
            body = "\n".join(self._header(i) for i in featured)
            return (
                f"(no tool matched {q!r}; showing the common tools — try other "
                f'keywords or search_tools(..., include_all=True))\n\n{body}'
            )
        return (
            f"(no tool matched {q!r}; try other keywords or "
            'search_tools("*") to list everything)'
        )

    def close(self) -> None:
        """Close every MCP server connection this registry opened."""
        for client in self._mcp_clients:
            try:
                client.close()
            except Exception:
                pass
        self._mcp_clients.clear()


def _skill_trust_block(info: ToolInfo) -> str:
    """The trust preamble shown by `describe` for a learned skill: whether it has
    ever worked, and its recent outcomes so a breaking change is visible before
    the agent relies on the procedure below."""
    if info.verified:
        head = "verified: yes — has run successfully before."
    else:
        head = (
            "verified: no — never confirmed against the real surface. Treat the "
            "steps below as a hypothesis: check them before relying on them."
        )
    lines = [head]
    if info.uses:
        lines.append("recent uses (oldest first):")
        for entry in info.uses[-3:]:
            note = f" — {entry['note']}" if entry.get("note") else ""
            lines.append(f"  {entry.get('at', '?')}  {entry.get('outcome', '?')}{note}")
    return "\n".join(lines)


def _score(info: ToolInfo, q: str, words: list[str]) -> float:
    """Lexical relevance of a tool to a query, matching only metadata that is
    available without connecting the tool (name, summary, keywords, category)."""
    if not q:
        return 1.0  # browse mode; featured filtering happens in search()
    name = info.name.lower()
    summary = info.summary.lower()
    keywords = [k.lower() for k in info.keywords]
    category = (info.category or "").lower()

    score = 0.0
    if q == name:
        score += 100
    elif q in name:
        score += 40
    elif q in summary:
        score += 8
    for word in words:
        if word == name or word in keywords:
            score += 20
        elif word in name:
            score += 10
        elif word == category:
            score += 8
        elif word in summary:
            score += 5
    return score


def _status(info: ToolInfo) -> str | None:
    if info.error:
        return f"unavailable: {info.error}"
    if info.module is not None:
        return f"{sum(1 for _ in _public_functions(info.module))} fn"
    if info.loader is not None:
        return "not loaded"
    return None


def _public_functions(module: ModuleType):
    for name, obj in inspect.getmembers(module, inspect.isfunction):
        if not name.startswith("_") and obj.__module__ == module.__name__:
            yield name, obj
