from __future__ import annotations

from pathlib import Path
from typing import Callable
from uuid import uuid4

from .. import telemetry
from ..audit import AuditLog
from ..broker.capabilities import (
    AgentsCapability,
    BrowserCapability,
    FilesCapability,
    HistoryCapability,
    HttpSessionCapability,
    LLMCapability,
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
from ..broker.dispatch import Approver, Broker
from ..broker.remote import RemoteKernel
from ..budget import Budget
from ..llm.client import PROVIDER_SECRET_ENV, AnthropicLLM
from ..security.policy import Policy
from ..security.vault import PASSPHRASE_ENV, Vault
from ..tools.registry import Registry
from ..trace import TraceLog
from .agent import Agent
from .kernel import Kernel
from .media import MediaOutbox
from .session_venv import SessionVenv
from .workspace import Workspace


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
        registry: Registry | None = None,
        approver: Approver | None = None,
        on_event: Callable[[str, str], None] | None = None,
        out_of_process: bool = False,
        mcp_config: str | Path | dict | None = None,
        skills_dir: str | Path | None = None,
        index_db: str | Path | None = None,
    ):
        telemetry.setup_telemetry()
        self.id = uuid4().hex
        self.workspace = Workspace(root)
        self.budget = budget or Budget()
        self.audit = AuditLog(self.workspace.root / "audit.jsonl")
        self.trace = TraceLog(self.workspace.root / "trace.jsonl")
        self.llm = llm or AnthropicLLM(budget=self.budget)
        # Saving a skill (agent-authored code that auto-loads later) and
        # installing packages sign off at author time; any state-changing HTTP
        # request is gated per-call (reads stay free).
        def _look_after_injected_secret(action: str, args: tuple, kwargs: dict) -> bool:
            """Gate a screenshot-to-model once a secret was typed into the page —
            pixels carry the credential into the model's context where text
            redaction can't reach. Reads self.browser at call time (set below)."""
            if action != "browser.look":
                return False
            sid = args[0] if args else kwargs.get("session_id")
            return sid is not None and self.browser.has_injected_secrets(sid)

        self.policy = policy or Policy(
            require_approval={
                "skills.save_skill",
                "packages.install",
                # State-changing browser actions; navigation and reads stay free.
                *MUTATING_BROWSER_ACTIONS,
            },
            approve_if=[_is_mutating_http, _look_after_injected_secret],
        )
        self.vault = vault or Vault.from_env()
        self.registry = registry or Registry()
        if mcp_config is not None:
            from ..tools.mcp import mount_config

            mount_config(self.registry, mcp_config, vault=self.vault)

        # Skills are learned tools persisted on disk; load any from prior sessions
        # (or hand-authored by the user) so they reload here. Cross-session by
        # design, so the root defaults outside the per-session workspace.
        from ..tools.skills import load_skills

        self.skills_dir = Path(skills_dir or "~/.pyharness/skills").expanduser()
        load_skills(self.registry, self.skills_dir)

        # The session index (direction 5) — a derived SQLite view over past
        # sessions' JSONL, feeding the `stats`/`inspect_session` builtins and the
        # preamble. None (the default) leaves those dataless; the CLI passes the
        # global DB. Refresh is fail-open: the index is a cache, never a blocker.
        self.index_db = Path(index_db).expanduser() if index_db else None
        self._refresh_index()

        # Variables agent-controlled code must never see: env-backed secrets
        # (the vault's prefix), the file-vault passphrase, and the provider API
        # keys the parent uses to call the LLM. Stripped from the child's
        # environment and from any shell subprocess.
        secret_prefixes = (self.vault.env_prefix,)
        secret_names = (PASSPHRASE_ENV, *PROVIDER_SECRET_ENV)

        self.session_venv = SessionVenv()
        self.broker = Broker(self.policy, self.audit, self.budget, approver=approver)
        # Web fetch is a thin wrapper over the stateful HTTP capability, so the
        # latter is built first and shared with WebCapability.
        self.http = HttpSessionCapability(self.workspace, vault=self.vault)
        # One outbox shared by the browser (fills it via look()) and the agent
        # loop (drains it into image content blocks after each cell).
        self.media = MediaOutbox()
        self.browser = BrowserCapability(self.workspace, vault=self.vault, media=self.media)
        # Core builtins — the agent's own body (workspace, shell, delegation,
        # reflection) plus the tool-discovery entrypoint. Always in scope.
        for capability in (
            FilesCapability(self.workspace),
            ShellCapability(
                self.workspace,
                secret_env_prefixes=secret_prefixes,
                secret_env_names=secret_names,
            ),
            SearchCapability(self.workspace),
            LLMCapability(self.llm),
            AgentsCapability(self.llm),
            ToolsCapability(self.registry),
            SecretsCapability(self.vault),
            # Skill authorship/outcomes land in the trace (not the display stream)
            # so the session index can attribute skill uses to sessions.
            SkillsCapability(self.registry, self.skills_dir, on_event=self.trace.record),
            HistoryCapability(self.audit),
            ObservabilityCapability(self.index_db, self.llm),
        ):
            self.broker.register(capability)

        # External-reaching capabilities — the web, a browser, HTTP APIs, the
        # package index. Registered for gating but NOT surfaced as builtins; the
        # agent discovers and loads them through the tool registry
        # (search_tools -> describe_tool -> use_tool), same path as MCP tools and
        # learned skills. One coherent, discoverable surface for everything
        # external; gating is identical to a builtin's.
        tool_caps = [
            (WebCapability(self.llm, http=self.http),
             "Read the web: search_results (a raw ranked list to fan out over), and fetch a single URL.",
             "web", ("web", "http", "fetch", "search", "results", "url", "download", "browse", "internet")),
            (self.http,
             "Stateful HTTP: open a session (cookies persist), POST/PUT, upload files.",
             "web", ("http", "request", "post", "put", "session", "api", "cookie", "upload", "rest")),
            (self.browser,
             "Drive a headless browser: navigate, snapshot the page (element refs), click/fill/select/press by ref or selector, upload, look (a screenshot the model sees), read the page.",
             "web", ("browser", "playwright", "snapshot", "ref", "aria", "click", "fill", "select", "press", "upload", "look", "screenshot", "page", "dom", "headless", "form")),
            (PackagesCapability(self.session_venv),
             "Install Python packages into the session for later import.",
             "packages", ("install", "pip", "package", "dependency", "library", "import")),
        ]
        for capability, summary, category, keywords in tool_caps:
            self.broker.register(capability, core=False)
            self.registry.register(
                self.broker.as_tool_module(capability.name, summary=summary),
                source="core", name=capability.name,
                keywords=keywords, category=category,
            )

        # In-process: the broker's proxies run directly in the host namespace.
        # Out-of-process: agent code runs in a restricted child and every call
        # crosses IPC back to the same broker (see broker/remote).
        self.kernel = (
            RemoteKernel(
                self.broker,
                secret_env_prefixes=secret_prefixes,
                secret_env_names=secret_names,
                venv=self.session_venv,
                workspace=self.workspace,
            )
            if out_of_process
            else Kernel(self.broker.namespace())
        )

        def on_event_traced(kind: str, text: str, **extra) -> None:
            # llm_token fires once per streaming chunk — too frequent for trace
            if kind != "llm_token":
                self.trace.record(kind, text, **extra)
            if on_event is not None:
                on_event(kind, text)

        self.agent = Agent(
            self.llm,
            self.kernel,
            self.budget,
            workspace_root=self.workspace.dir,
            on_event=on_event_traced,
            media=self.media,
            preamble_extra=self._render_history_preamble(),
        )
        self.messages: list[dict] = []
        self._closed = False
        self.trace.record("session_start", session_id=self.id, root=str(self.workspace.root))

    def _refresh_index(self) -> None:
        """Bring the session index up to date (all remembered roots plus this
        session's parent dir). Fail-open — a broken index never blocks a session."""
        if self.index_db is None:
            return
        try:
            from ..index import update_index

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
        if self.index_db is None or not self.index_db.exists():
            return ""
        try:
            from ..index import query

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
            return ""
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
        return "\n".join(lines)

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
        and any MCP server connections)."""
        if not self._closed:
            self._closed = True
            self.trace.record(
                "session_end",
                session_id=self.id,
                spent_usd=self.budget.spent_usd,
                calls=self.budget.calls,
                by_model=self.budget.by_model,
            )
            self._refresh_index()  # fold this session into the index for the next one
        if hasattr(self.kernel, "close"):
            self.kernel.close()
        self.http.close_all()
        self.browser.close_all()
        self.registry.close()
