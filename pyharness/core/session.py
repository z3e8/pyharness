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
    HttpSessionCapability,
    LLMCapability,
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
from ..llm.client import AnthropicLLM
from ..security.policy import Policy
from ..security.vault import Vault
from ..tools.registry import Registry
from ..trace import TraceLog
from .agent import Agent
from .kernel import Kernel
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
        self.policy = policy or Policy(
            require_approval={
                "skills.save_skill",
                "packages.install",
                # State-changing browser actions; navigation and reads stay free.
                *MUTATING_BROWSER_ACTIONS,
            },
            approve_if=[_is_mutating_http],
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

        # Variables agent-controlled code must never see: env-backed secrets
        # (the vault's prefix) and the file-vault passphrase. Stripped from the
        # child's environment and from any shell subprocess.
        secret_prefixes = (self.vault.env_prefix,)

        self.session_venv = SessionVenv()
        self.broker = Broker(self.policy, self.audit, self.budget, approver=approver)
        # Web fetch is a thin wrapper over the stateful HTTP capability, so the
        # latter is built first and shared with WebCapability.
        self.http = HttpSessionCapability(self.workspace, vault=self.vault)
        self.browser = BrowserCapability(self.workspace, vault=self.vault)
        for capability in (
            FilesCapability(self.workspace),
            ShellCapability(self.workspace, secret_env_prefixes=secret_prefixes),
            SearchCapability(self.workspace),
            self.http,
            self.browser,
            WebCapability(self.llm, http=self.http),
            LLMCapability(self.llm),
            AgentsCapability(self.llm),
            ToolsCapability(self.registry),
            SecretsCapability(self.vault),
            SkillsCapability(self.registry, self.skills_dir),
            PackagesCapability(self.session_venv),
        ):
            self.broker.register(capability)

        # In-process: the broker's proxies run directly in the host namespace.
        # Out-of-process: agent code runs in a restricted child and every call
        # crosses IPC back to the same broker (see broker/remote).
        self.kernel = (
            RemoteKernel(self.broker, secret_env_prefixes=secret_prefixes, venv=self.session_venv)
            if out_of_process
            else Kernel(self.broker.namespace())
        )

        def on_event_traced(kind: str, text: str, **extra) -> None:
            # llm_token fires once per streaming chunk — too frequent for trace
            if kind != "llm_token":
                self.trace.record(kind, text, **extra)
            if on_event is not None:
                on_event(kind, text)

        self.agent = Agent(self.llm, self.kernel, self.budget, on_event=on_event_traced)
        self.messages: list[dict] = []

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
        if hasattr(self.kernel, "close"):
            self.kernel.close()
        self.http.close_all()
        self.browser.close_all()
        self.registry.close()
