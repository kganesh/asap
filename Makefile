.DEFAULT_GOAL := help
VENV ?= .venv
BIN := $(VENV)/bin
ASAP := $(BIN)/asap

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-14s %s\n", $$1, $$2}'

setup: ## Create .venv, install ASAP, download the OPA policy engine
	@if command -v uv >/dev/null 2>&1; then \
		uv venv -q -p 3.12 $(VENV) && uv pip install -q -p $(VENV) -e ".[dev]"; \
	else \
		python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required (or install uv: https://docs.astral.sh/uv/)"' && \
		python3 -m venv $(VENV) && $(BIN)/pip install -q --upgrade pip && $(BIN)/pip install -q -e ".[dev]"; \
	fi
	@bash scripts/install_opa.sh
	@$(ASAP) doctor

demo: ## Walk through the three scenarios one at a time (pauses between them; you approve the rollback)
	$(ASAP) demo

demo-auto: ## Run all three scenarios back to back with simulated approvals (non-interactive)
	$(ASAP) demo --approve auto --no-pause

demo-bad-deploy: ## Scenario 1 only: bad deploy -> rollback with human approval
	$(ASAP) demo --scenario bad_deploy

demo-cpu: ## Scenario 2 only: CPU throttling -> automatic scale-up
	$(ASAP) demo --scenario cpu_throttle

demo-db: ## Scenario 3 only: DB red herring -> report only
	$(ASAP) demo --scenario db_red_herring

attack: ## Adversarial mock models vs. the guardrails
	$(ASAP) attack

storm: ## 5,000-alert storm through the ingestion funnel
	$(ASAP) storm

tokens: ## Measure per-run LLM input tokens: caching, compaction, budget
	$(ASAP) tokens

test: ## Rego policy tests + pytest
	@if [ -x bin/opa ]; then bin/opa test policies/ -v; elif command -v opa >/dev/null; then opa test policies/ -v; fi
	$(BIN)/pytest

lint: ## Ruff
	$(BIN)/ruff check asap tests

all: setup test demo-auto attack storm ## Everything, non-interactive

docker-demo: ## Run the demo in Docker (no local Python needed)
	docker compose run --rm asap demo --approve auto --no-pause
	docker compose run --rm asap attack

clean: ## Remove runs and caches
	rm -rf runs .pytest_cache .ruff_cache **/__pycache__

.PHONY: tokens help setup demo demo-auto demo-bad-deploy demo-cpu demo-db attack storm test lint all docker-demo clean
