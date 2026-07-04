# Security & sandboxing — hardening roadmap

> Builds on [`../agents/design.md`](../agents/design.md) §3 (the capability
> broker), §8 (budget), §9 (permissions/approvals/audit), and §11 (deferred
> scope). Read those first; this doc is the *forward-looking* security plan — the
> known gaps in the isolation story and the order to close them.

## How to read this

The broker model (every side effect gated in one place) and the secrets model
(name-in, value-never-out) are sound. The gaps below are not "the design is
wrong" — they are places where an **enforcement layer is missing, degrades
silently, or defaults open**. Each item states: the gap, why it matters, the
approach, the files, and how to verify.

Priorities:

- **P0** — breaks an *intended* guarantee, or the guarantee silently doesn't
  exist on a common platform.
- **P1** — real limit on the blast radius / cost story; not a hole in the model.
- **P2** — hardening and reliability; defense in depth.

---

## Already landed (for context)

These two closed outright breaks of the design's stated guarantees:

- **JSON IPC across the trust boundary.** The parent no longer `pickle`-decodes
  anything the untrusted child sends; child→parent is JSON, parent→child stays
  pickle. Closes a child→parent `__reduce__` RCE into the unsandboxed parent.
  (`broker/remote/protocol.py`, `host.py`, `child.py`.)
- **Environment scrubbing.** Secret-bearing env (`PYHARNESS_SECRET_*`, the vault
  passphrase) is stripped from the spawned child *and* from `shell.bash`
  subprocesses, upholding "cleartext never enters the child" (design §3/§5)
  against spawn inheritance and `echo $SECRET`. (`broker/remote/sandbox.py`,
  `child.py`, `capabilities/shell.py`, `core/session.py`.)

---

## P0 — intended guarantees that don't hold everywhere

### P0.1 — Linux/container OS sandbox (seccomp + namespaces)

**Gap.** The OS confinement (deny network, deny filesystem writes) exists only on
macOS via Seatbelt (`broker/remote/sandbox.py`). On Linux — the typical server
target — `make_child_executable` returns `None` and the child runs with **no
network or write confinement**, relying solely on the process boundary + rlimits.
Design §11 already lists this as deferred; it is the single biggest gap.

**Why it matters.** Out-of-process mode is sold as "real isolation: malicious
code can only do what capabilities permit" (design §3). On Linux that is false
today — the child can open sockets and write files directly, bypassing the
broker entirely.

**Approach.**
- A Linux confinement layer parallel to the Seatbelt one: a `seccomp` syscall
  filter (via `pyseccomp`/libseccomp) blocking `socket`/`connect` and the write
  family, and/or a user namespace with no network namespace (`unshare -n`) plus a
  read-only root bind-mount and a tmpfs scratch.
- Keep the "two channels only" philosophy: deny exactly outbound network and
  filesystem writes; allow reads + compute.
- Offer a container profile (run the child in a minimal container) as the
  strongest option for deployments that can afford it.

**Files.** `broker/remote/sandbox.py` (new `linux_*` builders), `host.py` (select
per platform).

**Verify.** Port the existing macOS tests
(`test_sandbox_denies_filesystem_writes`, `test_sandbox_denies_outbound_network`)
to run on Linux behind a `requires_linux_sandbox` marker.

### P0.2 — Fail loud when no sandbox is available

**Gap.** `Session(out_of_process=True)` silently runs an **unconfined** child when
the platform has no OS sandbox (today: any non-macOS). No warning, no audit entry
— the caller believes they are sandboxed.

**Why it matters.** Silent degradation is how a "secure" deployment ships
insecure. The operator must be able to *require* confinement.

**Approach.**
- A `sandbox=` mode on `RemoteKernel`/`Session`: `"auto"` (current best-effort),
  `"require"` (refuse to start if no OS confinement is available), `"off"`
  (explicit opt-out for dev).
- Emit an audit/trace record at session start naming which confinement layers are
  active (Seatbelt / seccomp / namespaces / none).

**Files.** `broker/remote/host.py`, `core/session.py`, `audit.py`.

**Verify.** `sandbox="require"` raises on a platform with no OS sandbox; a startup
audit record names the active layers.

### P0.3 — Default-deny policy

**Gap.** `Policy.decide` returns `ALLOW` for anything not explicitly listed
(`security/policy.py`). A fresh `Policy()` permits every capability.

**Why it matters.** For a security boundary this is backwards — a newly added
capability is exposed until someone remembers to deny it. The safe default is the
opposite.

**Approach.**
- Invert to allowlist: `decide` returns `ALLOW` only for actions matching an
  `allow` set (with prefix rules), else `DENY`/`APPROVE`. Ship a sensible default
  allowlist so existing flows keep working.
- Keep backward compatibility via a `default=ALLOW|DENY` knob during migration, or
  a preset (`Policy.permissive()` / `Policy.locked_down()`), so this is not a
  silent breaking change.

**Files.** `security/policy.py`, `core/session.py` (default preset), tests.

**Verify.** A `Policy()` with no rules denies `files.write`; the default session
preset still passes the existing capability tests.

---

## P1 — blast-radius and cost limits

### P1.1 — Filesystem reads confined (not just writes)

**Gap.** The Seatbelt profile is `allow default` + deny writes + deny network, so
the child can **read** anything: `/etc/passwd`, `~/.ssh`, the encrypted vault
file, arbitrary source. Reads are blocked from *leaving* only because network is
denied — so any future egress gap turns readable secrets into stolen ones.

**Approach.** Tighten the profile (and the P0.1 Linux equivalent) to deny reads
outside the workspace, the interpreter, and its library paths.

**Files.** `broker/remote/sandbox.py`.

**Verify.** A cell reading `/etc/passwd` raises; reading a workspace file and
importing stdlib still works.

### P1.2 — Budget as a reservation, not post-hoc accounting

**Gap.** `Budget.check()` raises only once `spent >= limit` (`budget.py`). A single
expensive call overshoots arbitrarily, and concurrent sub-agents
(`map_agents` thread pool) can all pass `check()` before any of them records,
racing past the cap.

**Approach.** Reserve under a lock before a metered call (estimate a max cost),
settle the actual on completion. Optionally per-cell / per-fan-out sub-budgets
(design §8 already anticipates these).

**Files.** `budget.py`, `broker/dispatch.py`, `capabilities/agents.py`.

**Verify.** N concurrent metered calls cannot collectively exceed the cap; a
single call that would overshoot is refused before it runs.

### P1.3 — Network egress allowlist + rate limits for `web_fetch`

**Gap.** `web_fetch` is all-or-nothing: once `web` is allowed, the agent can reach
any host. There is no destination allowlist and no request rate limit.

**Approach.** A per-session host/scheme allowlist enforced in `WebCapability`
(and surfaced in policy), plus a simple request-rate cap. This is the egress
control for the *one* network channel that is intentionally open.

**Files.** `capabilities/web.py`, `security/policy.py`.

**Verify.** A fetch to a non-allowlisted host raises; allowlisted hosts succeed;
exceeding the rate cap raises.

### P1.4 — Stronger resource limits + wall-clock timeout

**Gap.** `apply_resource_limits` sets only `RLIMIT_CORE=0` and `RLIMIT_NPROC=512`
(`broker/remote/sandbox.py`). No memory cap, CPU-time cap, file-size cap, or
open-FD cap; `bash` has a per-call timeout but there is no **session/cell
wall-clock** limit.

**Approach.** Add `RLIMIT_AS` (memory), `RLIMIT_CPU`, `RLIMIT_FSIZE`,
`RLIMIT_NOFILE`; lower `NPROC`; add a cell/session wall-clock deadline enforced by
the host (kill the child on overrun, surface as a structured error per design §8).

**Files.** `broker/remote/sandbox.py`, `broker/remote/host.py`.

**Verify.** A memory bomb / infinite loop in a cell is killed and returns a
structured timeout/limit error rather than hanging.

---

## P2 — hardening & reliability

### P2.1 — In-process mode is dev-only — make that explicit

`Session(out_of_process=False)` has **no** process boundary, OS sandbox, or read
confinement; the env scrub helps but `shell.bash` and file reads run in the host.
Document it as development-only and consider a startup warning when it is used
outside tests.

**Files.** `core/session.py`, this doc, README.

### P2.2 — Audit log integrity

The audit log is a plain appendable JSONL the parent writes (`audit.py`). It is
the safety record; harden it against tampering (append-only fsync, optional hash
chaining) so a post-incident review is trustworthy. Design §9 anticipates an
"agent-immutable" trail.

**Files.** `audit.py`.

### P2.3 — Crash-restart reliability race

`test_child_restarts_after_crash` fails on a clean tree: after the child
`os._exit`s, `RemoteKernel.run` can see `is_alive()` True and reuse a dead pipe
(`BrokenPipeError`). Fix: `join()`/reap before the `is_alive()` check, or restart
on send failure. Reliability, not security, but it sits in the same module.

**Files.** `broker/remote/host.py`, `tests/test_remote_kernel.py`.

### P2.4 — IPC denial-of-service bounds

The parent reads child frames with no size cap; a child can send an enormous
frame to exhaust parent memory. Add a max frame size to `recv_json` and reject
oversize messages.

**Files.** `broker/remote/protocol.py`.

### P2.5 — Persistent / richer policy & approvals

Design §9 anticipates "per-path file rails, spend limits" and an agent-immutable
approval UI. Today policy is in-memory prefix rules and approvals are a CLI y/n.
Build per-path file policy, durable approval decisions, and the immutable UI on
the existing seams.

**Files.** `security/policy.py`, `broker/dispatch.py`.

### P2.6 — Move fully off `pickle` (robustness)

Parent→child still uses pickle. It is not a security hole (the child is the
sandbox), but it is fragile (custom return types / exceptions may not pickle).
Longer term, a typed codec both directions removes the last pickle dependency and
makes the wire fully auditable.

**Files.** `broker/remote/protocol.py`, `host.py`, `child.py`.

---

## Tracking

| ID | Item | Priority | Status |
|----|------|----------|--------|
| — | JSON IPC (no pickle RCE) | P0 | ✅ done |
| — | Env scrubbing (child + shell) | P0 | ✅ done |
| P0.1 | Linux/container sandbox | P0 | open |
| P0.2 | Fail loud w/o sandbox | P0 | open |
| P0.3 | Default-deny policy | P0 | open |
| P1.1 | Confine filesystem reads | P1 | open |
| P1.2 | Budget reservation | P1 | open |
| P1.3 | Egress allowlist + rate limit | P1 | open |
| P1.4 | Resource limits + wall-clock | P1 | open |
| P2.1 | In-process = dev-only | P2 | open |
| P2.2 | Audit integrity | P2 | open |
| P2.3 | Crash-restart race | P2 | open |
| P2.4 | IPC DoS bounds | P2 | open |
| P2.5 | Richer policy/approvals | P2 | open |
| P2.6 | Off pickle entirely | P2 | open |

## What is *not* on this list (deliberately)

- **MCP subprocess sandboxing.** MCP stdio servers are operator-configured and may
  legitimately need credentials in their environment; they are trusted, not
  agent-authored, so their env is intentionally not scrubbed. Sandboxing them is a
  separate, optional feature, not a hole.
- **"Use but don't view" sealing beyond secrets** and the **artifact store** —
  tracked in design §3/§4/§11 as product scope, not security hardening.
