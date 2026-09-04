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

This repository is the **foundations** phase (contracts, event library, ingestion
adapters, feature cube, streaming skeleton with replay, validation harness). Models come later.
See `RELEASE_STATUS.md` for what is real, what is stubbed and what is missing.

## What serac is not

- serac does **not** predict the day or hour of a bedrock collapse from satellites. That is not a
  goal and it is not a claim anywhere in this code, documentation or output.
- The planned detector is a **stub**. It emits test-status CAP messages only; see `RELEASE_STATUS.md` for what exists.
- Numbers in the event library that have no reliable published estimate are `null`.
- The ≤ 180 s detachment-to-alert figure is a design budget, not a measured result.
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
| `make promote` | refuses unless `validate-serac` passed on a clean tree at HEAD |
| `make underwriting-check` | avoided loss for the Lhende AOI on the Langtang replay; uncostable assets are `undetermined`, not zero |
| `make replay EVENT=chamoli-2021 SPEED=max` | replay archived waveforms through the lane; writes `reports/replay/<event>.json` |
| `make dvc-remote` | configure the DVC remote from `DVC_REMOTE_URL` |

The `validate-*`, `promote`, `underwriting-check` and `replay` targets depend on `serac`
sub-commands that land as the foundation phases merge; `RELEASE_STATUS.md` says which exist.

Credentials (none needed for `make test`; all believed free, verify on registration): copy `.env.example` to `.env`
and see `docs/CREDENTIALS.md`. Local Redis for the streaming lane:
`docker compose -f infra/docker/compose.yaml up -d` (see `infra/docker/README.md`).

## Documentation

| Document | Contents |
|---|---|
| `CLAUDE.md` | how to work in this repo: non-negotiables, rules, things never to claim |
| `docs/ARCHITECTURE.md` | C4 L1–L3, the batch EO lane and the real-time seismic lane, latency budget, observation cadence |
| `docs/adr/` | ADR-0001 … ADR-0015, one per stack decision |
| `docs/CREDENTIALS.md` | every `.env` variable, where to get it, which adapter needs it |
| `docs/DATA_SOURCES.md` | every data source: URL, licence, cadence, latency, credentials, known gaps |
| `docs/EVENT_LIBRARY.md` | how to add an event; `Range` / `FieldNote` / `SourceRef`; the nine seeded events |
| `RELEASE_STATUS.md` | maturity ledger and numbered known gaps |
| `CONTRIBUTING.md` | ground rules and workflow |
| `infra/docker/README.md`, `infra/jobs/README.md` | dev compose; portable job manifests for scaled runs |

## Layout

```
contracts/            published JSON Schemas (generated)
data/events, data/aoi event records and AOI definitions (committed)
data/fixtures/        tiny real samples for offline tests
data/raw|interim|features   DVC-tracked, gitignored
data/manifest.jsonl   provenance ledger
src/serac/{domain,ports,adapters,pipelines,streaming,validation,cli.py}
tests/{unit,integration,contract}; tests/fixtures/synthetic (the only home for synthetic data)
infra/{docker,jobs}
```

Licence: Apache-2.0.
