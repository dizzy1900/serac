# CLAUDE.md — how to work in the serac repository

This file is read by Claude Code (and humans) at the start of every session in this repo.
It states what serac is, the rules that cannot be relaxed, how the tree is organised, and the
claims that must never be made. When this file and a code comment disagree, this file wins;
when this file and `docs/adr/` disagree, fix whichever is stale and record it in an ADR.

## Purpose

serac is a standalone open-source model of high-mountain rock-ice avalanche cascades: a
high-altitude bedrock + hanging-glacier failure, a frictional-melt-driven rock-ice avalanche, a
bulked debris flow, landslide-dam / barrier-lake formation and secondary outburst surges,
propagating on the order of 100 km downstream in hours. The motivating event is the
Langtang Lirung / Lhende Khola / Bhote Koshi / Trishuli cascade of 26 August 2026
(USGS `us7000tbwb`). Reference events include Chamoli 2021, Sedongpu 2017–2018, Aru Co 2016,
Blatten 2025 and Kolka 2002.

serac has four operational layers, built in this order across two prompts (Prompt 1 =
foundations, this repository as it stands; Prompt 2 = models):

| Layer | Horizon | Feasibility (settled science) | serac component |
|---|---|---|---|
| L0 Inventory | years → months | High | Slope-unit susceptibility inventory from InSAR / optical archives |
| L1 Watch | months → days | Medium (ductile) / Low (brittle crystalline) | Kinematic anomaly tracking; probabilistic "watch" state, never a deterministic time-of-failure |
| L2 Detect | seconds → 3 min | High | Real-time seismic single-force detection + Landslide Force History (LFH) inversion |
| L3 Cascade | seconds → hours | High (given L2) | Neural runout surrogate → arrival hydrographs at transects → CAP alerts → avoided-loss accounting |

**Deterministic prediction of the day/hour of a bedrock collapse from satellites is not a serac
goal and must never appear as a claim in code, docs or model outputs.** L2 + L3 are where lives
are saved; L0/L1 are where exposure is prioritised.

## Standalone-project framing

serac has no parent organisation, no internal platform to integrate with and no house
conventions to inherit. It depends on no private repository, internal package or hosted
platform. Everything it needs is defined here: its own data-versioning layout, validation
suites, release ledger and deployment unit (a plain Docker image).

Downstream consumers (a decision layer, a financial layer, a dashboard) integrate **only**
through the versioned JSON Schemas published in `contracts/` (`serac schema export`). They
never import `serac` internals. serac never depends on a particular consumer.

## Non-negotiables (verbatim from the founding brief)

1. **No fabricated data, ever.** If a dataset cannot be downloaded in this session, write the adapter, write the fixture-based tests, mark the dataset `status: not_fetched` in the manifest, and fail loudly at runtime. Never synthesise "example" observations and pass them off as real. Synthetic data is allowed only where it is explicitly labelled synthetic in the schema (`provenance: synthetic`).
2. **Unknowns are `null`, not guesses.** The Langtang 2026 source volume has no peer-reviewed estimate as of September 2026. Store it as `null` with a `notes` field and the range of public estimates, each attributed. The same rule applies everywhere.
3. **Provenance on every record.** Source URL, retrieval timestamp, checksum, licence.
4. **Tests run offline.** All tests must pass with no network using committed fixtures (small, real, licence-clean samples). Network-dependent behaviour is exercised by `make smoke-online`, which is allowed to skip.
5. **Self-contained repo conventions.** serac depends on no private repo, internal package or hosted platform. Establish and document its own conventions here: DVC-versioned `data/` and `baselines/`, `src/validation/` suites, Makefile targets `validate-*`, `promote`, `underwriting-check`, an honest `RELEASE_STATUS.md`, and a plain Docker image as the deployment unit. Any downstream consumer integrates through the versioned JSON-Schema contracts in `contracts/`, never by importing serac internals.
6. **Compute targets are portable.** Local dev is Docker Compose; scaled runs (large simulation ensembles, model training) are described as job manifests in `infra/jobs/*.yaml` with core-hour and storage estimates, written for a generic container host and annotated for AWS (Batch / EC2 GPU) as the assumed target. No managed-platform lock-in.
7. **Ask before any download > 5 GB** or any credentialed API call that costs money. Read credentials from `.env` (never commit); document every required credential in `docs/CREDENTIALS.md`.
8. **Small commits, conventional messages, working tree green at every commit.**

## Repository map

```
CLAUDE.md, README.md, CONTRIBUTING.md, RELEASE_STATUS.md, LICENSE (Apache-2.0)
Makefile, pyproject.toml, uv.lock, .python-version, .env.example
.github/workflows/ci.yml        ruff + mypy --strict + offline pytest
contracts/                      published JSON Schemas (generated by `serac schema export`)
docs/ARCHITECTURE.md            C4 L1–L3, the two lanes, latency budget, cadence table
docs/adr/                       one ADR per decision (ADR-0001 …)
docs/CREDENTIALS.md             every .env variable, where to get it, who needs it
docs/DATA_SOURCES.md            every source: URL, licence, cadence, latency, gaps
docs/EVENT_LIBRARY.md           how to add an event; the null-not-guess rule
data/events/  data/aoi/         committed: event records, AOI definitions
data/fixtures/                  committed: tiny REAL samples for offline tests (+ FIXTURES.md)
data/raw/ interim/ features/    DVC-tracked, gitignored
data/manifest.jsonl             the provenance ledger (append-only JSON Lines)
src/serac/domain/               pydantic contracts; imports nothing from adapters/
src/serac/ports/                abstract interfaces (ABCs)
src/serac/adapters/{eo,seismic,bus,storage,hydro,cap}/
src/serac/pipelines/            ingest_*, build_cube, replay
src/serac/streaming/            seedlink_ingestor, detector_stub, cap_stub
src/serac/validation/           validate-* suites
src/serac/cli.py                `serac` (typer)
tests/unit/ integration/ contract/
tests/fixtures/synthetic/       the ONLY place synthetic data may live
infra/docker/                   dev compose (redis; grass placeholder for Prompt 2)
infra/jobs/                     portable job manifests for scaled runs (AWS-annotated)
```

Architecture is hexagonal (ADR-0009): `src/serac/domain/` imports nothing from
`src/serac/adapters/`; adapters implement `src/serac/ports/`. See `docs/ARCHITECTURE.md`.

## Make targets

Run `make help` for the live list. As of this file:

| Target | What it does |
|---|---|
| `sync` | `uv sync --all-extras` — install the locked environment |
| `lint` | `ruff check .` and `ruff format --check .` |
| `typecheck` | `mypy --strict src` |
| `test` | offline test suite, network blocked (`pytest -n auto -m "not online and not redis"`) |
| `smoke-online` | network-dependent tests with `SERAC_ONLINE=1`; **allowed to skip** |
| `validate-events` | event library: schema, every range sourced, no `best` without a source, negative control present |
| `validate-ingest` | manifest integrity, checksums re-hashed, no NISAR BETA/PROVISIONAL mixing |
| `validate-cube` | grid/CRS consistency, monotonic time, provenance attrs per layer |
| `validate-stream` | replay end-to-end on fixtures; CAP validates against the CAP 1.2 XSD |
| `validate-serac` | every suite, **all of them even when one fails**, then writes a validation stamp. Exit 1 means something is broken; exit 3 means every suite ran and a criterion of the brief was not reached (make reports its own exit 2 for either, so run `serac validate all` directly when the distinction matters) |
| `promote` | refuses unless `validate-serac` passed on a clean tree at HEAD |
| `underwriting-check` | avoided loss on the best available input for the Langtang replay; computes and exits 0. Every asset it cannot cost is reported `undetermined`, never zero |
| `replay` | `serac replay --event $(EVENT) --speed $(SPEED)` (defaults: `chamoli-2021`, `max`) |
| `dvc-remote` | writes `$DVC_REMOTE_URL` into the gitignored `.dvc/config.local` |
| `clean` | removes caches and generated reports |

All of these targets are wired to real `serac` sub-commands (`serac --help` lists them).
`RELEASE_STATUS.md` remains the ledger of what each component actually does: a target that
passes does not mean the component behind it is validated against events.

## Running tests

- **Offline (the default, what CI runs):** `make test`. `pyproject.toml` passes
  `--disable-socket --allow-unix-socket` to pytest-socket, so any test that opens a network
  socket fails mechanically (ADR-0010). All tests must pass with no network using committed
  fixtures.
- **Online (optional, allowed to skip):** `make smoke-online` sets `SERAC_ONLINE=1` and runs
  tests marked `online` or `redis` without xdist. `online` tests must call
  `require_network(host)` from `tests/conftest.py` and **skip**, not fail, when the host is
  unreachable. `redis` tests skip unless `SERAC_REDIS_URL` is set and a server answers `PING`.

Pytest markers (declared in `pyproject.toml`, `--strict-markers` is on):

| Marker | Meaning |
|---|---|
| `online` | needs internet; run via `make smoke-online` (`SERAC_ONLINE=1`); skips when offline |
| `redis` | needs a live Redis at `SERAC_REDIS_URL`; skipped otherwise |
| `slow` | long-running test |

## Fixture policy (ADR-0013)

- Real, small, licence-clean samples live in `data/fixtures/`, described in
  `data/fixtures/FIXTURES.md`, each with a matching `status: fetched` entry in
  `data/manifest.jsonl` carrying URL, retrieval time, sha256, size and licence.
- Synthetic doubles live **only** under `tests/fixtures/synthetic/`, labelled
  `provenance: synthetic`, with `notes` explaining the placeholder. `ManifestEntry` rejects a
  synthetic entry whose path is anywhere else, and rejects anything synthetic under `data/`.
- Nothing may be written under `data/` without a manifest entry.

## Contract registry convention

Every domain module that defines a public contract exposes a module-level `CONTRACTS` dict
mapping a schema name (kebab-case, e.g. `mass-movement-event`) to the pydantic model class.
Each model carries a contract version constant (see `MANIFEST_CONTRACT_VERSION` in
`src/serac/domain/manifest.py`). `serac schema export` walks the registries and
writes `contracts/<name>.v<major>.json`; a contract test in `tests/contract/` fails on drift.
There are 18 published contracts.
Changing a contract means bumping its version and regenerating the schema. ADR-0002.

## Provenance ledger

`data/manifest.jsonl` is the single source of truth for "what data do we actually have, where
did it come from, under what licence". One `ManifestEntry` per line, append-only, never
rewritten (`src/serac/adapters/storage/manifest_ledger.py`). Statuses: `fetched`, `listed`,
`requested`, `not_fetched`, `failed`, `dry_run`, `synthetic`. It is deliberately **not** a DVC
output (ADR-0004).

## Subagent roles

Work in this repo is orchestrated across narrow-brief subagents, each in its own git worktree:

| Role | Owns |
|---|---|
| `architect` | governance docs, ADRs, layout, ports, infra manifests, `RELEASE_STATUS.md` |
| `eo-data-engineer` | EO ingestion adapters, feature cube, EO fixtures, DVC pipeline |
| `seismic-engineer` | seismic adapters, bus, streaming skeleton, replay, CAP stub |
| `domain-modeller` | pydantic contracts, event library with fetched citations, AOIs |
| `qa-reviewer` | reads every PR-sized change for fabricated data, unsourced numbers, network calls in unit tests, and violations of the non-negotiables. **Has veto.** |

A change vetoed by `qa-reviewer` does not merge. The orchestrator runs `make test` and
`make validate-serac` after every merge and keeps the tree green.

## Things you must never claim

1. That serac predicts, or will predict, the day or hour of a bedrock collapse from
   satellites. L1 is a probabilistic watch state only.
2. That the detector is more than a stub. `src/serac/streaming/detector_stub.py` is a
   placeholder energy-ratio filter; it has no discriminator, no location and no validated
   detection performance. On both real fixtures it fires on pre-event background noise
   (`RELEASE_STATUS.md`, Known gaps 14). CAP messages it produces are `status: Test`.
3. That the ≤ 180 s detachment-to-CAP latency is proven. It is a design budget
   (`docs/ARCHITECTURE.md`). Replay reports prove plumbing, not latency.
4. That data are real where fixtures are synthetic. Any layer, file or fixture with
   `provenance: synthetic` must be described as synthetic everywhere it is mentioned.
5. That a `best` value exists where no qualifying source supports it. A `Range.best` is
   non-null only when a referenced source is peer-reviewed, an agency/official statement,
   USGS ComCat, or a dataset. Press-only ranges carry `best: null`.
6. That a dataset was fetched when the manifest says `not_fetched`, `listed` or `requested`.
7. That a station, endpoint or service was verified live when it was only exercised against
   a fixture or a fake (see `RELEASE_STATUS.md` → tested-online).

## Commit conventions

- Conventional Commits: `feat:`, `fix:`, `docs:`, `test:`, `chore:`, with an optional scope
  (`feat(domain):`, `chore(infra):`).
- Small commits; the working tree is green (`make lint typecheck test`) at every commit.
- Every commit authored by a Claude Code session ends with a `Co-Authored-By:` trailer
  naming the model that wrote it, e.g. `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- `TODO` comments must reference a GitHub issue or a numbered entry in the Known gaps
  section of `RELEASE_STATUS.md`.
- Subagents do not push. The orchestrator merges and pushes `main`.

## Citation rule

- A DOI is written into a record, doc or fixture only after it has been resolved through the
  Crossref API (`https://api.crossref.org/works/<doi>`) or the publisher's landing page in the
  same session. Unresolved DOI → not cited. (Recon on 2026-09-03 found that DOIs recalled
  from memory were frequently wrong.)
- A `SourceRef` enters an event record only after a successful fetch: `accessed_utc` and
  `sha256` of the bytes actually retrieved are mandatory.
- Wikipedia, blogs and social media are never sources. Reputable press is allowed as
  `press_report` for 2025–2026 events only, and press-only figures never carry `best`.
- The founding brief and this file are not citable sources.

## Ask-first rules

- Any download larger than 5 GB.
- Any credentialed API call that costs money (none of the credentials in `.env.example` do;
  see `docs/CREDENTIALS.md`).
- Any change to a published contract's major version.
