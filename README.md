# serac

Open-source model of high-mountain rock-ice avalanche cascades: bedrock and hanging-glacier
failure, frictional-melt rock-ice avalanche, bulked debris flow, landslide-dam formation and
secondary outburst surges, propagating tens to a hundred kilometres downstream in hours.

Motivating event: Langtang Lirung / Lhende Khola / Bhote Koshi / Trishuli, 26 August 2026
(USGS `us7000tbwb`). Reference events include Chamoli 2021, Sedongpu 2017–2018, Aru Co 2016,
Blatten 2025 and Kolka 2002.

serac is a standalone project: no parent organisation, no private dependencies. Downstream
consumers integrate through the versioned JSON Schemas in `contracts/`, never by importing
`serac` internals.

## What serac is

Four operational layers, built in order:

| Layer | Horizon | Component |
|---|---|---|
| L0 Inventory | years → months | slope-unit susceptibility inventory from InSAR / optical archives |
| L1 Watch | months → days | kinematic anomaly tracking; a probabilistic watch state |
| L2 Detect | seconds → 3 min | real-time seismic single-force detection + force-history inversion |
| L3 Cascade | seconds → hours | runout surrogate → arrival hydrographs → CAP alerts → avoided-loss accounting |

This repository holds the foundations (contracts, event library, ingestion adapters,
feature cube, streaming lane with replay, validation harness) and five v0 model components
(M1 seismic discriminator, M2 force-history inversion, M3 slope watch, M4 runout, M5 avoided
loss). **No model is validated against events and none has been promoted**, and one
validation suite reports an unmet criterion of the brief rather than passing.
See `RELEASE_STATUS.md` for what is real, what is stubbed and what is missing. If you are picking this up to continue it, start at its **Deferred** section: everything there was found and understood, and deliberately not fixed.

## What serac is not

- serac does **not** predict the day or hour of a bedrock collapse from satellites. That is not a
  goal and it is not a claim anywhere in this code, documentation or output.
- The live stream lane runs a **stub** detector and emits test-status CAP messages only. The trained discriminator exists and can be selected in a replay, but `serac stream run` does not mount it; see `RELEASE_STATUS.md` for what exists.
- Numbers in the event library that have no reliable published estimate are `null`.
- The ≤ 180 s detachment-to-alert figure is a design budget, not a measured result: no end-to-end latency has ever been measured. The detection stage alone was measured at 210 s on Chamoli against a 60 s budget, and the budget is unreachable for that component (`docs/ARCHITECTURE.md` §4.3).
- serac owns no satellites and no seismic network: the constellation is bought, not built.

## Quickstart

```bash
uv sync --all-extras        # locked environment, Python 3.12
make help                   # list make targets
make lint typecheck test    # ruff, mypy --strict, offline tests (network blocked)
uv run serac --help         # CLI entrypoint
```

Make targets:

| Target | Purpose |
|---|---|
| `make test` | offline suite; any network socket fails the test |
| `make smoke-online` | network tests with `SERAC_ONLINE=1`; allowed to skip |
| `make validate-events` / `validate-aoi` / `validate-ingest` / `validate-cube` / `validate-stream` / `validate-contracts` / `validate-lfh` / `validate-discriminator` / `validate-runout` / `validate-watch` / `validate-e2e` | the eleven individual suites |
| `make validate-serac` | every suite, all of them even when one fails, then a validation stamp |
| `make promote` | refuses unless `validate-serac` passed on a clean tree at HEAD **and** `PROMOTE_APPROVED_BY` names the human approving it; the name goes into the promotion record |
| `make underwriting-check` | avoided loss for the Lhende AOI on the Langtang replay; uncostable assets are `undetermined`, not zero |
| `make replay EVENT=chamoli-2021 SPEED=max` | replay archived waveforms through the lane; writes `reports/replay/<event>.json` |
| `make dvc-remote` | configure the DVC remote from `DVC_REMOTE_URL` |

Every target above resolves against a `serac` sub-command that exists today;
`RELEASE_STATUS.md` says which of them currently pass.

Credentials (none needed for `make test`; all believed free, verify on registration): copy `.env.example` to `.env`
and see `docs/CREDENTIALS.md`. Local Redis for the streaming lane:
`docker compose -f infra/docker/compose.yaml up -d` (see `infra/docker/README.md`).

The deployment unit is a plain Docker image built from the repository root:
`docker build -f infra/docker/Dockerfile -t serac:$(git rev-parse --short HEAD) .`
**No image has been pushed to any registry**, so the tags in `infra/jobs/*.yaml` are
deliberately unresolvable placeholders; build and tag your own (`RELEASE_STATUS.md`
Known gap 68).

## Documentation

| Document | Contents |
|---|---|
| `CLAUDE.md` | how to work in this repo: non-negotiables, rules, things never to claim |
| `docs/ARCHITECTURE.md` | C4 L1–L3, the batch EO lane and the real-time seismic lane, latency budget, observation cadence |
| `docs/adr/` | ADR-0001 … ADR-0016, one per stack decision |
| `docs/CREDENTIALS.md` | every `.env` variable, where to get it, which adapter needs it |
| `docs/DATA_SOURCES.md` | every data source: URL, licence, cadence, latency, credentials, known gaps |
| `docs/EVENT_LIBRARY.md` | how to add an event; `Range` / `FieldNote` / `SourceRef`; the nine seeded events |
| `RELEASE_STATUS.md` | maturity ledger and numbered known gaps |
| `CONTRIBUTING.md` | ground rules and workflow |
| `infra/docker/README.md`, `infra/jobs/README.md` | deployment image and dev compose; portable job manifests for scaled runs |

## Layout

```
contracts/            published JSON Schemas (generated)
data/events, data/aoi event records and AOI definitions (committed)
data/fixtures/        tiny real samples for offline tests
data/raw|interim|features   DVC-tracked, gitignored
data/manifest.jsonl   provenance ledger
src/serac/{domain,ports,adapters,pipelines,streaming,validation,cli.py}
src/serac/models/{discriminator,lfh,watch,runout}, src/serac/cascade, src/serac/alerting
tests/{unit,integration,contract}; tests/fixtures/synthetic (the only home for synthetic data)
infra/{docker,jobs}
```

Licence: Apache-2.0.
