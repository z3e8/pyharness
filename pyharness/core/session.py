from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..audit import AuditLog
from ..broker.capabilities import (
    AgentsCapability,
    FilesCapability,
    LLMCapability,
    SearchCapability,
    ShellCapability,
    ToolsCapability,
    WebCapability,
)
from ..broker.dispatch import Approver, Broker
from ..broker.remote import RemoteKernel
from ..budget import Budget
from ..llm.client import AnthropicLLM
from ..security.policy import Policy
from ..security.vault import Vault
from ..tools.registry import Registry
from .agent import Agent
from .kernel import Kernel
from .workspace import Workspace


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
    ):
        self.workspace = Workspace(root)
        self.budget = budget or Budget()
        self.audit = AuditLog(self.workspace.root / "audit.jsonl")
        self.llm = llm or AnthropicLLM(budget=self.budget)
        self.policy = policy or Policy()
        self.vault = vault or Vault()
        self.registry = registry or Registry()
        if mcp_config is not None:
            from ..tools.mcp import mount_config

            mount_config(self.registry, mcp_config, vault=self.vault)

        self.broker = Broker(self.policy, self.audit, self.budget, approver=approver)
        for capability in (
            FilesCapability(self.workspace),
            ShellCapability(self.workspace),
            SearchCapability(self.workspace),
            WebCapability(self.llm, vault=self.vault),
            LLMCapability(self.llm),
            AgentsCapability(self.llm),
            ToolsCapability(self.registry),
        ):
            self.broker.register(capability)

        # In-process: the broker's proxies run directly in the host namespace.
        # Out-of-process: agent code runs in a restricted child and every call
        # crosses IPC back to the same broker (see broker/remote).
        self.kernel = (
            RemoteKernel(self.broker) if out_of_process else Kernel(self.broker.namespace())
        )
        self.agent = Agent(self.llm, self.kernel, self.budget, on_event=on_event)
        self.messages: list[dict] = []

    def run(self, task: str) -> str:
        return self.agent.run(task, self.messages)

    def close(self) -> None:
        """Tear down session resources (the child process, if out-of-process,
        and any MCP server connections)."""
        if hasattr(self.kernel, "close"):
            self.kernel.close()
        self.registry.close()
