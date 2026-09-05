# serac — local orchestration. `prefect` is deliberately not used (ADR-0008).
UV ?= uv
EVENT ?= chamoli-2021
SPEED ?= max

.PHONY: help sync lint typecheck test smoke-online validate-events validate-aoi validate-ingest validate-cube validate-stream validate-contracts validate-lfh validate-discriminator validate-runout validate-watch validate-e2e validate-serac require-approval promote underwriting-check replay dvc-remote clean

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

validate-lfh: ## Force history: published reproductions, refusals, fixtures, seal
	$(UV) run serac validate lfh

validate-discriminator: ## M1: leakage assertions, forced-group detection, F1 vs baseline
	$(UV) run serac validate discriminator

validate-runout: ## M4: surrogate gates, frozen design, NOT-r.avaflow disclosure
	$(UV) run serac validate runout

validate-watch: ## M3: pre-registration ancestry, causality, no failure date anywhere
	$(UV) run serac validate watch

validate-e2e: ## Both replays run to their honest end; latency, CAP XSD, avoided-loss contract
	$(UV) run serac validate e2e

# Not a list of prerequisites: make stops at the first failing one, so a suite reporting an
# unmet criterion would hide every suite after it. `serac validate all` runs all of them and
# reports together. Note that make reports its own exit 2 for any failed recipe, so a caller
# that needs the 1-vs-3 distinction (CI does) should run `serac validate all` directly.
validate-serac: ## Every validation suite; runs all of them even when one fails
	$(UV) run serac validate all

# PROMOTE_APPROVED_BY is deliberately not defaulted anywhere: promotion is a person's
# decision and the promotion record has to name the person who made it. `serac promote` is
# what enforces this; the prerequisite below only fails fast, before spending the gate run.
require-approval:
	@test -n "$$PROMOTE_APPROVED_BY" || (echo "PROMOTE_APPROVED_BY is not set: promotion needs a named human approver, e.g. PROMOTE_APPROVED_BY='A. Name' make promote"; exit 1)

promote: require-approval validate-serac ## Refuses unless validate-serac passed on a clean tree at HEAD and $$PROMOTE_APPROVED_BY names a human
	$(UV) run serac promote

underwriting-check: ## Avoided loss on the best available input for the Langtang replay
	$(UV) run serac underwriting-check

replay: ## serac replay --event $(EVENT) --speed $(SPEED)
	$(UV) run serac replay --event $(EVENT) --speed $(SPEED)

dvc-remote: ## Configure the DVC remote from $$DVC_REMOTE_URL into .dvc/config.local
	@test -n "$$DVC_REMOTE_URL" || (echo "DVC_REMOTE_URL is not set (see .env.example)"; exit 1)
	$(UV) run dvc remote add -d --local origin "$$DVC_REMOTE_URL"

clean:
	rm -rf .mypy_cache .ruff_cache .pytest_cache reports/validation reports/replay reports/promotion
