# serac

Open-source model of high-mountain rock-ice avalanche cascades: bedrock and hanging-glacier
failure, frictional-melt rock-ice avalanche, bulked debris flow, landslide-dam formation and
secondary outburst surges, propagating tens to a hundred kilometres downstream in hours.

Motivating event: Langtang Lirung / Lhende Khola / Bhote Koshi / Trishuli, 26 August 2026
(USGS `us7000tbwb`). Reference events include Chamoli 2021, Sedongpu 2017–2018, Aru Co 2016,
Blatten 2025 and Kolka 2002.

## What serac is

Four operational layers, built in order:

| Layer | Horizon | Component |
|---|---|---|
| L0 Inventory | years → months | slope-unit susceptibility inventory from InSAR / optical archives |
| L1 Watch | months → days | kinematic anomaly tracking; a probabilistic watch state |
| L2 Detect | seconds → 3 min | real-time seismic single-force detection + force-history inversion |
| L3 Cascade | seconds → hours | runout surrogate → arrival hydrographs → CAP alerts → avoided-loss accounting |

This repository currently contains the **foundations** (contracts, event library, ingestion
adapters, feature cube, streaming skeleton with replay, validation harness). Models come later.
See `RELEASE_STATUS.md` for what is real, what is stubbed and what is missing.

## What serac is not

- serac does **not** predict the day or hour of a bedrock collapse from satellites. That is not a
  goal and it is not a claim anywhere in this code, documentation or output.
- The detector in this repository is a **stub**. It emits test-status CAP messages only.
- Numbers in the event library that have no reliable published estimate are `null`.

## Quickstart

```bash
uv sync --all-extras
make test              # offline, network blocked
make validate-serac    # all validation suites
uv run serac --help
```

Licence: Apache-2.0.
