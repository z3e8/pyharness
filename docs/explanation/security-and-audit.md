# Security & audit

*Understanding-oriented: the trust model.*

- **Policy** (`pyharness/security/policy.py`) gates actions before they run.
- **Vault** (`pyharness/security/vault.py`) keeps secrets encrypted and out of
  the model's context.
- **Audit** (`pyharness/audit.py`) is a tamper-evident hash chain of every
  action; verify with `make verify-audit DIR=.sessions/<name>`.

<!-- TODO: the threat model these defend against, how the hash chain makes
tampering detectable, and where the boundaries currently are. -->
