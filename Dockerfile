# syntax=docker/dockerfile:1
# pyharness container — self-contained, non-root, BYO API key.
#
#   docker build -t pyharness .            # or: make docker-build
#   docker run -it --rm --env-file .env pyharness
#
# The image never contains a key or any other config: everything arrives at
# `docker run` time (see docs/how-to/run-in-docker.md). Agent code inside the
# container is still confined by pyharness's own Landlock + seccomp sandbox,
# which needs the *host* kernel to offer Landlock ABI >= 3 (Linux 6.2+). Where
# it does not, pyharness refuses to start rather than run agent code
# unconfined — `make docker-verify` proves which one your Docker host is.

# --- build: resolve the locked dependency set into a relocatable venv ---------
# Both stages share the python:3.13-slim base so the venv's interpreter path is
# identical when copied across. uv is pinned to the minor the lockfile was
# produced with.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY pyharness/ pyharness/
# --frozen: the lockfile is authoritative — same versions as dev and CI.
# --no-editable: a real site-packages install, so nothing in the final image
# references /src (which only exists in this stage).
ENV UV_PROJECT_ENVIRONMENT=/opt/pyharness
RUN uv sync --frozen --no-dev --no-editable --no-cache

# --- runtime ------------------------------------------------------------------
FROM python:3.13-slim
LABEL org.opencontainers.image.title="pyharness" \
      org.opencontainers.image.description="A containment and audit layer for code-as-action agents." \
      org.opencontainers.image.source="https://github.com/z3e8/pyharness" \
      org.opencontainers.image.licenses="Apache-2.0"

# Non-root: the agent parent process needs no privileges, and the OS sandbox's
# threat model assumes agent code runs as an ordinary user.
RUN useradd --create-home --user-group agent

COPY --from=build /opt/pyharness /opt/pyharness
COPY deploy/container/verify-sandbox.py /opt/pyharness/verify-sandbox.py

# PYHARNESS_WATCH=false: the live viewer binds 127.0.0.1 *inside* the container,
# which `-p` port publishing cannot reach — a listening socket nobody can visit
# is worse than none. `--network host -e PYHARNESS_WATCH=true` re-enables it on
# a Linux host (see docs/how-to/run-in-docker.md).
ENV PATH="/opt/pyharness/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYHARNESS_WATCH=false

USER agent
# Session state (.sessions/, ~/.pyharness) lands under /home/agent — mount a
# volume there to keep it across runs.
WORKDIR /home/agent

ENTRYPOINT ["pyharness"]
