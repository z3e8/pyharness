"""Out-of-process kernel: agent code runs in a child process, every capability
call crosses IPC back to the parent's broker. The agent code in these cells is
byte-for-byte what the in-process kernel runs — only the execution boundary moves.
"""

import json
from pathlib import Path

import pytest

from pyharness import Budget, Policy, Workspace
from pyharness.audit import AuditLog
from pyharness.broker import Broker
from pyharness.broker.capabilities import (
    AgentsCapability,
    FilesCapability,
    ToolsCapability,
)
from pyharness.broker.remote import RemoteKernel
from pyharness.broker.remote.sandbox import macos_sandbox_supported
from pyharness.llm.client import Completion
from pyharness.tools.registry import Registry

requires_sandbox = pytest.mark.skipif(
    not macos_sandbox_supported(), reason="OS sandbox only built for macOS"
)


class StubLLM:
    """A worker that always succeeds, so map_agents results are deterministic."""

    def complete(self, *, system, messages, tier="cheap", tools=None, max_tokens=None):
        return Completion(text="worked", tool_calls=[], content=[])


def _broker(tmp_path, policy=None, *, with_agents=False):
    ws = Workspace(tmp_path)
    broker = Broker(policy or Policy(), AuditLog(tmp_path / "audit.jsonl"), Budget())
    broker.register(FilesCapability(ws))
    broker.register(ToolsCapability(Registry()))
    if with_agents:
        broker.register(AgentsCapability(StubLLM()))
    return broker


@pytest.fixture
def kernel_factory():
    kernels = []

    def make(broker):
        k = RemoteKernel(broker)
        kernels.append(k)
        return k

    yield make
    for k in kernels:
        k.close()


def test_variables_persist_across_cells(kernel_factory, tmp_path):
    kernel = kernel_factory(_broker(tmp_path))
    assert kernel.run("n = 41") == "(no output)"
    # Pure compute stays in the child; only the print returns.
    assert kernel.run("print(n + 1)") == "42"


def test_capability_routes_to_parent_broker_and_audits(kernel_factory, tmp_path):
    broker = _broker(tmp_path)
    kernel = kernel_factory(broker)
    out = kernel.run("write('note.txt', 'data')\nprint(read('note.txt'))")
    assert out == "data"
    # The side effect happened parent-side: file on disk + audit entry.
    assert (Workspace(tmp_path).dir / "note.txt").read_text() == "data"
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    actions = [json.loads(line).get("action") for line in lines]
    assert "files.write" in actions and "files.read" in actions


def test_policy_denial_surfaces_as_traceback(kernel_factory, tmp_path):
    broker = _broker(tmp_path, policy=Policy(deny={"files"}))
    kernel = kernel_factory(broker)
    out = kernel.run("write('x.txt', 'y')")
    assert "PermissionDenied" in out


def test_policy_denial_is_catchable_by_agent_code(kernel_factory, tmp_path):
    broker = _broker(tmp_path, policy=Policy(deny={"files"}))
    kernel = kernel_factory(broker)
    out = kernel.run(
        "from pyharness.broker import PermissionDenied\n"
        "try:\n"
        "    write('x.txt', 'y')\n"
        "except PermissionDenied:\n"
        "    print('blocked')\n"
    )
    assert out == "blocked"


def test_use_tool_remote_module(kernel_factory, tmp_path):
    kernel = kernel_factory(_broker(tmp_path))
    out = kernel.run("calc = use_tool('calc')\nprint(calc.evaluate('2 + 3 * 4'))")
    assert out == "14"
    # The tool call routed through the broker as tools.invoke.
    actions = [
        json.loads(line).get("action")
        for line in (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    ]
    assert "tools.invoke" in actions


def test_map_agents_returns_results(kernel_factory, tmp_path):
    kernel = kernel_factory(_broker(tmp_path, with_agents=True))
    out = kernel.run(
        "rs = map_agents(['a', 'b', 'c'])\n"
        "print(sum(r.ok for r in rs), rs[0].value)"
    )
    assert out == "3 worked"


@requires_sandbox
@pytest.mark.parametrize("where", ["home", "temp"])
def test_sandbox_denies_filesystem_writes(kernel_factory, tmp_path, where):
    # The child reaches around the broker and writes straight to disk. The OS
    # sandbox denies *every* write — both a sensitive user path (home) and an
    # ephemeral one (temp). The temp case matters most: such a file would be
    # invisible to the agent and the human and bypass the broker, so it is denied
    # deliberately rather than waved through as "just scratch".
    kernel = kernel_factory(_broker(tmp_path))
    # tmp_path lives under the OS temp root (/var/folders); home is a real file.
    escape = (Path.home() / ".pyharness_sandbox_escape_test") if where == "home" else (tmp_path / "escape.txt")
    out = kernel.run(
        f"try:\n"
        f"    open({str(escape)!r}, 'w').write('x')\n"
        f"    print('WROTE')\n"
        f"except OSError as e:\n"
        f"    print('denied', e.errno)\n"
    )
    assert out.startswith("denied")
    assert not escape.exists()


@requires_sandbox
def test_sandbox_denies_outbound_network(kernel_factory, tmp_path):
    kernel = kernel_factory(_broker(tmp_path))
    out = kernel.run(
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n"
        "    print('CONNECTED')\n"
        "except OSError:\n"
        "    print('denied')\n"
    )
    assert out == "denied"


@requires_sandbox
def test_sandbox_still_allows_broker_writes_and_compute(kernel_factory, tmp_path):
    # The sandbox blocks the child's own writes, not the broker's: `write()`
    # executes parent-side (unsandboxed), and pure compute is untouched.
    kernel = kernel_factory(_broker(tmp_path))
    out = kernel.run(
        "write('note.txt', 'data')\nprint(read('note.txt'), sum(range(5)))"
    )
    assert out == "data 10"
    assert (Workspace(tmp_path).dir / "note.txt").read_text() == "data"


def test_child_restarts_after_crash(kernel_factory, tmp_path):
    kernel = kernel_factory(_broker(tmp_path))
    kernel.run("keep = 1")
    # A hard exit kills the child mid-session; state is lost (no durable resume).
    crashed = kernel.run("import os; os._exit(1)")
    assert "died" in crashed
    # The next cell transparently starts a fresh child — but `keep` is gone.
    assert "NameError" in kernel.run("print(keep)")
    assert kernel.run("print('alive')") == "alive"
