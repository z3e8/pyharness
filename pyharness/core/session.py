from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from ..audit import AuditLog
from ..broker.capabilities import (
    BrowserCapability,
    FilesCapability,
    HistoryCapability,
    HttpSessionCapability,
    InboxCapability,
    LLMCapability,
    NotifyCapability,
    ObservabilityCapability,
    PackagesCapability,
    SearchCapability,
    SecretsCapability,
    ShellCapability,
    SkillsCapability,
    ToolsCapability,
    WebCapability,
)
from ..broker.capabilities.browser import MUTATING_ACTIONS as MUTATING_BROWSER_ACTIONS
from ..broker.capabilities.http import MUTATING_METHODS
from ..broker.capabilities.spawn import ChildRun, SpawnCapability, SpawnResult
from ..broker.capabilities.tools import unvetted_mcp_call
from ..broker.dispatch import Approver, Broker
from ..broker.remote import RemoteKernel
from ..budget import Budget
from ..llm.client import AnthropicLLM
from ..obs import telemetry
from ..obs.trace import TraceLog
from ..security.egress import normalize_scope_hosts
from ..security.policy import Policy
from ..security.profiles import ProfileStore
from ..security.vault import Vault
from ..tools.registry import Registry
from .agent import Agent
from .kernel import Kernel
from .media import MediaOutbox
from .session_venv import SessionVenv
from .workspace import Workspace

# What a spawned child session may hold. Its body — workspace files, search,
# LLM-as-function delegation — is always in scope; everything else must be
# granted by name in `spawn(tools=...)`. `spawn` itself is never grantable, so
# delegation depth is one by construction (the Claude Code trick: enforce the
# cap by omitting the capability, not by a runtime check).
CHILD_BODY = frozenset({"files", "search", "llm"})
CHILD_GRANTABLE = frozenset(
    {
        "shell",
        "secrets",
        "skills",
        "history",
        "obs",
        "notify",
        "web",
        "http",
        "browser",
        "inbox",
        "packages",
    }
)
# External capabilities are reached through tool discovery, so granting any of
# them implies the `tools` capability (search_tools/describe_tool/use_tool).
_EXTERNAL = frozenset({"web", "http", "browser", "inbox", "packages"})
# The capabilities a host scope (`allowed_hosts`) governs. Scoping a spawn that
# grants none of these is a confused call and refused.
_NETWORK = frozenset({"web", "http", "browser"})


def _is_mutating_http(action: str, args: tuple, kwargs: dict) -> bool:
    """Force approval on state-changing HTTP requests (POST/PUT/PATCH/DELETE);
    reads stay free. The method is the second positional arg or a keyword —
    `request(session_id, method, url, ...)`."""
    if action != "http.request":
        return False
    method = kwargs.get("method")
    if method is None and len(args) >= 2:
        method = args[1]
    return bool(method) and method.upper() in MUTATING_METHODS


def _request_carries_secret(action: str, args: tuple, kwargs: dict) -> bool:
    """Force approval whenever a call attaches a vault secret to an outbound
    request — on `http.request` (`auth`/`secret_fields`) or `web.fetch` (`auth`) —
    regardless of HTTP method. Sending a credential off-box is a credential-release
    action, like `browser.fill_secret`; without this gate a GET (a free read) could
    exfiltrate a *named* secret to any attacker host with no human checkpoint, since
    reads are otherwise unapproved. Grantable per host (see the capabilities'
    `scope()`), so repeated authenticated reads to a vetted host don't re-prompt."""
    if action == "http.request":
        return bool(kwargs.get("auth") or kwargs.get("secret_fields"))
    if action == "web.fetch":
        return bool(kwargs.get("auth") or (args[1] if len(args) >= 2 else None))
    return False


def _opens_with_profile(action: str, args: tuple, kwargs: dict) -> bool:
    """Force approval when a browser opens with a saved login profile — it acts as
    that identity, and this is the one human checkpoint before free navigation
    transmits the restored cookies to the site. A plain `open_browser()` stays
    free. The profile is the first positional arg or the `profile=` keyword."""
    if action != "browser.open_browser":
        return False
    return bool(kwargs.get("profile") or (args[0] if args else None))


class Session:
    """One conversation between the user and the agent. Owns the five things that
    must stay consistent: history, kernel, policy + budget, audit log, and the
    workspace directory."""

    def __init__(
        self,
        root: str | Path,
        *,
        llm=None,
        budget: Budget | None = None,
        policy: Policy | None = None,
        vault: Vault | None = None,
        profiles: ProfileStore | None = None,
        registry: Registry | None = None,
        approver: Approver | None = None,
        on_event: Callable[[str, str], None] | None = None,
        # Test-only escape hatch: True exec()s agent code in THIS process, with
        # the host's os.environ and live vault/broker objects reachable by
        # introspection. The default runs it in a sandboxed child (broker/remote).
        unsafe_in_process: bool = False,
        mcp_config: str | Path | dict | None = None,
        skills_dir: str | Path | None = None,
        index_db: str | Path | None = None,
        keep_outputs: int = 8,
        max_steps: int = 30,
        tier: str = "mid",
        # Host scope: confines this session's web/http/browser reach to these
        # hosts (and their subdomains) — everything else is refused at the
        # egress layer, and web search is disabled (the query would leave
        # scope). None = unscoped. The first user is spawn(allowed_hosts=...),
        # but any session can be scoped this way.
        allowed_hosts: Iterable[str] | None = None,
        # Spawned-child wiring (set by Session._spawn_child, not by callers):
        # a capability allowlist, the parent's audit log (one hash chain for the
        # whole session tree), the parent's workspace dir (shared data plane),
        # and a preamble that replaces the history/skills orientation block.
        capabilities: Iterable[str] | None = None,
        audit: AuditLog | None = None,
        workspace_dir: Path | None = None,
        preamble: str | None = None,
    ):
        telemetry.setup_telemetry()
        self.id = uuid4().hex
        self.workspace = Workspace(root, shared_dir=workspace_dir)
        self.budget = budget or Budget()
        self.audit = audit or AuditLog(self.workspace.root / "audit.jsonl")
        # None = a full parent session; a set = a spawned child holding its
        # body plus exactly what it was granted.
        if capabilities is None:
            self._caps: frozenset[str] | None = None
        else:
            granted = frozenset(capabilities)
            self._caps = (
                granted
                | CHILD_BODY
                | ({"tools"} if granted & _EXTERNAL else frozenset())
            )
        self.allowed_hosts: frozenset[str] | None = (
            normalize_scope_hosts(allowed_hosts) if allowed_hosts is not None else None
        )
        self._unsafe_in_process = unsafe_in_process
        self._spawn_seq = 0
        self.trace = TraceLog(self.workspace.root / "trace.jsonl")
        self.llm = llm or AnthropicLLM(budget=self.budget)
        # Serialize the display callback: the parent agent loop and N spawned
        # child threads all call it, and renderers keep cross-call state (the
        # CLI's open-thinking marker) and print in multiple writes — so
        # concurrent calls would interleave mid-line. One lock makes each
        # event's full render atomic. (A child session wraps its own callback
        # too; that inner callback funnels into the parent's locked one, so
        # the parent lock is where the whole tree serializes.)
        if on_event is not None:
            _raw_on_event = on_event
            _display_lock = threading.Lock()

            def on_event(
                kind: str, text: str, _inner=_raw_on_event, _lock=_display_lock
            ):
                with _lock:
                    _inner(kind, text)

        # The raw display callback (the CLI renderer), kept so spawned children
        # running in background threads can surface progress lines to it.
        self._display_event = on_event
        # Spawned children run in parent-side threads, so a child may ask for
        # approval while the parent (or a sibling) is also prompting — serialize
        # every prompt behind one lock so the terminal shows one at a time.
        self._approval_lock = threading.Lock()
        if approver is not None:
            inner_approver = approver

            def approver(request, _inner=inner_approver, _lock=self._approval_lock):
                with _lock:
                    return _inner(request)

        # One event sink shared by the agent loop and any capability that emits
        # events (notify): everything lands in trace.jsonl, then the caller's
        # on_event (the CLI renderer) sees it live.
        # llm_token fires once per streaming chunk — too frequent for the trace.
        # llm_thinking is chunk-frequent too, but the live viewer streams it
        # from the trace, so chunks are buffered and flushed in batches (size or
        # age), with any remainder flushed before the next non-thinking record
        # so ordering in the file matches what happened.
        think_buf: list[str] = []
        think_last = [0.0]

        def _flush_thinking() -> None:
            if think_buf:
                self.trace.record("llm_thinking", "".join(think_buf))
                think_buf.clear()

        def on_event_traced(kind: str, text: str, **extra) -> None:
            if kind == "llm_thinking":
                now = time.monotonic()
                if not think_buf:
                    think_last[0] = now
                think_buf.append(text)
                if sum(len(c) for c in think_buf) >= 600 or now - think_last[0] >= 1.5:
                    _flush_thinking()
                    think_last[0] = now
            elif kind != "llm_token":
                _flush_thinking()
                self.trace.record(kind, text, **extra)
            if on_event is not None:
                on_event(kind, text)

        # Saving a skill (agent-authored code that auto-loads later) and
        # installing packages sign off at author time; any state-changing HTTP
        # request is gated per-call (reads stay free).
        def _look_after_injected_secret(action: str, args: tuple, kwargs: dict) -> bool:
            """Gate a screenshot once a secret was typed into the page — pixels
            carry the credential where text redaction can't reach. `look` puts the
            image in the model's context; `screenshot` writes a PNG the agent can
            then read back (via files.read or raw open()), so both are covered.
            Reads self.browser at call time (set below)."""
            if action not in ("browser.look", "browser.screenshot"):
                return False
            sid = args[0] if args else kwargs.get("session_id")
            return sid is not None and self.browser.has_injected_secrets(sid)

        self.policy = policy or Policy(
            require_approval={
                "skills.save_skill",
                "skills.edit_skill",
                "packages.install",
                # An arbitrary program, run parent-side. The OS sandbox jails its
                # filesystem/network, but the command itself is unvetted — and on
                # an unsandboxed platform there is no jail at all — so a human
                # signs each one.
                "shell.bash",
                "tools.add_mcp_server",  # mounting a server installs code
                # Approving a spawn is approving the child's whole plan: task,
                # capability set, budget slice (see SpawnCapability.preview).
                "spawn.spawn",
                # State-changing browser actions; navigation and reads stay free.
                *MUTATING_BROWSER_ACTIONS,
                # Persisting a login writes a standing credential — sign off once.
                "browser.save_profile",
                # Minting a new identity + standing credential — sign off per
                # site (never grant-covered; see SecretsCapability.scope).
                "vault.create_login",
            },
            approve_if=[
                _is_mutating_http,
                _request_carries_secret,
                _look_after_injected_secret,
                _opens_with_profile,
                # MCP tool calls prompt unless the server marks them read-only;
                # the registry is read at decision time (it's built below).
                unvetted_mcp_call(lambda: self.registry),
            ],
        )
        self.vault = vault or Vault.from_env()
        # Encrypted, named browser login profiles (persistent web identity). None
        # when no vault passphrase is set — opening a profile then fails closed
        # rather than falling back to plaintext.
        self.profiles = profiles or ProfileStore.from_env()
        self.registry = registry or Registry()
        # A path is remembered even if the file doesn't exist yet, so
        # tools.add_mcp_server(save=True) has somewhere to write; a dict config
        # has no path (saving is then refused).
        self.mcp_config_path: Path | None = (
            Path(mcp_config) if isinstance(mcp_config, (str, Path)) else None
        )
        if mcp_config is not None:
            from ..tools.mcp import mount_config

            if self.mcp_config_path is None or self.mcp_config_path.exists():
                mount_config(
                    self.registry,
                    mcp_config,
                    vault=self.vault,
                    allowed_hosts=self.allowed_hosts,
                )

        # Skills are learned tools persisted on disk; load any from prior sessions
        # (or hand-authored by the user) so they reload here. Cross-session by
        # design, so the root defaults outside the per-session workspace.
        from ..tools.skills import load_skills

        self.skills_dir = Path(skills_dir or "~/.pyharness/skills").expanduser()
        if self._has("skills"):
            load_skills(self.registry, self.skills_dir)
        # Lessons (distilled cross-session facts) live beside the skills root.
        self.lessons_path = self.skills_dir.parent / "lessons.json"

        # The session index (direction 5) — a derived SQLite view over past
        # sessions' JSONL, feeding the `stats`/`inspect_session` builtins and the
        # preamble. None (the default) leaves those dataless; the CLI passes the
        # global DB. Refresh is fail-open: the index is a cache, never a blocker.
        self.index_db = Path(index_db).expanduser() if index_db else None
        self._refresh_index()

        self.session_venv = SessionVenv()
        # Activity events go to the trace only (not the display stream): they are
        # the machine-readable "happening right now" record a live viewer tails;
        # the CLI already renders the human-facing side (code, output, prompts).
        self.broker = Broker(
            self.policy,
            self.audit,
            self.budget,
            approver=approver,
            on_event=self.trace.record,
        )
        # Web fetch is a thin wrapper over the stateful HTTP capability, so the
        # latter is built first and shared with WebCapability.
        self.http = HttpSessionCapability(
            self.workspace, vault=self.vault, allowed_hosts=self.allowed_hosts
        )
        # One outbox shared by the browser (fills it via look()) and the agent
        # loop (drains it into image content blocks after each cell).
        self.media = MediaOutbox()
        self.browser = BrowserCapability(
            self.workspace,
            vault=self.vault,
            media=self.media,
            profiles=self.profiles,
            audit=self.audit,
            allowed_hosts=self.allowed_hosts,
        )
        # Core builtins — the agent's own body (workspace, shell, delegation,
        # reflection) plus the tool-discovery entrypoint. Always in scope for a
        # parent session; a spawned child holds its body plus what `spawn(tools=)`
        # granted it (see CHILD_BODY/CHILD_GRANTABLE above).
        core_caps: list = [
            FilesCapability(self.workspace),
            SearchCapability(self.workspace),
            LLMCapability(self.llm, budget=self.budget, on_event=on_event_traced),
        ]
        if self._has("shell"):
            # bash and the child kernel run on the minimal allowlist environment
            # (security/env.py) — no secret, and no unknown .env var, is one
            # `printenv` away from agent code.
            core_caps.append(ShellCapability(self.workspace))
        if self._has("tools"):
            core_caps.append(
                ToolsCapability(
                    self.registry,
                    broker=self.broker,
                    vault=self.vault,
                    mcp_config_path=self.mcp_config_path,
                    allowed_hosts=self.allowed_hosts,
                )
            )
        if self._has("secrets"):
            # The identity email stays a parent-side config: the capability runs
            # in the parent, so it is deliberately NOT on the child env allowlist.
            core_caps.append(
                SecretsCapability(
                    self.vault,
                    identity_email=os.environ.get("PYHARNESS_IDENTITY_EMAIL"),
                )
            )
        if self._has("skills"):
            # Skill authorship/outcomes land in the trace (not the display stream)
            # so the session index can attribute skill uses to sessions.
            core_caps.append(
                SkillsCapability(
                    self.registry, self.skills_dir, on_event=self.trace.record
                )
            )
        if self._has("history"):
            core_caps.append(HistoryCapability(self.audit))
        if self._has("obs"):
            core_caps.append(ObservabilityCapability(self.index_db, self.llm))
        if self._has("notify"):
            core_caps.append(NotifyCapability(on_event=on_event_traced))
        # Real sub-agents — parent sessions only: a child's capability set never
        # includes spawn, so delegation depth is one by construction. The ref is
        # kept so close() can cooperatively stop children still running.
        self._spawn_cap: SpawnCapability | None = None
        if self._caps is None:
            self._spawn_cap = SpawnCapability(self._start_child)
            core_caps.append(self._spawn_cap)
        for capability in core_caps:
            self.broker.register(capability)

        # External-reaching capabilities — the web, a browser, HTTP APIs, the
        # package index. Registered for gating but NOT surfaced as builtins; the
        # agent discovers and loads them through the tool registry
        # (search_tools -> describe_tool -> use_tool), same path as MCP tools and
        # learned skills. One coherent, discoverable surface for everything
        # external; gating is identical to a builtin's.
        tool_caps = [
            (
                WebCapability(http=self.http),
                "Read the web: search (a raw ranked list to fan out over), and fetch a single URL.",
                "web",
                (
                    "web",
                    "http",
                    "fetch",
                    "search",
                    "results",
                    "url",
                    "download",
                    "browse",
                    "internet",
                ),
            ),
            (
                self.http,
                "Stateful HTTP: open a session (cookies persist), POST/PUT, upload files.",
                "web",
                (
                    "http",
                    "request",
                    "post",
                    "put",
                    "session",
                    "api",
                    "cookie",
                    "upload",
                    "rest",
                ),
            ),
            (
                self.browser,
                "Drive a headless browser. open_browser() returns a session_id every other function takes as its first argument: goto (navigate to a url), snapshot the page (element refs), click/fill/select_option/press by ref or selector, upload, look (a screenshot the model sees), read_text. Login without seeing credentials: fill_secret(session_id, ref=..., secret=NAME) types the vault secret named NAME, fill_totp the current 2FA code from a vault TOTP seed. Named login profiles persist across sessions (open_browser(profile=...) / save_profile). describe_tool('browser') for exact signatures.",
                "web",
                (
                    "browser",
                    "playwright",
                    "snapshot",
                    "ref",
                    "aria",
                    "click",
                    "fill",
                    "select",
                    "press",
                    "upload",
                    "look",
                    "screenshot",
                    "page",
                    "dom",
                    "headless",
                    "form",
                    "profile",
                    "login",
                    "cookie",
                    "persistent",
                    "identity",
                    "totp",
                    "2fa",
                    "otp",
                    "mfa",
                ),
            ),
            (
                InboxCapability(self.workspace, vault=self.vault),
                "Read-only email over IMAP: list/search message metadata, read one message as clean text + links (attachments land in the workspace). No send/delete/flag — reads leave the mailbox untouched.",
                "email",
                (
                    "inbox",
                    "email",
                    "imap",
                    "mail",
                    "message",
                    "verification",
                    "magic",
                    "link",
                    "code",
                    "2fa",
                    "otp",
                    "confirmation",
                    "receipt",
                    "attachment",
                    "unread",
                ),
            ),
            (
                PackagesCapability(self.session_venv),
                "Install Python packages into the session for later import.",
                "packages",
                ("install", "pip", "package", "dependency", "library", "import"),
            ),
        ]
        for capability, summary, category, keywords in tool_caps:
            if not self._has(capability.name):
                continue
            self.broker.register(capability, core=False)
            self.registry.register(
                self.broker.as_tool_module(capability.name, summary=summary),
                source="core",
                name=capability.name,
                keywords=keywords,
                category=category,
            )

        # Default (out-of-process): agent code runs in a restricted child and
        # every call crosses IPC back to the same broker (see broker/remote).
        # unsafe_in_process (test-only): the broker's proxies run directly in
        # the host namespace — no process boundary at all.
        self.kernel = (
            Kernel(self.broker.namespace())
            if unsafe_in_process
            else RemoteKernel(
                self.broker,
                venv=self.session_venv,
                workspace=self.workspace,
            )
        )

        self.agent = Agent(
            self.llm,
            self.kernel,
            self.budget,
            tier=tier,
            max_steps=max_steps,
            workspace_root=self.workspace.dir,
            on_event=on_event_traced,
            media=self.media,
            media_dir=self.workspace.root / "media",
            preamble_extra=preamble
            if preamble is not None
            else self._render_history_preamble(),
            keep_outputs=keep_outputs,
        )
        self.messages: list[dict] = []
        # close() must run exactly once even when called concurrently — the
        # owner's thread and an abandoning parent can both reach it.
        self._closed = False
        self._close_lock = threading.Lock()
        self.trace.record(
            "session_start", session_id=self.id, root=str(self.workspace.root)
        )

    def _has(self, name: str) -> bool:
        """Whether this session holds a capability: parents hold everything,
        a spawned child holds its body plus what it was granted."""
        return self._caps is None or name in self._caps

    def _child_preamble(
        self, granted: frozenset[str], allowed_hosts: frozenset[str] | None = None
    ) -> str:
        """The sub-session block appended to the child's system prompt. It must
        correct the static prompt's builtin list (the child holds a subset) and
        carry the report contract — the handoff is the whole point of a spawn.

        Deliberately free of per-child numbers (budget slice, step ceiling): they
        vary per spawn, and baking them into the cached system prefix gives every
        child a distinct prompt so siblings never share a prompt cache. The live
        figures already reach the child in the per-cell meter; here we keep only
        the qualitative note."""
        builtins_by_cap = {
            "files": "read/write/edit",
            "search": "search",
            "llm": "llm/map_llm",
            "shell": "bash",
            "secrets": "secrets",
            "skills": "save_skill/edit_skill/record_skill_use",
            "history": "history",
            "obs": "stats/inspect_session",
            "notify": "notify",
            "tools": "search_tools/describe_tool/use_tool/add_mcp_server",
        }
        held = (
            CHILD_BODY | granted | ({"tools"} if granted & _EXTERNAL else frozenset())
        )
        builtins = ", ".join(v for k, v in builtins_by_cap.items() if k in held)
        mounted = sorted(granted & _EXTERNAL)
        tools_line = (
            f"TOOLS mounted this session: {', '.join(mounted)} — load with use_tool(name)."
            if mounted
            else "No external tools are mounted this session."
        )
        # Host-scope lines: vary per spawn, but the child must know its bounds
        # to plan within them — worth the (rare) lost prompt-cache sharing.
        # Honesty matters here: `allowed_hosts` governs the web/http/browser
        # reach (and MCP servers over HTTP); shell/packages and local MCP
        # servers are deliberately outside it (approval-gated instead), and the
        # child must not be promised a containment the code does not enforce.
        scope_line = ""
        if allowed_hosts is not None:
            not_scoped = sorted(granted & {"shell", "packages"})
            not_scoped.append("local (command-run) MCP servers")
            scope_line = (
                "- Your web/http/browser reach — including MCP servers reached\n"
                "  over HTTP — is scoped to these hosts (and their subdomains)\n"
                f"  only: {', '.join(sorted(allowed_hosts))}. Requests on those\n"
                "  surfaces to any other host are refused, and web search is unavailable.\n"
                "  If the task needs a host outside this scope, say so in your\n"
                "  report instead of retrying.\n"
                f"- Not covered by the host scope: {', '.join(not_scoped)} —\n"
                "  those are gated by per-call human approval and the OS sandbox\n"
                "  instead. Do not use them to reach hosts outside the scope.\n"
            )
        return (
            "## Spawned sub-session\n"
            "You are a sub-session spawned by an orchestrator to complete the one\n"
            "task below. The rules above apply, with corrections:\n"
            f"- Your builtins are only: {builtins}. Anything else named above\n"
            "  (including spawn) is NOT available this session — do not call it.\n"
            f"- {tools_line}\n"
            f"{scope_line}"
            "- You share the orchestrator's workspace: write anything large or\n"
            "  durable to files there, at the paths the task assigns.\n"
            "- Your step and spend budget are tighter than a top-level session and\n"
            "  shown in the meter after each cell. Checkpoint results to the\n"
            "  workspace as you go.\n"
            "- Your final plain-text reply is your REPORT — the only thing the\n"
            "  orchestrator sees. Make it self-contained and condensed: outcome\n"
            "  first, then key findings and decisions, then the workspace paths of\n"
            "  everything you produced. Never paste large content into the report;\n"
            "  point at files instead."
        )

    def _start_child(
        self,
        task: str,
        *,
        tools: tuple[str, ...],
        budget_usd: float | None,
        max_steps: int,
        tier: str,
        allowed_hosts: tuple[str, ...] | None = None,
    ) -> ChildRun:
        """Build one spawned child session and start it running in a parent-side
        thread (the working half of the `spawn` builtin — the gated surface
        lives in SpawnCapability). The build is synchronous so the handle the
        caller gets back names a session whose workspace and trace already
        exist; only the run is asynchronous."""
        granted = frozenset(tools) - CHILD_BODY  # body names are accepted, implied
        unknown = granted - CHILD_GRANTABLE
        if unknown:
            raise ValueError(
                f"unknown spawn tools {sorted(unknown)}; grantable: {sorted(CHILD_GRANTABLE)}"
            )
        # Normalize the host scope up front so a bad entry fails the spawn call
        # (not the child mid-run), and so the trace/preamble show the canonical
        # hosts. Scoping without a network capability is a confused call.
        scope: frozenset[str] | None = None
        if allowed_hosts is not None:
            scope = normalize_scope_hosts(allowed_hosts)
            if not granted & _NETWORK:
                raise ValueError(
                    "allowed_hosts requires granting a network capability "
                    f"({', '.join(sorted(_NETWORK))}) in tools"
                )

        self._spawn_seq += 1
        name = f"{self.workspace.root.name}-spawn-{self._spawn_seq:02d}"
        short = f"spawn-{name.rsplit('-spawn-', 1)[1]}"
        # A sibling session dir, so the index/inspect_session/`pyharness show`
        # all see the child exactly like any other session.
        child_root = self.workspace.root.parent / name

        # Reserve the slice up front: the child budget is its own accumulator
        # with its own limit, settled into the parent's on close via absorb().
        remaining = self.budget.remaining()
        if budget_usd is None:
            limit = None if remaining == float("inf") else remaining / 4
        else:
            limit = min(budget_usd, remaining)
        child_budget = Budget(limit_usd=limit)
        child_llm = (
            self.llm.with_budget(child_budget)
            if hasattr(self.llm, "with_budget")
            else self.llm
        )

        # Approvals bubble to the same human, labeled with the child's name so
        # the approver can tell whose plan is asking. The parent's approver is
        # already serialized behind the session approval lock, so concurrent
        # children prompt one at a time.
        parent_approver = self.broker.approver
        approver = None
        if parent_approver is not None:

            def approver(request, _name=short):  # noqa: E306
                return parent_approver(
                    replace(request, summary=f"[{_name}] {request.summary}")
                )

        # The child's own events land in its trace (the live viewer lanes them
        # from there); the milestones additionally surface on the parent's
        # display stream so the terminal shows background children progressing.
        def child_display(kind: str, text: str, _name=short) -> None:
            if self._display_event is None or kind not in (
                "task",
                "code",
                "answer",
                "error",
                "notify",
            ):
                return
            brief = " ".join(text.split())
            brief = brief[:100] + "…" if len(brief) > 100 else brief
            line = f"[{_name}] {kind}: {brief}" if brief else f"[{_name}] {kind}"
            self._display_event("spawn_progress", line)

        self.trace.record(
            "spawn",
            task,
            child=name,
            tools=sorted(granted),
            allowed_hosts=sorted(scope) if scope is not None else None,
            budget_usd=limit,
            max_steps=max_steps,
            tier=tier,
        )
        child = Session(
            child_root,
            llm=child_llm,
            budget=child_budget,
            vault=self.vault,
            profiles=self.profiles,
            approver=approver,
            on_event=child_display,
            unsafe_in_process=self._unsafe_in_process,
            skills_dir=self.skills_dir,
            index_db=self.index_db,
            max_steps=max_steps,
            tier=tier,
            capabilities=granted,
            allowed_hosts=sorted(scope) if scope is not None else None,
            audit=self.audit,  # one hash chain for the whole session tree
            workspace_dir=self.workspace.dir,
            preamble=self._child_preamble(granted, scope),
        )
        child.trace.record(
            "spawned_by", parent=self.id, parent_name=self.workspace.root.name
        )

        # The child's slice settles into the parent exactly once — normally
        # below when the child thread finishes, or parent-side via _abandon()
        # when close() gives up on a straggler. Whoever gets there first wins;
        # the loser's call is a no-op, so a child that finishes after the
        # parent's session_end snapshot can never mutate the parent's budget.
        _settle_lock = threading.Lock()
        _settled = False

        def _settle() -> None:
            nonlocal _settled
            with _settle_lock:
                if _settled:
                    return
                _settled = True
            self.budget.absorb(child_budget)

        def _abandon() -> None:
            """Parent-side force-stop for a child that outlived the shutdown
            join: close the child session now (its kernel process is killed via
            the reap escalation, and in-flight broker calls get their aborted
            audit outcome before the parent's session_end) and settle its
            spend as of this moment."""
            child.close()
            _settle()

        def _run() -> SpawnResult:
            try:
                report = child.run(task)
            except Exception as exc:  # noqa: BLE001 — a failed child is data, not a crash
                report = f"(spawn failed: {exc!r})"
            finally:
                child.close()
                _settle()

            from ..obs.transcript import session_digest

            digest = session_digest(child_root)
            outcome = digest["outcome"]
            return SpawnResult(
                ok=outcome == "answered",
                report=report,
                outcome=outcome,
                session=name,
                spent_usd=round(child_budget.spent_usd, 6),
                steps=digest["steps"],
            )

        future: Future = Future()

        def _runner() -> None:
            try:
                future.set_result(_run())
            except BaseException as exc:  # noqa: BLE001 — the future is the only report channel
                future.set_exception(exc)

        # Daemon: an abandoned child must not block interpreter exit; normal
        # teardown goes through SpawnCapability.shutdown() in close().
        threading.Thread(target=_runner, name=name, daemon=True).start()
        return ChildRun(name=name, future=future, budget=child_budget, abandon=_abandon)

    def _refresh_index(self) -> None:
        """Bring the session index up to date (all remembered roots plus this
        session's parent dir). Fail-open — a broken index never blocks a session."""
        if self.index_db is None:
            return
        try:
            from ..obs.index import update_index

            update_index(
                self.index_db, [self.workspace.root.parent], skills_dir=self.skills_dir
            )
        except Exception:  # noqa: BLE001 — the index is a cache, never a blocker
            import logging

            logging.getLogger("pyharness.index").debug(
                "index refresh failed", exc_info=True
            )

    def _render_history_preamble(self) -> str:
        """A few ambient lines from the index — recent sessions and skill trust —
        appended to the system preamble so the agent starts oriented in its own
        past instead of having to remember to look. Empty without an index."""
        sessions: list = []
        skills: list = []
        if self.index_db is not None and self.index_db.exists():
            try:
                from ..obs.index import query

                sessions = query(
                    self.index_db,
                    "SELECT name, task, outcome, cost_usd FROM sessions "
                    "ORDER BY started DESC LIMIT 5",
                )
                skills = query(
                    self.index_db,
                    "SELECT name, verified, uses, worked, failed FROM skills "
                    "ORDER BY last_used DESC LIMIT 10",
                )
            except Exception:  # noqa: BLE001
                pass
        lines: list[str] = []
        if sessions:
            lines.append("## Recent sessions (query more with stats())")
            for s in sessions:
                task = (s["task"] or "")[:70]
                lines.append(
                    f"- {s['name']}: {task!r} — {s['outcome']}, ${s['cost_usd'] or 0:.2f}"
                )
        if skills:
            lines.append("## Skills (trust from real runs; details via describe_tool)")
            for s in skills:
                state = "verified" if s["verified"] else "unverified"
                lines.append(
                    f"- {s['name']}: {state}, {s['worked']}/{s['uses']} runs worked"
                )
        try:
            from ..lessons import established

            lessons = established(self.lessons_path)
        except Exception:  # noqa: BLE001
            lessons = []
        if lessons:
            lines.append("## Lessons (facts observed across sessions)")
            lines += [f"- {e['text']}" for e in lessons[-10:]]
        return "\n".join(lines)

    def reflect(self) -> str | None:
        """Run the post-session reflection pass (see reflect.py): a separate
        cheap-model read of this session's trace that may propose one durable
        improvement — a new skill, a delta edit, a lesson, or nothing. Skill
        writes go through the broker (same approval gate as the agent's own).
        Returns a one-line summary, or None. Never raises."""
        from ..reflect import reflect as run_reflection

        return run_reflection(self)

    def run(self, task: str) -> str:
        self.trace.record("task", task)
        with telemetry.turn_span(task, self.id) as span:
            try:
                answer = self.agent.run(task, self.messages)
            except Exception as exc:
                self.trace.record("error", f"{type(exc).__name__}: {exc}")
                raise
            finally:
                self.trace.record(
                    "budget",
                    spent_usd=self.budget.spent_usd,
                    calls=self.budget.calls,
                    by_model=self.budget.by_model,
                )
                telemetry.record_budget(
                    span, spent_usd=self.budget.spent_usd, calls=self.budget.calls
                )
            self.trace.record("answer", answer)
            return answer

    def close(self) -> None:
        """Tear down session resources (the child process, if out-of-process,
        and any MCP server connections). Idempotent and step-isolated: each
        teardown step runs in its own try/except so one failure cannot skip
        the rest — failures are logged, never raised or silently dropped."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        import logging

        log = logging.getLogger("pyharness.session")

        def step(name: str, fn) -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001 — every remaining step must still run
                log.warning("close: %s failed", name, exc_info=True)

        if self._spawn_cap is not None:
            # Cooperatively stop children still running (their budget slice
            # drops to spent, ending them at the next step boundary) so the
            # session_end snapshot below includes their settled spend. A child
            # that outlives the join is abandoned: record it and force-settle
            # it NOW — before the snapshot — so nothing mutates the parent
            # budget or appends child-close audit records after session_end.
            def shutdown_children() -> None:
                for run in self._spawn_cap.shutdown():
                    self.trace.record("spawn_abandoned", child=run.name)
                    if run.abandon is not None:
                        step(f"abandon child {run.name}", run.abandon)

            step("spawn shutdown", shutdown_children)
        # Broker calls still executing in orphaned threads get their audit
        # outcome record ({ok: false, error: "aborted"}) now, best-effort,
        # so the chain shows how they ended instead of omitting them.
        step("abort in-flight actions", self.broker.abort_inflight)
        step(
            "session_end snapshot",
            lambda: self.trace.record(
                "session_end",
                session_id=self.id,
                spent_usd=self.budget.spent_usd,
                calls=self.budget.calls,
                by_model=self.budget.by_model,
            ),
        )
        # Fold this session into the index for the next one.
        step("index refresh", self._refresh_index)
        if hasattr(self.kernel, "close"):
            step("kernel", self.kernel.close)
        step("http sessions", self.http.close_all)
        step("browser sessions", self.browser.close_all)
        step("tool registry", self.registry.close)
