# Security Policy

`pyharness` runs model-authored Python and reaches the network, a browser, an
encrypted secrets vault, and the local shell. That is a real attack surface, so
security reports are taken seriously and handled privately.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report privately through **GitHub's private vulnerability reporting**:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability** (GitHub Security Advisories).
3. Describe the issue, the impact, and a reproduction if you have one.

This opens a private advisory visible only to you and the maintainer, and no
email address is needed. Private reporting is the only channel: this repository
has issues disabled, so there is no public tracker to fall back to.

This is a single-maintainer reference implementation rather than a maintained
package, so there is no response-time commitment: reports are read and taken
seriously, but a fix may take a while or may be answered with a documented
boundary instead. Please allow reasonable time before any public disclosure.

## Supported versions

Only the **latest `main`** receives fixes. There are no releases and no
backported patches for older revisions.

## Scope

In scope — issues that let untrusted, model-authored code or attacker-controlled
input escape the intended trust boundary, for example:

- Escaping the OS sandbox that confines executed code (macOS Seatbelt today).
- Reading or exfiltrating vault secrets, TOTP seeds, or browser login profiles
  that the policy is supposed to keep out of the agent's reach.
- SSRF / egress-guard bypasses (reaching internal or link-local addresses that
  `check_url` should block, including via redirects or DNS tricks).
- Bypassing the approval policy for a gated action (shell, state-changing HTTP,
  credential-bearing requests, skill/package writes).
- Forging or silently rewriting the tamper-evident audit log.

Out of scope:

- Anything requiring a pre-existing local attacker who already has your shell,
  your `.env`, or your vault passphrase — that is game-over independent of
  pyharness.
- Running unsandboxed **by explicit opt-in** (`PYHARNESS_ALLOW_UNSANDBOXED=true`,
  `unsafe_in_process=True`) — these are documented, deliberately loud escape
  hatches, not vulnerabilities.
- Prompt injection that stays within already-granted capabilities (the policy
  boundary is the security control; a model convinced to do something it is
  *allowed* to do is a policy-configuration question, not a harness bug).
- Denial of service from the agent spending its own budget.
- Vulnerabilities in third-party MCP servers or npm/PyPI packages you choose to
  mount or install (report those upstream).

If you are unsure whether something is in scope, report it privately anyway.

## Security model

The trust boundary and the four mechanisms that enforce it (policy, the OS
sandbox, the secrets vault, and the audit log) are documented in
[docs/explanation/security-and-audit.md](docs/explanation/security-and-audit.md).
The non-macOS sandbox gap and its opt-in gate are covered there and in
[docs/reference/configuration.md](docs/reference/configuration.md). Read those
before reporting to confirm the behavior is unintended rather than a documented
limitation.
