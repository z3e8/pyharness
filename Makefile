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

## setup: create .env and install the package + dev deps (one-time)
.PHONY: setup
setup: .env install
	@echo ""
	@echo "✓ setup done. Edit .env and set ANTHROPIC_API_KEY, then: make dev"

.env:
	@cp .env.example .env
	@echo "✓ created .env from .env.example"

## install: create the venv and install pyharness (editable) + pytest
.PHONY: install
install:
	uv venv
	uv pip install -e . pytest

## dev: start observability (background) and run the agent (foreground) — the daily command
.PHONY: dev
dev: up run

## up: start the local Phoenix observability container and wait until ready
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

## test: run the test suite (no API key needed)
.PHONY: test
test:
	uv run pytest -q

## verify-audit: check a session's audit log hash chain (DIR=.sessions/<name>)
.PHONY: verify-audit
verify-audit:
	@uv run python -c "import sys; from pyharness.audit import verify_chain; ok,bad=verify_chain('$(DIR)/audit.jsonl'); print('✓ intact' if ok else f'✗ broken at entry {bad}'); sys.exit(0 if ok else 1)"
