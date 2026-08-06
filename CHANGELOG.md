# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

There are no releases. The project is distributed as source and is not published
to PyPI; see [project status](README.md#project-status). Everything below is
unreleased and describes `main`.

## [Unreleased]

### Added

- Apache-2.0 `LICENSE` and complete `pyproject.toml` packaging metadata.
- Manually-triggered CI (`workflow_dispatch`; nothing fires on push or PR): a
  GitHub Actions test matrix (Python 3.11 / 3.12 / 3.13 on Linux, plus one macOS
  leg that exercises the real Seatbelt sandbox), a ruff lint/format check, and a
  non-blocking mypy job.
- Developer tooling: `ruff` (lint + format) and `mypy` config, a PEP 735 `dev`
  dependency group, and `make lint` / `make format` / `make typecheck` targets.
- Open-source community files: `SECURITY.md` (private disclosure via GitHub
  advisories), `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, this changelog, and
  GitHub issue / pull-request templates.

### Changed

- Kernel default flipped to the sandboxed **out-of-process** child; the
  in-process kernel is now reachable only via an explicit `unsafe_in_process=True`
  opt-in (test use).
- Non-macOS platforms now refuse to start a kernel without an explicit
  `PYHARNESS_ALLOW_UNSANDBOXED=true` opt-in, rather than degrading silently.

### Security

- `shell.bash` runs under an OS sandbox (macOS Seatbelt) and is approval-gated by
  default.
- SSRF hardening: `check_url` is re-evaluated on every redirect hop for both HTTP
  and browser navigation; DNS-resolution failure now fails closed.
- Secrets-vault, MCP-egress, and audit-log tamper-evidence hardening (anchor
  sidecar); minimal scrubbed environment for `pip` installs and subprocesses.
- LLM transport reliability: a byte-liveness read timeout with visible retries,
  and an IPv4 transport pin (escape hatch `PYHARNESS_LLM_IPV6=true`).
