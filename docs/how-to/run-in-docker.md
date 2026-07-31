# Run in Docker

Run the agent in a container: nothing on your machine but Docker, your API key
supplied at run time, and the same OS confinement the native install has. The
image is self-contained and non-root, and never contains a key — the deny-all
`.dockerignore` guarantees `.env`, `.git` and session state cannot enter a
layer.

## Build and run

```bash
make docker-build                                  # build the image (once)
docker run -it --rm --env-file .env pyharness      # interactive agent
```

`--env-file .env` hands your local config (the `ANTHROPIC_API_KEY` line from
`make setup`) to the container at run time. No `.env`? Pass the key alone,
forwarded from your shell environment so it stays out of shell history:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # if not already set
docker run -it --rm -e ANTHROPIC_API_KEY pyharness
```

`make docker-run` does the `.env` variant with a persistent volume (below).
Headless one-shots and session inspection work the same as the native CLI —
arguments after the image name go to `pyharness`:

```bash
docker run --rm --env-file .env pyharness run "probe task" --json
```

## Verify the sandbox engages

The container does not weaken confinement, but it does move the dependency:
Landlock + seccomp are enforced by the **host** kernel (the Docker VM's kernel
on macOS/Windows), which must offer Landlock ABI 3+ — Linux 6.2 or newer with
the `landlock` LSM enabled. Docker's default seccomp profile permits the
Landlock syscalls, so no `--security-opt`, extra capability, or privilege is
needed. Prove it on your host rather than trusting this page:

```bash
make docker-verify
```

This starts a real sandboxed kernel inside the image and probes enforcement
from agent code: outbound sockets denied by address family, writes jailed to
the workspace, `$HOME` unreadable, no escape by exec'ing a subprocess. Exit 0
means every probe was enforced. Exit 2 means the host kernel cannot confine
agent code — pyharness then **refuses to start** a kernel in the container
(fail closed) rather than run unconfined; fix the host, and never set
`PYHARNESS_ALLOW_UNSANDBOXED` to paper over it. Exit 1 — claimed support
without enforcement — should never happen and is a bug worth reporting.

## Keep state across runs

Sessions, the vault, and the index live under `/home/agent`. Anonymous
containers (`--rm`) discard them; mount a volume to keep them:

```bash
docker run -it --rm --env-file .env -v pyharness-home:/home/agent pyharness
```

## What differs from a native install

- **No live viewer by default.** It binds `127.0.0.1` inside the container,
  which `-p` publishing cannot reach, so the image sets `PYHARNESS_WATCH=false`.
  On a Linux host, `--network host -e PYHARNESS_WATCH=true` restores it at
  `http://localhost:6061`.
- **No browser lane.** The `[browser]` extra and its Chromium binary are not in
  the image; `open_browser` is unavailable.
- **Telemetry endpoints resolve inside the container.** An
  `OTEL_EXPORTER_OTLP_ENDPOINT` of `localhost:4317` from your `.env` points at
  the container itself; leave telemetry off, or point it at a host-reachable
  address.

## Related

- [Security & audit](../explanation/security-and-audit.md) — what the sandbox
  enforces and why the launch gate fails closed.
- [Configuration](../reference/configuration.md) — every env var `--env-file`
  can carry.
