from __future__ import annotations

import functools
import hashlib
import inspect
import json
from pathlib import Path
from types import ModuleType

from ...security.grants import GrantScope
from ...security.policy import ActionCategory
from ...tools.registry import Registry, _public_functions
from ...tools.skills import SKILL_DIR_ATTR
from ...util import summarize_args

_SECRET_PREFIX = "secret:"


def mcp_tool_meta(registry: Registry, tool, func) -> tuple[bool, dict | None]:
    """The MCP descriptor behind one `tools.invoke` target, **without ever
    connecting a server** (policy runs before execution; deciding it must not
    spawn the server as a side effect). Returns `(is_mcp, meta)`: `(False,
    None)` for a non-MCP target, `(True, {"name", "annotations"})` for a
    resolved MCP function, and `(True, None)` when the MCP entry is not yet
    resolved or the function is unknown — callers fail closed on that."""
    info = registry.info(str(tool)) if tool is not None else None
    if info is None or info.kind != "mcp":
        return False, None
    if info.module is None:
        return True, None
    return True, getattr(info.module, "_mcp_tools", {}).get(str(func))


def mcp_tool_call(registry_getter):
    """Build the `approve_if` predicate that forces approval on any `tools.invoke`
    targeting an MCP server tool. `registry_getter` is called at decision time
    (the default Policy is built before the Session's registry exists). Never
    connects a server — an unresolved MCP target fails closed. Non-MCP targets
    (installed modules, learned skills) stay ungated here.

    No server-declared annotation suppresses this. `readOnlyHint` used to, which
    made the gate worth exactly what the server's own claim about itself was
    worth: a hostile server marking every tool read-only bought silence for the
    session. Whether a call is approved is the human's decision, taken per tool
    (see `ToolsCapability.scope`); the server only gets to describe itself in the
    prompt."""

    def predicate(action: str, args: tuple, kwargs: dict) -> bool:
        if action != "tools.invoke":
            return False
        tool, func, _, _ = _invoke_target(args, kwargs)
        is_mcp, _ = mcp_tool_meta(registry_getter(), tool, func)
        return is_mcp

    return predicate


def mcp_annotation_pin(meta: dict) -> str:
    """A short digest of the server-declared descriptor behind one tool — the
    original MCP name it maps to, plus its annotations. Folded into the grant
    scope so an approval covers the tool *as it was described when the human saw
    it*: a server that renames what `demo.echo` forwards to, or that flips a
    hint, no longer matches the grant and gets asked again."""
    payload = json.dumps(
        {"name": meta.get("name"), "annotations": meta.get("annotations") or {}},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _invoke_target(args: tuple, kwargs: dict) -> tuple:
    """The (tool, func, call_args, call_kwargs) of an invoke call, from either
    calling convention (child IPC sends positionals; JSON turns tuples into
    lists)."""
    tool = kwargs.get("tool", args[0] if len(args) >= 1 else None)
    func = kwargs.get("func", args[1] if len(args) >= 2 else None)
    call_args = kwargs.get("args", args[2] if len(args) >= 3 else ())
    call_kwargs = kwargs.get("kwargs", args[3] if len(args) >= 4 else {})
    return tool, func, tuple(call_args or ()), dict(call_kwargs or {})


class ToolsCapability:
    name = "tools"
    # `invoke` is the internal funnel every tool-module proxy (in-process and
    # the out-of-process child's RemoteToolSpec) routes through — not an entry
    # point for agent code to call by bare name. Keep it gated and reachable
    # via call()/call_op(), just not bound in the kernel namespace.
    hidden_ops = frozenset({"invoke"})

    def __init__(
        self,
        registry: Registry,
        *,
        broker=None,
        vault=None,
        mcp_config_path=None,
        allowed_hosts: frozenset[str] | None = None,
    ):
        self.registry = registry
        self._broker = broker  # set by Session; None leaves use_tool ungated
        self._vault = vault  # resolves secret:NAME refs when mounting a server
        self._mcp_config_path = Path(mcp_config_path) if mcp_config_path else None
        # The session's host scope, threaded into every MCP mount so a scoped
        # child's remote (HTTP) MCP reach is confined like its web/http reach.
        self._allowed_hosts = allowed_hosts

    def exports(self) -> dict:
        return {
            "search_tools": self.search_tools,
            "describe_tool": self.describe_tool,
            "use_tool": self.use_tool,
            "invoke": self.invoke,
            "add_mcp_server": self.add_mcp_server,
        }

    def search_tools(self, query: str = "", include_all: bool = False) -> str:
        return self.registry.search(query, include_all=include_all)

    def describe_tool(self, name: str) -> str:
        return self.registry.describe(name)

    def use_tool(self, name: str) -> ModuleType:
        """Load a tool and return it as a module of broker-gated functions.

        A module that is already broker-gated (a core capability surfaced via
        `as_tool_module`) passes through untouched — wrapping it again would
        gate the same call twice. Everything else (MCP wrappers, installed
        modules, learned skills) is returned as proxies routing through
        `tools.invoke`, so in-process calls get the same policy/audit/approval
        treatment the out-of-process child already gets via `RemoteToolSpec`."""
        module = self.registry.use(name)
        if self._broker is None or getattr(module, "_broker_gated", False):
            return module
        gated = ModuleType(name)
        gated.__doc__ = module.__doc__
        # A learned skill keeps its source marker across the wrapper. Out of
        # process the host reads it and ships source to the child instead of
        # this proxy module, so the skill runs sandboxed (`_seal_for_wire`);
        # in process there is no child and these gated proxies are what runs.
        if (skill_dir := getattr(module, SKILL_DIR_ATTR, None)) is not None:
            setattr(gated, SKILL_DIR_ATTR, skill_dir)
        for fname, func in _public_functions(module):
            proxy = self._gated_proxy(name, fname)
            functools.wraps(func)(proxy)  # copy __doc__/__wrapped__ (signature)
            proxy.__module__ = name  # after wraps, so _public_functions still finds it
            setattr(gated, fname, proxy)
        return gated

    def _gated_proxy(self, tool: str, fname: str):
        def proxy(*args, **kwargs):
            return self._broker.call("tools", "invoke", tool, fname, args, kwargs)

        proxy.__name__ = fname
        return proxy

    def invoke(self, tool: str, func: str, args: tuple, kwargs: dict):
        """Call a tool's function through the broker. Both kernel modes route
        tool-module calls here — the child via its `RemoteToolSpec` proxy, the
        in-process kernel via `use_tool`'s gated proxies — so every call gets
        the same policy/audit/budget gating as any other capability."""
        return getattr(self.registry.use(tool), func)(*args, **kwargs)

    def add_mcp_server(
        self,
        name: str,
        command: str | None = None,
        args: tuple = (),
        *,
        url: str | None = None,
        env: dict | None = None,
        headers: dict | None = None,
        summary: str | None = None,
        keywords: tuple = (),
        category: str | None = None,
        timeout: float = 30.0,
        save: bool = False,
    ) -> str:
        """Mount an MCP server for this session — local (`command` + `args`) or
        remote (`url`) — as one tool module named `name`, findable via
        search_tools and loadable via use_tool. Mounting is lazy: the server is
        contacted on first describe/use, not now. Requires human approval
        (mounting a server installs code).

        Credential values in `env`/`headers` should be vault references
        (`"secret:NAME"`), resolved parent-side and never visible to you.
        `save=True` also persists the server to the session's MCP config file so
        later sessions mount it automatically — that path *requires* `secret:`
        refs for every env/header value; a cleartext credential is refused."""
        if self.registry.info(name) is not None:
            raise ValueError(
                f"a tool named {name!r} already exists; pick a different server name"
            )
        if bool(command) == bool(url):
            raise ValueError("specify exactly one of command (local) or url (remote)")
        spec = {
            key: value
            for key, value in {
                "command": command,
                "args": list(args),
                "url": url,
                "env": dict(env) if env else None,
                "headers": dict(headers) if headers else None,
                "summary": summary,
                "keywords": list(keywords),
                "category": category,
                "timeout": timeout,
            }.items()
            if value
        }
        if save:
            self._save_server(name, spec)
        from ...tools.mcp import mount_config

        mount_config(
            self.registry,
            {"mcpServers": {name: spec}},
            vault=self._vault,
            allowed_hosts=self._allowed_hosts,
        )
        saved = f" and saved to {self._mcp_config_path}" if save else ""
        return f"mounted MCP server {name!r} (lazy; connects on first use){saved}"

    def _save_server(self, name: str, spec: dict) -> None:
        """Merge one server spec into the session's MCP config file, refusing to
        persist anything that isn't a `secret:` reference in env/headers — the
        config file's contract is that no cleartext credential lives in it."""
        if self._mcp_config_path is None:
            raise ValueError(
                "this session has no MCP config path to save to; "
                "mount with save=False, or configure PYHARNESS_MCP_CONFIG"
            )
        for field in ("env", "headers"):
            for key, value in (spec.get(field) or {}).items():
                if not (isinstance(value, str) and value.startswith(_SECRET_PREFIX)):
                    raise ValueError(
                        f"{field}[{key!r}] is not a 'secret:NAME' reference; refusing to "
                        "write a possible cleartext credential to the config file. Store "
                        "the value in the vault and pass 'secret:NAME' instead."
                    )
        path = self._mcp_config_path
        config = json.loads(path.read_text()) if path.exists() else {}
        config.setdefault("mcpServers", {})[name] = spec
        path.write_text(json.dumps(config, indent=2) + "\n")

    def preview(self, op: str, args: tuple, kwargs: dict) -> tuple[ActionCategory, str]:
        """Describe a gated call for the approver. An MCP `invoke` is OUTWARD:
        the harness cannot know whether someone else's server tool can be taken
        back, and IRREVERSIBLE means an effect the *harness* knows is permanent,
        not one the server says is. A declared `destructiveHint` is shown to the
        human as what it is — the server's own claim about itself — and decides
        nothing: it neither raises the category nor withholds a grant, because a
        server willing to lie about it would simply not declare it. What bounds
        the call is that the grant covers this one tool only (see `scope`)."""
        if op == "invoke":
            tool, func, call_args, call_kwargs = _invoke_target(args, kwargs)
            summary = f"{tool}.{func}({summarize_args(call_args, call_kwargs)})"
            is_mcp, meta = mcp_tool_meta(self.registry, tool, func)
            if is_mcp:
                if meta is None:
                    summary += "  [MCP tool, server not yet connected]"
                elif meta["annotations"].get("destructiveHint"):
                    summary += "  [MCP tool the server declares destructive]"
                else:
                    summary += "  [MCP tool]"
            return ActionCategory.OUTWARD, summary
        if op == "add_mcp_server":
            # Secret-safe: env/header *names* only, never values (which may be
            # credentials the agent was allowed to pass as secret: refs).
            bound = inspect.signature(self.add_mcp_server).bind(*args, **kwargs)
            bound.apply_defaults()
            b = bound.arguments
            target = (
                f"command: {b['command']}" if b.get("command") else f"url: {b['url']}"
            )
            parts = [target]
            if b.get("env"):
                parts.append(f"env: {', '.join(b['env'])}")
            if b.get("headers"):
                parts.append(f"headers: {', '.join(b['headers'])}")
            if b.get("save"):
                parts.append("save: persists to the MCP config file")
            summary = f"mount MCP server {b['name']!r} ({'; '.join(parts)})"
            return ActionCategory.OUTWARD, summary
        return ActionCategory.OUTWARD, f"tools.{op}({summarize_args(args, kwargs)})"

    def scope(self, op: str, args: tuple, kwargs: dict) -> GrantScope | None:
        """The grant key for a gated invoke: action-class "mcp" on one
        `server.tool` pair, pinned to the descriptor the human was shown. A
        standing approval therefore covers repeat calls to *that* tool and
        nothing else — approving `demo.echo` says nothing about `demo.getenv`,
        which is a different decision the human never saw.

        A not-yet-resolved target yields None (not grantable): with no descriptor
        there is nothing to pin, and a grant minted now could not be tied to what
        the server later turns out to offer."""
        if op != "invoke":
            return None
        tool, func, _, _ = _invoke_target(args, kwargs)
        is_mcp, meta = mcp_tool_meta(self.registry, tool, func)
        if not is_mcp or meta is None:
            return None
        return GrantScope("mcp", f"{tool}.{func}", pin=mcp_annotation_pin(meta))
