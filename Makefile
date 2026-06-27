# pyharness — one entry point for setup, the agent, and observability.
# Run `make` (or `make help`) to see everything. Config lives in one `.env`
# (copied from .env.example by `make setup`); this file and docker compose both
# read it, so there are no env vars to export by hand.

# Load .env so recipes (and docker compose) see every value.
ifneq (,$(wildcard .env))
include .env
export
endif

COMPOSE := docker compose --env-file .env -f deploy/observability/docker-compose.yml

.DEFAULT_GOAL := help

## help: list available targets
.PHONY: help
help:
	@echo "pyharness — make targets:"
	@grep -hE '^## ' $(MAKEFILE_LIST) | sed 's/## /  /'
	@echo ""
	@echo "Typical first run:  make setup  →  edit .env (ANTHROPIC_API_KEY)  →  make up  →  make run"

## setup: create .env and install the package + dev deps (one-time)
.PHONY: setup
setup: .env install
	@echo ""
	@echo "✓ setup done. Edit .env and set ANTHROPIC_API_KEY, then: make up && make run"

.env:
	@cp .env.example .env
	@echo "✓ created .env from .env.example"

## install: create the venv and install pyharness (editable) + pytest
.PHONY: install
install:
	uv venv
	uv pip install -e . pytest

## up: start the observability stack (Langfuse self-provisions) and wait until ready
.PHONY: up
up:
	$(COMPOSE) up -d
	@echo "waiting for Langfuse to come up (first boot runs migrations, ~30-60s)..."
	@until curl -sf http://localhost:3000/api/public/health >/dev/null 2>&1; do printf '.'; sleep 3; done
	@echo ""
	@echo "✓ stack ready → Langfuse http://localhost:3000  (login: $${LANGFUSE_USER_EMAIL:-admin@example.com})  ·  Prometheus http://localhost:9090"

## down: stop the observability stack (keeps data)
.PHONY: down
down:
	$(COMPOSE) down

## clean: stop the stack and delete its data volumes
.PHONY: clean
clean:
	$(COMPOSE) down -v

## logs: tail the observability stack logs
.PHONY: logs
logs:
	$(COMPOSE) logs -f

## run: start the interactive pyharness agent (telemetry env from .env)
.PHONY: run
run:
	uv run pyharness

## observe: open the built-in session timeline UI (file-based, no stack needed)
.PHONY: observe
observe:
	uv run pyharness-observe

## test: run the test suite (no API key needed)
.PHONY: test
test:
	uv run pytest -q

## verify-audit: check a session's audit log hash chain (DIR=.sessions/<name>)
.PHONY: verify-audit
verify-audit:
	@uv run python -c "import sys; from pyharness.audit import verify_chain; ok,bad=verify_chain('$(DIR)/audit.jsonl'); print('✓ intact' if ok else f'✗ broken at entry {bad}'); sys.exit(0 if ok else 1)"
