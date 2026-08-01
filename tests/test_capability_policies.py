"""Cross-cutting policy enumeration: every capability, every policy, no silent
opt-outs.

The 2026-07-24 security recon found that every verified gap sat at the same
seam: a capability was added without being taught about a cross-cutting policy
that already existed (`allowed_hosts` knew about 3 of the capabilities,
`packages.install` skipped the sandbox wrap `shell.bash` has, MCP's transport
skipped the scope argument). Dispatch is centralized and holds; containment is
implemented per capability, so every new capability is a chance to silently opt
out of one.

These tests are the structural fix. For each cross-cutting policy they
enumerate every capability **from the live broker registry** — never from a
hand-written list of names — and assert an explicit classification:

- a capability either *participates* in the policy (and the test asserts the
  enforcement mechanism is actually wired), or
- it is a *named exemption with a written rationale*.

The classification tables below are therefore deliverables in their own right:
each exemption is a stated design boundary (the raw material for the public
threat-model page), and an unclassified newcomer fails every one of these tests
until someone decides — in writing — how it relates to each policy.

Policies covered: host scoping (`allowed_hosts`), parent-side sandbox wrapping,
dispatch mediation (everything the agent can reach is a broker proxy), approval
classification (preview/grant hooks), and secret-sink wiring. What each test
does and does not prove is stated in its docstring; the honest limits matter as
much as the assertions.
"""

from __future__ import annotations

import ast
import sys
from types import ModuleType

import pytest

from pyharness.core.session import Session
from pyharness.security.vault import Vault


@pytest.fixture(scope="module")
def sess(tmp_path_factory):
    """One full parent session, host-scoped so scope threading is observable.

    A parent session registers every capability there is (a spawned child holds
    a subset of the same set), so its broker's op table *is* the live registry
    these tests enumerate. In-process kernel: these tests inspect wiring and
    never run agent code."""
    session = Session(
        tmp_path_factory.mktemp("policy-session"),
        unsafe_in_process=True,
        allowed_hosts=["example.com"],
        skills_dir=tmp_path_factory.mktemp("skills"),
    )
    yield session
    session.close()


def _caps(sess) -> dict[str, object]:
    """The live capability registry: name -> registered instance. Read from the
    broker's own table (the single source of truth every dispatch consults), so
    a capability cannot be present at runtime yet absent here."""
    return dict(sess.broker._capabilities)


def _assert_partition(all_names: set[str], *groups: set[str]) -> None:
    """The classification discipline: the groups must cover every registered
    capability exactly once. An unclassified newcomer or a stale entry fails
    loudly with the offending names."""
    union: set[str] = set()
    for group in groups:
        overlap = union & group
        assert not overlap, f"capabilities classified twice: {sorted(overlap)}"
        union |= group
    assert union == all_names, (
        f"unclassified capabilities: {sorted(all_names - union)}; "
        f"stale classifications: {sorted(union - all_names)} — "
        "classify each capability for this policy (participant or named, "
        "written exemption) before registering it"
    )


def _assert_rationales(exemptions: dict) -> None:
    for key, rationale in exemptions.items():
        assert isinstance(rationale, str) and len(rationale) >= 40, (
            f"exemption {key!r} needs a real written rationale, not a stub — "
            "it is published as a stated design boundary"
        )


# ---------------------------------------------------------------------------
# Policy 1 — host scoping (`allowed_hosts`)
# ---------------------------------------------------------------------------

# Capabilities that enforce the session host scope, with an accessor reading
# the *effective* scope off the live instance — so a capability that stops
# threading the session's scope (the MCP-over-HTTP bug, before it was fixed)
# fails the equality below even though it still has the attribute.
HOST_SCOPE_ENFORCED = {
    "http": lambda cap: cap.allowed_hosts,
    "web": lambda cap: cap.http.allowed_hosts,  # scope lives on the shared substrate
    "browser": lambda cap: cap.allowed_hosts,
    "tools": lambda cap: cap._allowed_hosts,  # threaded into every MCP mount
}

# Named exemptions: capabilities the host scope deliberately does not govern.
# Each rationale is written for a skeptical outsider — these are published as
# design boundaries, not TODOs.
HOST_SCOPE_EXEMPT = {
    "files": "No network reach: reads/writes/edits are confined to the session "
    "workspace directory. There is no host to scope.",
    "search": "Text search over the session workspace only; never touches the network.",
    "llm": "Talks only to the configured model API. The endpoint is fixed by "
    "harness configuration, not chosen by agent code, so there is no "
    "agent-directed egress to scope — and scoping it would sever the agent's "
    "own reasoning loop. Spend is bounded by the budget wall instead.",
    "obs": "Read-only queries over the local session index (SQLite opened "
    "read-only); its summarize path uses the same fixed model API as `llm`.",
    "history": "Read-only view of the session's own audit log on local disk.",
    "skills": "Writes skill files under the local skills directory; no network reach.",
    "notify": "Output-only message to the human via the local event stream and "
    "a desktop notification helper; nothing leaves the box over the network.",
    "vault": "Returns secret *names* and mints vault entries parent-side; the "
    "values leave the box only through other capabilities' injection points "
    "(http/browser auth), which are scope-enforced and approval-gated there.",
    "inbox": "Connects only to the operator-configured IMAP server "
    "(PYHARNESS_IMAP_* in the environment); agent code cannot choose the host, "
    "so there is no agent-directed egress to scope. Reads are PEEK-only.",
    "shell": "Deliberate boundary: commands are outside the host scope's remit. "
    "Containment is the OS sandbox, which denies outbound network wholesale "
    "(macOS Seatbelt / Linux Landlock+seccomp) rather than per host; approval "
    "is an audit and review gate on top, not the containment. On an "
    "unsandboxed platform (explicit PYHARNESS_ALLOW_UNSANDBOXED opt-in only) "
    "approval is all that is left.",
    "packages": "Deliberate boundary: pip must reach the package index, which "
    "is outside any task's host scope by nature. Contained instead by per-call "
    "approval and the install sandbox profile (network allowed, writes "
    "confined to the venv plus scratch, $HOME read jail).",
    "spawn": "Makes no requests itself; it *threads* the scope down — a "
    "child's allowed_hosts is normalized at spawn, shown to the approving "
    "human, and wired into the child session (covered by "
    "test_spawn.py::test_child_session_is_host_scoped).",
}

# Boundaries *inside* the enforcing capabilities — enforced-as-known, so the
# threat-model page states them and a silent behavior change trips a test.
HOST_SCOPE_BOUNDARIES = {
    "browser subresources": "Scope applies to main-frame navigations only; "
    "subresource/iframe loads are scope-exempt (blocking CDNs breaks most "
    "pages) but stay under the SSRF guard. Asserted by test_security_hardening"
    ".py::test_browser_route_handler_scopes_main_frame_navigations.",
    "websockets": "No enforcement point exists at all: context.route('**/*') "
    "does not intercept WebSocket traffic and page.route_web_socket is unused "
    "repo-wide. Asserted still-open below.",
    "local MCP servers": "A command-run MCP server is a local process, not a "
    "host — outside the scope's remit, behind the tools.add_mcp_server "
    "approval gate (never grantable).",
}


def test_host_scope_classification_covers_every_capability(sess):
    """Every registered capability either enforces the session host scope (and
    the live instance provably holds the session's scope, not a stale or absent
    one) or is a named exemption with a written rationale."""
    caps = _caps(sess)
    _assert_partition(set(caps), set(HOST_SCOPE_ENFORCED), set(HOST_SCOPE_EXEMPT))
    _assert_rationales(HOST_SCOPE_EXEMPT)
    _assert_rationales(HOST_SCOPE_BOUNDARIES)

    # The fixture session is genuinely scoped — the equality below can never
    # pass vacuously as None == None.
    assert sess.allowed_hosts == frozenset({"example.com"})
    for name, effective_scope in HOST_SCOPE_ENFORCED.items():
        assert effective_scope(caps[name]) == sess.allowed_hosts, (
            f"capability {name!r} is classified as host-scope enforcing but "
            "the live instance does not hold the session's scope — the scope "
            "stopped being threaded through"
        )


def test_websocket_scope_gap_is_still_open_and_named(sess):
    """The WebSocket hole is a *stated* boundary: the browser's route
    interception does not cover WS and no page.route_web_socket handler exists.
    If this assertion ever fails, the gap was closed — celebrate, then update
    HOST_SCOPE_BOUNDARIES, the threat-model page, and delete this test."""
    module = sys.modules[type(_caps(sess)["browser"]).__module__]
    source = open(module.__file__).read()
    assert "route_web_socket" not in source, (
        "browser.py now references route_web_socket — the WebSocket "
        "enforcement gap appears to be closed. Update HOST_SCOPE_BOUNDARIES "
        "and the threat-model docs to match, then remove this test."
    )


# ---------------------------------------------------------------------------
# Policy 2 — parent-side sandbox wrapping
# ---------------------------------------------------------------------------

# Capabilities whose scanned sources are allowed to exec a program parent-side.
# Everything else must show zero exec sites in the scan.
PARENT_EXEC = {"shell", "packages", "notify"}

# Of those, the ones whose agent-controllable exec goes through the OS sandbox
# wrap (broker/remote/sandbox.py). The scan asserts the wrap is referenced in
# the same file as the exec.
SANDBOX_WRAPPED = {"shell", "packages"}

# Exec sites deliberately NOT wrapped, per (capability, module): each is a
# fixed, harness-authored argv with no agent-controlled code path.
UNWRAPPED_EXEC_EXEMPT = {
    ("packages", "pyharness.core.session_venv"): "Venv creation and the "
    "site-packages probe run a fixed harness-authored argv (the venv python "
    "printing sysconfig paths); no agent-controlled argument reaches them. "
    "The agent-controllable exec on this path — `pip install <package>` with "
    "its arbitrary build hooks — is the one wrapped in sandboxed_install_argv.",
    ("notify", "pyharness.broker.capabilities.notify"): "Best-effort desktop "
    "notification via a fixed helper argv (osascript / notify-send); the "
    "agent's message is passed as a single data argument (JSON-escaped for "
    "the osascript literal), never through a shell, and helper failure is "
    "ignored. The helper renders text; it executes nothing agent-authored.",
}

# Capabilities that cause a parent-side exec *outside* the modules this scan
# can see — named so the boundary is stated rather than silently out of frame.
EXEC_OUT_OF_SCAN_REACH = {
    "browser": "Playwright launches the Chromium process parent-side inside "
    "the playwright package, beyond this scan's reach and outside the harness "
    "sandbox profile. Containment for the browser is the per-request route "
    "interception (SSRF guard + host scope on main-frame navigations) plus "
    "approval on every mutating action — a stated boundary, not an oversight.",
    "spawn": "A spawned child's kernel process is created in broker/remote "
    "(host.py), not in the capability module; it starts under the same OS "
    "sandbox as the parent's kernel (Seatbelt / Landlock+seccomp), covered by "
    "test_shell_sandbox.py and test_linux_sandbox.py.",
    "tools": "A local (command-run) MCP server is exec'd parent-side by "
    "tools/mcp/transport.py with the minimal allowlist environment but no "
    "sandbox wrap; mounting one requires human approval and "
    "tools.add_mcp_server is never grantable. Stated boundary: an approved "
    "local MCP server runs with the operator's OS reach.",
}


def _module_files(cap) -> dict[str, str]:
    """The pyharness sources one capability's behavior lives in: its own class
    module plus the modules of its pyharness-typed constructor collaborators
    (one level — e.g. PackagesCapability -> SessionVenv). Returns
    {module_name: source_text}."""
    names = {type(cap).__module__}
    for value in vars(cap).values():
        mod = type(value).__module__
        if mod.startswith("pyharness"):
            names.add(mod)
    return {name: open(sys.modules[name].__file__).read() for name in sorted(names)}


def _references_sandbox_wrap(source: str) -> bool:
    """True when the module genuinely *uses* a sandboxed_*_argv wrap — an AST
    Name reference or an import of one, never a mention in a comment or
    docstring (which is exactly how a removed wrap would keep passing a
    textual check)."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id.startswith("sandboxed_"):
            return True
        if isinstance(node, ast.ImportFrom) and any(
            alias.name.startswith("sandboxed_") for alias in node.names
        ):
            return True
    return False


def _has_exec_sites(source: str) -> bool:
    """AST-level detector for parent-side process execution: subprocess usage
    (module attribute access or `from subprocess import ...`), bare Popen, and
    the os.system/exec*/spawn*/posix_spawn family. AST, not text, so comments
    and docstrings can never false-positive."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            return True
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "subprocess":
                return True
            if node.value.id == "os" and (
                node.attr == "system"
                or node.attr.startswith(("exec", "spawn", "posix_spawn"))
            ):
                return True
        if isinstance(node, ast.Name) and node.id == "Popen":
            return True
    return False


def test_sandbox_wrap_classification_covers_every_capability(sess):
    """Anything that execs a program parent-side goes through the OS sandbox
    wrap, or is a named fixed-argv exemption. The detector walks each
    capability's own module plus its pyharness collaborators, so the next
    capability that shells out (the `packages.install` failure mode) is caught
    whether the exec lives in the capability file or one level down. Limits:
    execs buried in third-party packages or deeper harness modules are out of
    scan reach — those are named per capability in EXEC_OUT_OF_SCAN_REACH
    rather than silently out of frame."""
    caps = _caps(sess)
    _assert_partition(set(caps), PARENT_EXEC, set(caps) - PARENT_EXEC)
    assert SANDBOX_WRAPPED <= PARENT_EXEC
    assert set(EXEC_OUT_OF_SCAN_REACH) <= set(caps) - PARENT_EXEC
    _assert_rationales(UNWRAPPED_EXEC_EXEMPT)
    _assert_rationales(EXEC_OUT_OF_SCAN_REACH)

    for name, cap in caps.items():
        hits = {
            mod: source
            for mod, source in _module_files(cap).items()
            if _has_exec_sites(source)
        }
        if name not in PARENT_EXEC:
            assert not hits, (
                f"capability {name!r} gained a parent-side exec site in "
                f"{sorted(hits)} without being classified — wrap it in "
                "sandboxed_*_argv (broker/remote/sandbox.py) or add a written "
                "exemption here"
            )
            continue
        assert hits, (
            f"capability {name!r} is classified PARENT_EXEC but the scan "
            "found no exec site — stale classification"
        )
        for mod, source in hits.items():
            wrapped = _references_sandbox_wrap(source)
            exempt = (name, mod) in UNWRAPPED_EXEC_EXEMPT
            assert wrapped or exempt, (
                f"{name}:{mod} execs parent-side without referencing a "
                "sandboxed_*_argv wrap and without a written exemption"
            )
        if name in SANDBOX_WRAPPED:
            assert any(_references_sandbox_wrap(s) for s in hits.values()), (
                f"capability {name!r} is classified SANDBOX_WRAPPED but no "
                "exec-bearing module references the sandbox wrap"
            )


# ---------------------------------------------------------------------------
# Policy 3 — dispatch mediation
# ---------------------------------------------------------------------------

# How each capability is surfaced to the agent. CORE = bare-name builtins in
# the kernel namespace; TOOL = reached through the registry (search_tools ->
# use_tool). Either way every call must route through Broker.call.
CORE_SURFACE = frozenset(
    {
        "files",
        "search",
        "llm",
        "shell",
        "tools",
        "vault",
        "skills",
        "history",
        "obs",
        "notify",
        "spawn",
    }
)
TOOL_SURFACE = frozenset({"web", "http", "browser", "inbox", "packages"})

# Ops kept off the bare-name surface even on a core capability, with rationale.
HIDDEN_OPS = {
    ("tools", "invoke"): "The internal funnel every tool-module proxy routes "
    "through. It stays broker-gated (reachable via call()/call_op()) but is "
    "never bound as a builtin, so agent code cannot call it by bare name and "
    "sidestep the per-tool proxies.",
}


def _is_dispatch_proxy(func) -> bool:
    """True when a callable is one of the broker's own dispatch closures —
    checked on the code object, which functools.wraps cannot disguise."""
    code = getattr(func, "__code__", None)
    return (
        code is not None
        and code.co_name == "proxy"
        and code.co_filename.endswith("broker/dispatch.py")
    )


def test_kernel_namespace_is_only_broker_proxies(sess):
    """Every bare-name builtin the agent's kernel holds is a Broker._proxy
    closure — no capability slips a raw bound method into the namespace, so
    there is no callable surface that skips policy -> audit -> budget. The
    child's IPC address list (op_names) is the same set."""
    namespace = sess.broker.namespace()
    assert namespace, "empty kernel namespace — nothing registered?"
    for op, func in namespace.items():
        assert _is_dispatch_proxy(func), (
            f"builtin {op!r} is not a broker dispatch proxy — it would "
            "execute without policy/audit/budget mediation"
        )
    assert set(sess.broker.op_names()) == set(namespace)
    assert sess.broker._hidden == set(HIDDEN_OPS), (
        "hidden-op set changed — classify the change in HIDDEN_OPS with a "
        "written rationale"
    )
    _assert_rationales(HIDDEN_OPS)


def test_surface_classification_covers_every_capability(sess):
    """Every capability is deliberately surfaced exactly one way (builtin vs
    tool-registry), the classification matches the broker's live core set, and
    every function reachable through the registry is a broker-gated proxy."""
    caps = _caps(sess)
    _assert_partition(set(caps), set(CORE_SURFACE), set(TOOL_SURFACE))
    assert sess.broker._core == CORE_SURFACE

    from pyharness.tools.registry import _public_functions

    tools_cap = caps["tools"]
    for name, info in sess.registry._tools.items():
        assert info.kind != "mcp", (
            "unexpected MCP entry in the enumeration session — this test "
            "would connect a server; give MCP entries their own handling"
        )
        module = tools_cap.use_tool(name)
        funcs = list(_public_functions(module))
        assert funcs, f"registry tool {name!r} exposes no functions"
        for fname, func in funcs:
            code = func.__code__
            gated = _is_dispatch_proxy(func) or (
                code.co_name == "proxy"
                and code.co_filename.endswith("broker/capabilities/tools.py")
            )
            assert gated, (
                f"{name}.{fname} reached through the registry is not a "
                "broker proxy — a registry tool bypassing dispatch"
            )


def test_use_tool_gates_modules_the_broker_never_registered(sess):
    """A module that enters the registry *without* going through the broker
    (the shape of a learned skill or an installed module) is still mediated:
    use_tool wraps it in proxies that funnel through the gated tools.invoke
    op, and the call lands in the audit chain like any capability call."""
    probe = ModuleType("policy_probe")
    probe.__doc__ = "throwaway module for the dispatch-mediation test"

    def ping(x: int) -> int:
        return x + 1

    ping.__module__ = "policy_probe"
    probe.ping = ping
    sess.registry.register(probe, source="installed", name="policy_probe")

    gated = _caps(sess)["tools"].use_tool("policy_probe")
    assert gated.ping is not ping, "use_tool returned the raw, ungated module"
    assert gated.ping(41) == 42
    audit_text = (sess.workspace.root / "audit.jsonl").read_text()
    assert '"tools.invoke"' in audit_text, (
        "calling a registry module's function left no tools.invoke record in "
        "the audit chain — the call bypassed the broker"
    )


# ---------------------------------------------------------------------------
# Policy 4 — approval classification
# ---------------------------------------------------------------------------

# Capabilities owning gated ops must define preview() so the human sees the
# harness's classification and a secret-safe summary — never dispatch's
# conservative fallback, which would mean an op was gated without anyone
# deciding what the approver should see.
PREVIEWED = frozenset(
    {"shell", "packages", "skills", "tools", "spawn", "vault", "http", "browser", "web"}
)

# Capabilities none of whose ops is ever approval-gated, with the reason there
# is nothing to sign.
NEVER_GATED = {
    "files": "Workspace-local reads/writes/edits; effects stay in the session "
    "workspace and are undoable there. Nothing leaves the box.",
    "search": "Read-only text search over the session workspace.",
    "llm": "Delegation to the configured model API — no side effect beyond "
    "spend, which the budget wall meters and caps independently of approval.",
    "history": "Read-only view of the session's own audit log.",
    "obs": "Read-only queries over the local session index (SQLite opened "
    "read-only, query_only, ATTACH denied).",
    "notify": "Output-only message to the human; carries no interactivity, "
    "fixed title, control characters stripped — it cannot function as (or "
    "forge) an approval prompt, so it needs none itself.",
    "inbox": "Read-only IMAP surface: PEEK-only reads, no send/delete/flag "
    "ops exist at all, and the server is operator-configured. A read leaves "
    "the mailbox untouched.",
}

# Which previewed capabilities are gated by argument-dependent approve_if
# predicates rather than (or in addition to) require_approval action names.
# Documentation only — predicates are opaque callables, so this subset cannot
# be derived mechanically; the load-bearing exhaustive check is the capability
# partition above plus the require_approval cross-check below.
PREDICATE_GATED = frozenset({"http", "web", "browser", "tools"})

# Grantability: capabilities defining a scope() hook can mint standing grants
# (or, for vault, deliberately always decline to). The rest re-prompt on every
# gated call by design.
SCOPE_HOOKED = frozenset({"http", "web", "browser", "tools", "spawn", "vault"})
NEVER_GRANTABLE = {
    "shell": "Every command is its own decision: there is no safe 'class of "
    "commands' a standing grant could cover, so bash re-prompts per call.",
    "packages": "Each install is a distinct supply-chain decision (package "
    "name is the whole risk), so no standing grant exists.",
    "skills": "Saving or editing a skill persists agent-authored code that "
    "auto-loads in later sessions — each write is signed individually.",
}


def test_approval_classification_covers_every_capability(sess):
    """Every capability either defines preview() (it owns gated ops and the
    human sees a deliberate classification) or is a named never-gated
    exemption — and the exemption is cross-checked against the live policy so
    it cannot silently rot. Also: every require_approval action names a real
    registered op (a typo'd gate that gates nothing fails here)."""
    caps = _caps(sess)
    _assert_partition(set(caps), set(PREVIEWED), set(NEVER_GATED))
    _assert_rationales(NEVER_GATED)
    assert PREDICATE_GATED <= PREVIEWED

    for name, cap in caps.items():
        has_preview = callable(getattr(cap, "preview", None))
        assert has_preview == (name in PREVIEWED), (
            f"capability {name!r} preview() presence disagrees with the "
            "classification — decide what the approver should see and "
            "classify it"
        )

    for action in sess.policy.require_approval:
        cap_name, _, op = action.partition(".")
        assert cap_name in caps and op in caps[cap_name].exports(), (
            f"require_approval entry {action!r} does not name a registered "
            "op — it gates nothing"
        )
        assert cap_name in PREVIEWED, (
            f"{action!r} is approval-gated but {cap_name!r} defines no "
            "preview() — the approver would see the conservative fallback, "
            "meaning nobody decided what to show the human"
        )

    for name in NEVER_GATED:
        gated = [a for a in sess.policy.require_approval if a.startswith(name + ".")]
        assert not gated, (
            f"capability {name!r} is classified never-gated but the live "
            f"policy gates {gated} — reclassify it and give it a preview()"
        )


def test_grant_scope_classification_covers_every_capability(sess):
    """Grantability is a decision, not a default: every previewed capability
    either defines the scope() hook that keys standing grants or is a named
    always-re-prompt exemption. Ungated capabilities need neither."""
    caps = _caps(sess)
    _assert_partition(set(PREVIEWED), set(SCOPE_HOOKED), set(NEVER_GRANTABLE))
    _assert_rationales(NEVER_GRANTABLE)
    for name in PREVIEWED:
        has_hook = callable(getattr(caps[name], "scope", None))
        assert has_hook == (name in SCOPE_HOOKED), (
            f"capability {name!r} scope() presence disagrees with the "
            "grantability classification"
        )
    # vault defines the hook precisely to always decline: minting an identity
    # is never covered by a standing grant.
    assert caps["vault"].scope("create_login", (), {}) is None


# ---------------------------------------------------------------------------
# Policy 5 — secret-sink wiring
# ---------------------------------------------------------------------------

# Capabilities that resolve vault secrets into live request/connection state
# must mirror every mask into the session-wide sink, so surfaces that outlive
# the injection context (audited exception reprs, returned cell output) can
# redact anything the session ever resolved.
SINK_MIRRORED = frozenset({"http", "browser", "inbox"})

# Capabilities that hold the vault without resolving values into returned
# content, with the reason no mirror is needed.
VAULT_HOLDING_EXEMPT = {
    "vault": "Returns secret *names* only; create_login returns the signup "
    "email (non-secret by contract) and stores the generated password without "
    "ever returning it. No resolved secret value enters a result.",
    "tools": "Resolves secret: refs only into an MCP server's env/headers "
    "parent-side at mount; the values are never returned to agent code. "
    "Stated boundary: what an external MCP server does with (or echoes of) "
    "its own credential is outside the harness's reach.",
}


def test_secret_sink_classification_covers_every_capability(sess):
    """Detector, not a hand list: any capability instance holding a Vault is
    made to declare itself — either its sink mirror is provably the session
    sink, or it carries a written exemption. A new capability that takes the
    vault without wiring the mirror (the open per-capability-discipline seam
    from the 2026-07-28 recon note) fails here. Honest limit: this asserts the
    *wiring*, not the deep property that every success result is masked — that
    still relies on each capability routing returned text through its sink,
    which cannot be asserted generically without invoking every op against
    live secrets."""
    caps = _caps(sess)
    vault_holding = {
        name
        for name, cap in caps.items()
        if any(isinstance(value, Vault) for value in vars(cap).values())
    }
    assert vault_holding == SINK_MIRRORED | set(VAULT_HOLDING_EXEMPT), (
        "capabilities holding the vault changed — classify each newcomer as "
        "sink-mirrored (and wire sink_mirror=session.secret_sink) or as a "
        "written exemption: "
        f"{sorted(vault_holding ^ (SINK_MIRRORED | set(VAULT_HOLDING_EXEMPT)))}"
    )
    _assert_rationales(VAULT_HOLDING_EXEMPT)
    for name in SINK_MIRRORED:
        assert caps[name]._sink_mirror is sess.secret_sink, (
            f"capability {name!r} resolves secrets but its sink does not "
            "mirror into the session-wide sink — exception-path and "
            "cell-output redaction would miss its masks"
        )
    # The broker's exception-path redaction is the session sink's, so an
    # audited repr(exc) can never carry a secret any capability resolved.
    assert sess.broker.redact.__self__ is sess.secret_sink
