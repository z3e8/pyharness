# pyharness — one entry point for setup, the agent, and observability.
# Run `make` (or `make help`) to see everything. Config lives in one `.env`
# (copied from .env.example by `make setup`); this file and docker compose both
# read it, so there are no env vars to export by hand.

# Load .env so recipes (and docker compose) see every value.
ifneq (,$(wildcard .env))
include .env
export
endif

OBS := deploy/observability
COMPOSE := docker compose --env-file .env -f $(OBS)/docker-compose.yml
COMPOSE_LF := docker compose --env-file .env -f $(OBS)/docker-compose.langfuse.yml

.DEFAULT_GOAL := help

## help: list available targets
.PHONY: help
help:
	@echo "pyharness — make targets:"
	@grep -hE '^## ' $(MAKEFILE_LIST) | sed 's/## /  /'
	@echo ""
	@echo "First run:  make setup  →  edit .env (ANTHROPIC_API_KEY)  →  make dev"

## setup: create .env and install the package + dev deps + the browser lane (one-time)
.PHONY: setup
setup: .env install browser
	@echo ""
	@echo "✓ setup done. Edit .env and set ANTHROPIC_API_KEY, then: make dev"

.env:
	@cp .env.example .env
	@echo "✓ created .env from .env.example"

## install: create the venv and install pyharness (editable) + the dev toolchain
.PHONY: install
install:
	uv venv
	uv pip install -e . --group dev

## browser: provision the browser lane — the pyharness[browser] extra + chromium binary
.PHONY: browser
browser:
	uv pip install -e '.[browser]'
	uv run playwright install chromium
	@echo "✓ browser lane ready (playwright + chromium)"

## dev: run the agent with its built-in live viewer — the daily command
.PHONY: dev
dev: run

## watch: live session viewer for a session started elsewhere (tails .sessions/, http://localhost:6061)
.PHONY: watch
watch:
	uv run pyharness-watch

## site: rebuild the committed static session pages from the demo run
# The pages are the artifact — .sessions/ is gitignored, so a stranger cloning
# the repo cannot regenerate them. SESSIONS names the run to bake; the four
# published pages are chosen in evals/demo/site/README.md.
SITE_RUN ?= .sessions/demo-20260730-225820/invoice-exfiltration
.PHONY: site
site:
	uv run pyharness-watch $(SITE_RUN) --static evals/demo/site \
		--title "pyharness — containment, one session at a time"

## up: start the optional Phoenix OTel backend (then set PYHARNESS_TELEMETRY_ENABLED=true)
.PHONY: up
up: .env
	$(COMPOSE) up -d
	@echo "waiting for Phoenix..."
	@until curl -sf http://localhost:6006/ >/dev/null 2>&1; do printf '.'; sleep 2; done
	@echo ""
	@echo "✓ Phoenix ready → http://localhost:6006"

## down: stop the Phoenix container (keeps data)
.PHONY: down
down:
	$(COMPOSE) down

## clean: stop Phoenix and delete its data volume
.PHONY: clean
clean:
	$(COMPOSE) down -v

## logs: tail the Phoenix container logs
.PHONY: logs
logs:
	$(COMPOSE) logs -f

## up-langfuse: start the heavier Langfuse + Prometheus stack instead (multi-user/cloud profile)
.PHONY: up-langfuse
up-langfuse: .env
	$(COMPOSE_LF) up -d
	@echo "waiting for Langfuse (first boot runs migrations, ~30-60s)..."
	@until curl -sf http://localhost:3000/api/public/health >/dev/null 2>&1; do printf '.'; sleep 3; done
	@echo ""
	@echo "✓ Langfuse ready → http://localhost:3000  ·  set PYHARNESS_TELEMETRY_METRICS=true in .env for metrics"

## down-langfuse: stop the Langfuse stack
.PHONY: down-langfuse
down-langfuse:
	$(COMPOSE_LF) down

## run: start the interactive pyharness agent (telemetry env from .env)
.PHONY: run
run: .env
	uv run pyharness

## test: run the test suite, adversarial suite included (no API key needed)
.PHONY: test
test:
	uv run pytest -q

## evals: run the adversarial suite and refresh the committed scoreboard
# Exit code is non-zero on any attack that deviates from its documented
# expectation. This used to need `-c` instead of `-m`, because some attacks
# start a real sandboxed kernel and multiprocessing re-imported the parent's
# __main__ inside it; the kernel now detaches __main__ before spawning, so the
# plain -m form works and this target doubles as the end-to-end check that it
# still does.
.PHONY: evals
evals:
	uv run python -m evals.run --write evals/SCOREBOARD.md

# Lint scope is listed explicitly rather than run repo-wide: agents/old/ holds
# retired scratch scripts that a bare `ruff check .` fails on, and they are not
# code anyone maintains. deploy/ is in: it carries the container's
# verify-sandbox script, which is maintained code.
LINT_PATHS = pyharness tests evals deploy

## lint: check formatting and lints without changing files (what CI enforces)
.PHONY: lint
lint:
	uv run ruff check $(LINT_PATHS)
	uv run ruff format --check $(LINT_PATHS)

## format: auto-format and apply autofixable lints
.PHONY: format
format:
	uv run ruff format $(LINT_PATHS)
	uv run ruff check --fix $(LINT_PATHS)

## typecheck: run mypy (lenient, non-blocking — see pyproject [tool.mypy])
.PHONY: typecheck
typecheck:
	uv run mypy

# --- container ------------------------------------------------------------------
# BYO key by design: the image is built from source only (see .dockerignore's
# deny-all context) and config arrives at `docker run` time via --env-file.
IMAGE ?= pyharness

## docker-build: build the container image (self-contained, non-root, no key baked in)
.PHONY: docker-build
docker-build:
	docker build -t $(IMAGE) .

## docker-run: interactive agent in the container (key from .env; state persists in a volume)
.PHONY: docker-run
docker-run: .env docker-build
	docker run -it --rm --env-file .env -v pyharness-home:/home/agent $(IMAGE)

## docker-verify: prove the OS sandbox (Landlock+seccomp) engages on this Docker host
.PHONY: docker-verify
docker-verify: docker-build
	docker run --rm --entrypoint python $(IMAGE) /opt/pyharness/verify-sandbox.py

## verify-audit: check a session's audit log hash chain (DIR=.sessions/<name>)
.PHONY: verify-audit
verify-audit:
	@uv run python -c "import sys; from pyharness.audit import verify_chain; ok,bad=verify_chain('$(DIR)/audit.jsonl'); print('✓ intact' if ok else f'✗ broken at entry {bad}'); sys.exit(0 if ok else 1)"
