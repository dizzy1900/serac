# serac — local orchestration. `prefect` is deliberately not used (ADR-0008).
UV ?= uv
EVENT ?= chamoli-2021
SPEED ?= max

.PHONY: help sync lint typecheck test smoke-online validate-events validate-aoi validate-ingest validate-cube validate-stream validate-contracts validate-serac promote underwriting-check replay dvc-remote clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

sync: ## Install the locked environment
	$(UV) sync --all-extras

lint: ## ruff lint + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

typecheck: ## mypy --strict on src/
	$(UV) run mypy --strict src

test: ## Offline test suite (network blocked)
	$(UV) run pytest -n auto -m "not online and not redis"

smoke-online: ## Network-dependent tests; allowed to skip
	SERAC_ONLINE=1 $(UV) run pytest -m "online or redis" -p no:xdist -ra

validate-events: ## Event library: schema, sourced ranges, negative control present
	$(UV) run serac validate events

validate-ingest: ## Manifest integrity, checksums, no NISAR BETA/PROVISIONAL mixing
	$(UV) run serac validate ingest

validate-cube: ## Grid/CRS consistency, time monotonic, provenance attrs
	$(UV) run serac validate cube

validate-stream: ## Replay end-to-end on fixtures; CAP validates against CAP 1.2 XSD
	$(UV) run serac validate stream

validate-aoi: ## AOI geometry, grid and sources
	$(UV) run serac validate aoi

validate-contracts: ## contracts/*.v0.json match the models
	$(UV) run serac validate contracts

validate-serac: validate-events validate-aoi validate-ingest validate-cube validate-stream validate-contracts ## All validation suites
	$(UV) run serac validate stamp

promote: validate-serac ## Refuses unless validate-serac passed on a clean tree at HEAD
	$(UV) run serac promote

underwriting-check: ## AvoidedLoss schema round-trip; exits 2 "not implemented: Prompt 2"
	$(UV) run serac underwriting-check

replay: ## serac replay --event $(EVENT) --speed $(SPEED)
	$(UV) run serac replay --event $(EVENT) --speed $(SPEED)

dvc-remote: ## Configure the DVC remote from $$DVC_REMOTE_URL into .dvc/config.local
	@test -n "$$DVC_REMOTE_URL" || (echo "DVC_REMOTE_URL is not set (see .env.example)"; exit 1)
	$(UV) run dvc remote add -d --local origin "$$DVC_REMOTE_URL"

clean:
	rm -rf .mypy_cache .ruff_cache .pytest_cache reports/validation reports/replay reports/promotion
