# Release status

Honest maturity ledger. Updated: 2026-09-04, after the five Prompt 2 model components
(M1–M5) merged to `main` at `a287542`. A cell is `yes` only when the corresponding command has
actually been run and passed on `main`.

## Gate status — read this first

**`make validate-serac` is RED.** All eleven suites run — the aggregate no longer stops at the
first failure — and ten pass. `validate-discriminator` fails on an unmet criterion of the brief:

> the brief requires Langtang and Chamoli both detected. Detected: `['chamoli-2021']`.
> — `reports/validation/discriminator.json`, `forced_groups_detected[loro_hma]` and
> `forced_groups_detected[time_forward]`, severity `criterion_unmet`

Nothing is broken. The criterion is not met, the suite says so rather than reporting green,
and **M1 therefore stays at `validated-against-events: no`** as the brief requires ("if a gate
fails, the component stays at its previous maturity level and the failure is described").

Consequences, all intended:

- **`make promote` is blocked.** `validate-serac` never reaches `serac validate stamp`, so
  `reports/validation/latest.json` is stale — it is stamped at `0b70091` and lists only seven
  suites. The most recent promotion record, `reports/promotion/71f3426c….json`, is from before
  any Prompt 2 component existed. **Nothing has been promoted since.** `discriminator` is one of
  the suites the stamp requires, so no fix to the harness can make `promote` reachable while
  this criterion is unmet. **`promote` now also refuses without a named human**: it takes an
  approver from `PROMOTE_APPROVED_BY` (or `--approved-by`), writes that name into the promotion
  record, and rejects placeholders such as `1`, `yes`, `ci` or `bot`. Nothing defaults it — not
  the Makefile, not `.env.example`, not CI — so no automated path can promote anything. The
  gates say a tree *may* be promoted; only a person says it *is*.
- **CI now runs the gates, and a green tick still means less than it looks.**
  `.github/workflows/ci.yml` runs `ruff`, `mypy --strict`, the offline test suite, every
  validation suite and `underwriting-check`. The gate step passes `--allow-unmet` naming the two
  criteria above, so CI is green while they stay unmet and **fails on any other unmet criterion
  or any broken suite**. What CI proves is that the harness runs and its results have not moved;
  it is not evidence that any model works. Until the Prompt 2 components merged, CI ran no
  validation suite at all, so every "CI green" recorded before 2026-09-04 means lint, types and
  tests only.

| Suite | Checks | Result |
|---|---|---|
| `validate-events` | 64 | pass |
| `validate-aoi` | 100 | pass (3 warnings: hand-digitised geometry) |
| `validate-ingest` | 15 | pass (1 warning: 517 transient rows) |
| `validate-cube` | 23 | pass |
| `validate-stream` | 33 | pass |
| `validate-contracts` | 26 | pass (22 contracts) |
| `validate-lfh` | 22 | pass |
| **`validate-discriminator`** | **24** | **FAIL — 2 unmet criteria, 4 warnings** |
| `validate-runout` | 30 | pass (1 warning: arrival coverage 0.794) |
| `validate-watch` | 36 | pass (1 warning: transient rows) |
| `validate-e2e` | 15 | pass (3 warnings: no forecast on either replay, 0 of 14 assets costed) |
| **total** | **388** | |

`make test` passes: **1,418 offline tests**, network blocked (1,316 before the documentation
checks of gap 72; 1,388 before the provenance checks of gap 73).

**No serac model is validated against events, and none has been promoted.** Four of the five
components returned a negative or a refusal on the motivating event (Langtang Lirung / Lhende
Khola, 26 August 2026) for four unrelated physical reasons; the fifth had no input to run on.
`reports/PROMPT2_SUMMARY.md` is the write-up.

## Deferred — read this before starting new work

Everything below was **found, understood and deliberately not fixed** in the time available. It
is recorded here rather than left in a conversation. Each entry points at its full Known-gaps
entry. Ordered by what blocks the most.

**Blocks the release mechanics**

- **Gap 66 — the gates dirty the tree that `promote` requires clean.** `validate-e2e` rewrites
  tracked `reports/e2e/*` with fresh timestamps on every run, so `make validate-serac` always
  leaves the working tree modified. Nothing can ever be promoted until the volatile fields are
  excluded from the committed artefacts or those artefacts stop being tracked. Masked today only
  because promotion is blocked for other reasons.
- **Gap 64 — `reports/validation/latest.json` is stale** (stamped at `0b70091`, seven suites).
  Do not read it as current; the per-suite reports are.

**Changes what a number means**

- **Gap 17 — a demonstrated leak vector survives all ten leakage assertions.** Positives realise
  ~1 more receiver than their matched negatives and `n_stations` alone scores ROC-AUC 0.587, so
  some of M1's reported skill may be archive density rather than source physics. This is a
  dataset fix, not a training one, and it is the single most important open item on M1.
- **Gap 42 — the surrogate fails one of its five gates** (5–95 % arrival coverage 0.794 against
  a 0.85–0.95 target), and the arrival gate that *passes* rests on 3 held-out members at one
  transect. Three of four transects scored nothing.
- **Gap 39 — no independent simulator.** `serac-swe-voellmy` has never been cross-validated
  against r.avaflow or any other code, so its structural bias cannot be separated from
  implementation error.

**Half-finished work**

- **Gap 56 — the live stream lane is still the stub.** Replay can select the trained detector;
  `serac stream run` cannot.
- **Gap 62 — `SourceRef` exists twice.** A contract test now fails on divergence, but the two
  copies have not been merged.
- **Gap 61 — no job manifest in `infra/jobs/` has ever been executed.** Every core-hour and
  storage figure in them is an estimate with a stated basis, never a measurement.
- **Gap 68 — the deployment image is unpublished and single-architecture.**
  `infra/docker/Dockerfile` now exists, builds and runs, but only on `linux/arm64` and only in
  its base form; nothing has been pushed to any registry and no GPU variant has ever been
  built, so submitting any manifest still needs an operator to build and tag the image first.

**Cheap and worth doing**

- M2's 200-draw bootstrap is serial and embarrassingly parallel; ~90 % of the warm inversion
  cost. Ten cores should take the warm total from ~75 s to near 20 s.
- Exposure valuation and population figures. 14 of 14 Lhende assets carry no replacement value
  and 3 of 3 settlements carry `population: null`, which is the single reason every M5 output is
  `undetermined`. This is the cheapest gap in the project.

## Component matrix

Columns: **designed** (an ADR, architecture entry or model card exists), **implemented** (code
in the tree), **tested-offline** (covered by `make test`), **tested-online** (exercised against
the real service), **validated-against-events** (checked against event-library records by a
`validate-*` suite or a replay), **production** (deployed and operated; nothing is).

`yes (card)` in the *designed* column means the design record is a `reports/MODEL_CARD_*.md`
rather than an ADR; only ADR-0016 (modelled data is derived, not synthetic) was written for
Prompt 2.

### Foundations (Prompt 1)

| Component | designed | implemented | tested-offline | tested-online | validated-against-events | production |
|---|---|---|---|---|---|---|
| Repo skeleton (uv, ruff, mypy strict, pytest-socket, CI) | yes | yes | yes | n/a | n/a | no |
| Provenance ledger (`ManifestEntry`, `JsonlManifestLedger`) | yes | yes | yes | n/a | no | no |
| Settings / `.env` (`SeracSettings`) | yes | yes | yes (import) | no | n/a | no |
| CLI skeleton (`serac --help`, `--version`) | yes | yes | yes | n/a | n/a | no |
| Domain contracts (`Range`, `SourceRef`, `FieldNote`, `MassMovementEvent`, geo, forecast, avoided-loss, slope-watch) | yes | yes | yes (validators, both paths) | n/a | no | no |
| `serac schema export` + `contracts/*.v0.json` + drift test | yes | yes (22 contracts) | yes | n/a | n/a | no |
| Event library (11 records / 9 items) + `serac events add/report/build-index` | yes | yes | yes (`validate-events` 64 checks) | sources fetched live 2026-09-03 | n/a | no |
| AOIs (lhende-khola-trishuli, chamoli-rishiganga, blatten-lotschental) | yes | yes | yes (`validate-aoi` 100 checks, 3 warnings) | OSM/agency sources fetched 2026-09-03 | n/a | no |
| Sentinel-1 ASF adapter | yes | yes | yes | search only | no | no |
| HyP3 InSAR adapter + pair planner | yes | yes | yes | **yes — 517 burst-InSAR products delivered 2026-09-03** | no | no |
| Sentinel-2 CDSE adapter | yes | yes | yes (fakes) | search only | no | no |
| Sentinel-2 Earth Search adapter | yes | yes | yes (fake STAC, real crops) | yes (smoke 2026-09-03; M3 optical pairs) | no | no |
| NISAR adapter + level constraints | yes | yes | yes (BETA/PROVISIONAL split on `collectionName`) | search only | no | no |
| Copernicus GLO-30 DEM adapter | yes | yes | yes (real crops) | yes (smoke 2026-09-03) | no | no |
| ERA5 adapter | yes | yes | yes (fakes) | no (no CDS key) | no | no |
| GACOS request/poll adapter | yes | yes | yes (fakes) | no (email workflow) | no | no |
| Feature cube (`build_cube`, Zarr v3, STAC) + `serac cube build/describe` | yes | yes | yes (`validate-cube` 23 checks) | n/a | no | no |
| DVC pipeline (`dvc.yaml`, `make dvc-remote`) | yes | yes | yes (`dvc stage list`, 14 stages) | no (no remote configured) | n/a | no |
| Message bus port + `InMemoryBus` | yes | yes | yes (contract test) | n/a | n/a | no |
| `RedisStreamsBus` | yes | yes | yes (fakeredis) | no (no Redis on dev machine) | n/a | no |
| FDSN waveform adapter (EarthScope, GEOFON, RESIF, INGV) | yes | yes | yes (fixtures) | yes — 366 fetched rows, 2.62 GB for M1, LH? sets for M2 | no | no |
| EarthScope Syngine adapter (Green's functions) | ADR-0016 | yes | yes (committed library) | yes — 419 fetched rows 2026-09-03 | n/a | no |
| SeedLink feed + ingestor | yes | yes | yes (fake client) | no (endpoint unverified) | n/a | no |
| USGS ComCat adapter | yes | yes | yes (57-event fixture) | fetched live 2026-09-03 | no | no |
| Hydrometric port + ICIMOD fixture adapter | yes | yes | yes | n/a | 2 reported stage changes, no clock time | no |
| RGI 7.0 glacier outlines (Bremen mirror) | yes | yes | yes | fetched live 2026-09-03 | n/a | no |
| Detector **stub** (`detector_stub.py`) | yes | yes (placeholder LP/SP ratio, threshold 10 untuned) | yes (golden on chamoli-2021) | n/a | **no — fires on pre-event background noise in both real fixtures** | no |
| CAP 1.2 renderer + **stub** + XSD validation | yes | yes | yes (offline XSD) | n/a | n/a | no |
| Replay + latency report (`serac replay`) | yes | yes | yes (chamoli-2021, langtang-2026, synthetic-lp-burst) | no | plumbing only — **defaults to `DetectorStub`**; `--detector discriminator` runs the real model, `serac stream run` cannot | no |
| Validation harness (`validate-*`, `validate-serac`, `promote`) | yes | yes | yes (**11 suites, 385 checks**; `promote` additionally requires a named human in `PROMOTE_APPROVED_BY`) | n/a | n/a | no |
| `underwriting-check` (runs the Lhende avoided-loss table) | yes | yes | yes | n/a | n/a | no |
| Ingest port + `BaseIngestAdapter` (dry-run, 5 GiB gate, credentials path, ledger) | yes | yes | yes | n/a | n/a | no |
| Seismic contracts (`SeismicTrace`, `Envelope`+codec, `DetectionCandidate`, `ForceHistory`, `CAPMessage`, `ReplayReport`) | yes | yes | yes | n/a | no | no |
| ObsPy MiniSEED codec + `Stage`/`Pipeline`/`Clock` skeleton | yes | yes | yes (round-trips the 4 real fixtures) | n/a | no | no |
| Fixtures: seismic, ComCat, CAP XSDs, DEM crops, S2 crops, S1/NISAR/CDSE listings, ESEC, LFH LH? sets | fetched 2026-09-03, sha256 in ledger | real | integrity tests | n/a | n/a | n/a |
| Docker Compose (`infra/docker/compose.yaml`) | yes | yes (file) | no | no | n/a | no |
| Deployment image (`infra/docker/Dockerfile`) | ADR-0014 | yes | yes (`tests/unit/test_deployment_image.py`: frozen lockfile, `serac` entrypoint, Python pin, build context, manifest image references) | **built and run 2026-09-04 — `linux/arm64` only; `serac --version` and a contracts drift check pass inside the container; never pushed to any registry** | n/a | no |
| Job manifests (`infra/jobs/*.yaml`, 7 files) | ADR-0014 | yes (files) | yes (`tests/unit/test_job_manifests.py`: every `command` resolves against the `serac` CLI, the `aws:` sketch is the same argv, README listing, cost bases, `status: designed`) | **no — none has ever been executed** | n/a | no |
| Governance docs (CLAUDE.md, ARCHITECTURE, ADRs, CREDENTIALS, DATA_SOURCES, EVENT_LIBRARY) | yes | yes | yes — every prose document (`tests/unit/test_docs_consistency.py`: paths resolve, no false absence claim, nothing in the tree deferred), plus DATA_SOURCES against the ledger (`tests/unit/test_data_sources_doc.py`) and ARCHITECTURE against the tree, the CLI and the latency report (`tests/unit/test_architecture_doc.py`) | n/a | n/a | n/a |

### M1 — seismic mass-movement discriminator

| Component | designed | implemented | tested-offline | tested-online | validated-against-events | production |
|---|---|---|---|---|---|---|
| Training-set build (`serac data build-discriminator-set`) — 1,925 windows / 308 groups, 297 positives | yes (card) | yes | yes | yes — 2.62 GB fetched 2026-09-03; 265 windows recorded `not_fetched` | n/a | no |
| Baseline classifier `lgbm-3class` 0.1.0 + sigmoid calibrator | yes (card) | yes | yes | n/a | **no — `validate-discriminator` fails: Langtang not detected under either split scheme** | no |
| Deep model `cnn-station-transformer` (120,091 params) | yes (card) | yes | yes | n/a | **no — not promoted; ΔF1 −0.016 [−0.205, +0.163], baseline retained** | no |
| `DiscriminatorDetector` behind the `Detector` port | yes (card) | yes | yes | n/a | **no — fired on Chamoli (`sliding_180s` 210 s, `batch_600s` 540 s); fired in neither mode on Langtang, 0 windows scored in 702 polls on the same 12-receiver selection** | no |
| Anti-tuning seal (`reports/m1/seal.json`, `seal_version` 2) | yes (card) | yes | yes | n/a | n/a | no |

### M2 — landslide force-history inversion

| Component | designed | implemented | tested-offline | tested-online | validated-against-events | production |
|---|---|---|---|---|---|---|
| gSF grid search + regularised single-force inversion (`serac.models.lfh`) | yes (card) | yes | yes (`validate-lfh` 22 checks, offline re-inversion to 1.7 %) | yes — Syngine + FDSN 2026-09-03 | **partial — 3 of 4 published reproductions overlap by interval (Bingham Canyon, Taan Fiord, Lamplugh); Chamoli refused** | no |
| Two mass estimators, published as a union (`MassEstimate`) | yes (card) | yes | yes (a point mass is unconstructible) | n/a | as above; medians 0.36–1.40 × published centres | no |
| Refusal rules (< 5 stations, > 200° gap, VR < 0.20) | yes (card) | yes | yes | n/a | **fired on all three new events — Langtang (3 stations, 317° gap), Chamoli (VR 0.089), Blatten (VR 0.191)** | no |
| Green's-function library (PREM `prem_a_20s`, Syngine) | ADR-0016 | yes | yes (committed, byte-stable) | yes — 419 rows | n/a | no |

### M3 — slope watch (L0/L1)

| Component | designed | implemented | tested-offline | tested-online | validated-against-events | production |
|---|---|---|---|---|---|---|
| Frozen track-selection rule (`SELECTION_RULE`, sha256 `d6c15960…`) | yes (card, pre-registered) | yes | yes | yes (DEM + ASF listings) | n/a | no |
| Slope-unit delineation (aspect octant × 250 m band; **not `r.slopeunits`**) | yes (card) | yes | yes | n/a | n/a | no |
| MintPy SBAS time series (2 AOIs, 260/260 and 257/257 interferograms) | yes (card) | yes | yes | yes — HyP3 2026-09-03 | n/a | no |
| Anomaly model v0 + tiers (Quiet / Elevated / Watch / insufficient_data) | yes (card, pre-registered) | yes | yes (`validate-watch` 36 checks, causality proved mechanically) | n/a | **no — Chamoli: labelled unit `insufficient_data` at all 56 steps, 0 of 780 source-zone units ever measurable. Langtang: 5 of 48 source-zone units measurable at 38 of 122 steps, 4 Quiet, 1 Elevated** | no |
| Optical feature tracking (orientation-correlation NCC; **not autoRIFT**) | yes (card) | yes | yes | yes (S2 COGs) | **no — noise floor degenerate on Langtang (median 0.0 m) and heavy-tailed on Chamoli; does not enter the tier** | no |
| v1 autoencoder hook | interface only | no | n/a | n/a | no | no |

### M4 — runout surrogate (L3)

| Component | designed | implemented | tested-offline | tested-online | validated-against-events | production |
|---|---|---|---|---|---|---|
| `serac-swe-voellmy` v0.2.0 solver (**NOT r.avaflow**) | yes (card) | yes | yes (9 verification cases: mass 1.8e-16, lake-at-rest exact, Ritter L1 falls monotonically) | n/a | **no — no independent simulator exists to cross-validate against; r.avaflow unobtainable** | no |
| r.avaflow Docker image (`infra/docker/ravaflow/`) | yes | **no — acquisition failed, recorded with dates and URLs** | n/a | n/a | n/a | no |
| Frozen 230-member ensemble (222 at 60 m, 8 at 30 m; design hash `ce679a8f…`) | yes (card) | yes | yes (`validate-runout` 30 checks) | n/a | **no — 45 of 230 members reach `rasuwagadhi-gyirong`; 0 of 230 reach `syabrubesi`, `betrawati` or `galchhi`** | no |
| Corridor FNO surrogate + transect regressor v0.1.0 | yes (card) | yes | yes | n/a | **no — 4 of 5 gates pass; 5–95 % arrival coverage 0.794 against a 0.85–0.95 target** | no |
| Cascade rules v0 (damming index, parametric breach) | yes (card) | yes | yes | n/a | **no — logistic midpoint and scale were chosen, not estimated; no dam inventory for this corridor** | no |
| Langtang sanity comparison against the recorded transect arrivals | yes (card) | yes | yes | n/a | **comparison only, never a calibration — and there is nothing to compare: the event record holds an arrival time for one of the four transects (`syabrubesi`, 13 min) and no member reaches it. 45 of 230 members reach `rasuwagadhi-gyirong`, where the record holds no arrival time** | no |

### M5 — avoided loss and alerting

| Component | designed | implemented | tested-offline | tested-online | validated-against-events | production |
|---|---|---|---|---|---|---|
| Avoided-loss computation (`AvoidedLossRequest → AvoidedLossResponse`) | yes (card) | yes | yes (`validate-e2e` 15 checks; an asset with no usable input is never zero) | n/a | **no — costed 0 of 14 Lhende assets; all 14 reported `undetermined`** | no |
| Damage functions (5 parameter sets) | yes (card) | yes | yes (assumption marker asserted per function) | n/a | **no — every parameter is an unsourced assumption** | no |
| CAP 1.2 generator + Ed25519 enveloped XML-Signature (RFC 9231) | ADR-0012 | yes | yes (signed and unsigned validate against the vendored XSD) | n/a | **n/a — no CAP message was produced on either replay** | no |
| `AlertSink` port + file/log and HTTP POST adapters | yes (card) | yes | yes | **no — nothing has ever been transmitted anywhere** | n/a | no |
| End-to-end replay lane (`serac cascade e2e`) | yes (card) | yes | yes | n/a | **no — both replays stop at the detection stage; no forecast and no CAP exist for Chamoli 2021 or Langtang 2026** | no |

## Known gaps

Numbered so `TODO` comments can reference them (`TODO(RELEASE_STATUS#n)`). Renumbered and
regrouped by component 2026-09-04; the eight citations in the tree were updated to match.

### Data and archives

1. **No calibrated pre-Langtang NISAR series.** Calibrated PROVISIONAL products exist only for
   acquisitions from 17 Jun 2026 (released 20 Jul 2026); BETA products Oct 2025–Jan 2026 are
   not inter-comparable; instrument gap 27 Jul–10 Aug 2026. As of 2026-09-03 `asf_search`
   returns only ancillary SCLKSCET files; NISAR is `listed`, never `fetched`.
2. **No open real-time Nepal/China hydrometric feed.** Nepal DHM gauges have no stable open
   API. The hydrometric adapter reads a fixture built from ICIMOD public reporting; anything
   else raises `DatasetNotFetchedError`.
3. **ERA5 and GACOS are absent.** No CDS key and no GACOS email workflow were available. The
   ERA5 cube layer is a labelled synthetic placeholder under `tests/fixtures/synthetic/`;
   GACOS is `not_fetched`. M3 therefore corrects the troposphere with MintPy
   `height_correlation`, which removes only the elevation-correlated part of the delay — the
   largest known error source in its velocities.
4. **CDSE fetch path exercised only with fakes.** CDSE search is public; downloads need OAuth
   credentials that were not available. M3's optical layer went through Earth Search instead.
5. **No open broadband seismic station within 300 km of Chamoli.** The Chamoli replay uses
   `NK.KKN` and `IC.LSA`; any Chamoli-based latency figure inherits that geometry.
6. **ComCat's landslide set is sparse and contributed nothing to M1.** `eventtype=landslide`
   returns 57 events since 2000, only 6 with M ≥ 4, mostly Alaska ml 1–2, and Chamoli 2021 is
   absent. Worse: `cli_data.py` never passed the committed fixture into the positive join, so
   **zero** of those 57 events are in the built M1 store. The bug is fixed; the store predates
   the fix and was deliberately not rebuilt (`reports/m1/build.json`, note 1).
7. **Langtang 2026 figures are largely press-attributed.** No peer-reviewed source exists yet;
   volume is `null`, and press-derived ranges carry `best: null`. ~~The four public transect
   timings M4 compares against are in this class.~~ **Corrected 2026-09-04:** that sentence was
   wrong, and in the direction that flattered the repository. The record holds an arrival time
   for **one** of the four transects — `syabrubesi`, 13 min, the difference between two clock
   times the Kathmandu Post states, `best: null`. `rasuwagadhi-gyirong` and `betrawati` carry
   `arrival_time_min: null` because the ~7.5 min and ~45 min figures that circulate publicly
   have no retrievable source (`docs/EVENT_LIBRARY.md`, "Figures seen and deliberately not
   entered"), and `galchhi` holds a **+9 m stage rise over 30 minutes**, which is not an arrival
   time. M4 nevertheless held all four as a `PUBLIC_TIMINGS_MIN` literal in
   `src/serac/models/runout/langtang.py` and described them as press-attributed. The literal is
   gone: `serac.models.runout.observed` derives every comparison target from the record, and
   `validate-runout` re-derives them and fails if a committed artifact carries anything the
   record does not (`langtang_targets_come_from_the_event_record`,
   `langtang_sanity_md_renders_from_its_json`).
8. **The seismic and ComCat fixture ledger rows carry no `event_id`/`aoi_id`**, so those
   columns show `-` in `serac events report`; the ledger is append-only and was not rewritten.
9. **517 HyP3 ledger rows are transient.** 20.05 GB of product zips were hashed on arrival,
   cropped to the AOI and deleted; those rows can never be re-hashed. The crops that replaced
   them are ordinary retained rows and are re-hashed by `validate-ingest`.
10. **No `dvc.lock`**: producing one requires running an ingest stage over the network.
11. **The cube's S1 layers still prefer the synthetic placeholder over real burst products.**
    `tests/unit/pipelines/test_layers_s2_s1.py` excludes `data/raw/hyp3_burst_insar/` to keep
    that behaviour pinned. The fix is for the cube pipeline to select by `raw_root` rather than
    scanning the ledger; until then the layer is not reading the 517 real interferograms M3
    fetched.
12. **Terrain layers in the Chamoli fixture cube are partial.** The committed GLO-30 crop covers
    the source zone, not the whole corridor AOI, so `dem`/`slope`/`aspect` cover 11.7 % of the
    cube grid and are `status: partial`, NaN elsewhere.
13. **The NISAR GCOV (HDF5) and ERA5 NetCDF-4 readers are untested against real products.**
    `h5py` 3.16.0 *is* in the locked environment (this entry previously said it was absent,
    which is no longer true), but NISAR is `listed` and ERA5 is synthetic, so neither reader has
    ever parsed a real product.

### M1 — discriminator

14. **`validate-discriminator` fails by design, and M1 is not validated.** The brief requires
    Langtang and Chamoli both detected; Langtang is not, under either split scheme. See the
    Gate status section above.
15. **M1 classifies the motivating event as `tectonic`.** On the Langtang window as the open
    archives held it eight days after the event — 2 of 12 selected receivers with
    response-removed data — the sealed model returns `tectonic` 0.464 against `mass_movement`
    0.447 (calibrated P(mass movement) 0.369). It would not have prevented the "M4.4
    earthquake" misreport. The receiver threshold was deliberately not lowered to admit the
    window.
16. **The headline F1 rests on 9 positives.** LORO-HMA: mass_movement F1 **0.516
    [0.333, 0.692]**, ROC-AUC 0.868 [0.631, 0.991], on 56 test windows over 9 groups. The
    time-forward fold is smaller still (7 positives, F1 0.375 [0.075, 0.714]). No point
    estimate from this component should be quoted without its interval.
17. **A demonstrated leak vector survives all ten leakage assertions.** Positives realise on
    average **+1.01** more receivers than their own matched negatives; `n_stations` alone gives
    **ROC-AUC 0.587**, better than chance. No feature counts receivers directly, but the
    cross-receiver aggregates (`*_mad`, `*_p90`, `lp_envelope_coherence`) are functions of how
    many traces contributed. Some of the reported skill may be archive density rather than
    source physics.
18. **Four windows are duplicated and they sit in the held-out fold.**
    `neg/sedongpu-2017-2018/*` appear twice. De-duplicated, F1 is 0.533 (n=52) against the
    reported 0.516 (n=56); **the reported number is the lower one**. Fixed in `catalog.py` for
    future builds; the store was not rebuilt.
19. **The anti-tuning seal covers named constants, not code.** The float32-overflow fix changed
    behaviour without moving a constant, so `config_hash()` did not trip and the re-seal was a
    manual version bump. Read the seal as protection against hyperparameter tuning between
    scorings, not against all behavioural change.
20. **The ≤ 60 s detection budget is physically unreachable for this component.** Measured on
    Chamoli: 210 s (`sliding_180s`) and 540 s (`batch_600s`), against theoretical floors of
    153 s and 573 s set by travel time to a ≥ 100 km receiver plus the record length a
    20–100 s band needs. No amount of compute moves the floor.
21. **The discriminator Zarr store is not committed**, so the byte-level re-hash assertion is
    skipped on a fresh clone. The window index and `chunk_hashes.tsv` are committed, so every
    other leakage assertion still runs.
22. **Negatives are not magnitude-matched** (ESEC publishes no magnitude), and the noise class
    means "no catalogued source", not "quiet".
23. ~~**No job manifest exists for full deep-model training.**~~ Fixed:
    `infra/jobs/discriminator-train-deep.yaml`. It is still `status: designed` and has never
    been executed, and its own notes say that executing it is expected to change nothing — the
    binding constraint on M1 is 9 held-out positives, not epochs.

### M2 — force-history inversion

24. **M2 refuses every event serac cares about.** Langtang (3 contributing stations against a
    minimum of 5, 317° azimuthal gap), Chamoli (variance reduction 0.089, median pre-event SNR
    0.70, 180° gap) and Blatten (VR 0.191 against a 0.20 floor) all return
    `status: failed`. The refusals are results, not defects — but they mean serac has produced
    no mass for any recent event, and therefore no release volume for M4.
25. **`duration_s` is not a usable output.** serac returns 296 s for Taan Fiord against a
    published 90 s: the 300 s source window and the second-difference penalty spread energy
    across the whole window, so the 5 %-of-peak threshold reads back the window length. It is
    published because the contract has a field for it, and it should not be believed.
26. **The three passing reproductions pass by interval overlap, not by agreement.** Median ÷
    published centre is 0.96 (Bingham Canyon), 1.40 (Lamplugh) and **0.36** (Taan Fiord); the
    intervals span an order of magnitude. Taan Fiord is close to the [1/3, 3] sanity edge.
27. **A systematic low bias in peak force is possible and uncorrected.** On the synthetic round
    trip the L-curve corner recovers peak force at **0.36×** and shape at r = 0.41, where a
    lightly regularised solution gives 1.09× and r = 0.81. The criterion is the one the brief
    specifies and was not changed after this was measured.
28. **One Earth model.** PREM only; the bootstrap does not resample it, so whatever a different
    model would change lies outside every published interval.
29. **The cold latency figure depends on a third-party service.** ~115 s cold assumes Syngine
    answers at 16 calls/s; the endpoint proved intermittent during the session. A per-AOI
    Green's-function library or a local `instaseis` database is a **deployment prerequisite**
    (`infra/jobs/m2-greens-library.yaml`). 90 % of the warm cost is a serial 200-draw bootstrap
    that is embarrassingly parallel and was not parallelised.
30. **Chamoli's comparison interval is derived, not published.** No paper retrieved in session
    gives a Chamoli mass; 5.9–6.5 × 10¹⁰ kg comes from a published volume and an assumed
    density range.

### M3 — slope watch

31. **The measurability thresholds are not pre-registered, and the result depends on them.**
    `MIN_PIXEL_TEMPORAL_COHERENCE = 0.40` and `MIN_PIXELS_PER_UNIT = 5` decide whether a unit is
    measurable at all and are more decisive than any pre-registered parameter. At a 0.20 cut the
    Chamoli source zone would have had 111 nominally measurable units instead of 0. They were
    introduced before any backtest ran and never edited, so this is not post-hoc tuning — but
    the headline is a statement about this configuration, not a threshold-free fact about
    C-band InSAR. The full sweep is committed in both backtest reports.
32. **Chamoli's source zone was never measurable, so no precursor question was ever asked.**
    0 of 780 source-zone units measurable at any of 56 steps; AOI median temporal coherence
    0.139. The labelled unit is west-facing (aspect 271°) with a **signed LOS sensitivity of
    −0.074** on the chosen ascending track. This is an observability result and must never be
    reported as "no precursor detected".
33. **Langtang is footprint-limited, not coherence-limited.** Median temporal coherence over
    imaged pixels is 0.622 and 83.9 % clear 0.40, but **19,385 of 26,935 units (72 %) lie
    outside the processed burst footprint** — a Sentinel-1 subswath is ~85 km wide and the AOI
    is a 100 km corridor. A second track is the fix, and it does not exist here.
34. **The Langtang interferogram archive is truncated to 2022-01-05 → 2026-08-19**, a disclosed
    budget choice. A precursor that began before 2022 is outside what this run could have
    detected.
35. **The optical layer's significance flag is not usable at v0.** The pre-registered *median*
    noise floor is degenerate on well-correlated ground (Langtang median floor 0.0 m, because
    stable chips land on the zero-shift sample) and heavy-tailed on Chamoli (median 2.6–10.3 m,
    p95 54–59 m, on 512 stable chips). Left uncorrected because fixing it after seeing it would
    be tuning. The optical layer does not enter the watch score.
36. **A Quiet tier on competent rock is weak evidence of stability.** Brittle crystalline
    failure can occur with little or no resolvable precursory displacement, and that is the
    class of event serac exists for.
37. **`insufficient_data` can be misread as safe.** A unit outside the footprint or below the
    LOS-sensitivity floor is not being watched. It is never reported Quiet, but a reader
    skimming a tier table can still mistake absence for safety.
38. **Slope units are not `r.slopeunits`** and not a hydrological half-basin delineation (GRASS
    containers are amd64-only on the dev machine), so they are not comparable with the
    published slope-unit literature.

### M4 — runout

39. **The solver is `serac-swe-voellmy` v0.2.0, NOT r.avaflow.** r.avaflow could not be
    obtained: no official GRASS addon (404), no canonical public repository, avaflow.org behind
    a registration wall. The acquisition attempt is recorded with dates and URLs in
    `infra/docker/ravaflow/README.md`. **Cross-validation against an independent simulator is
    outstanding**, so structural bias and implementation error cannot be separated.
40. **The corridor stops the flow, and that is the dominant finding.** 87.4 % of the thalweg
    lies below **4.57°** — the slope at which a Voellmy Coulomb coefficient of 0.08 stops being
    able to drive the flow — with a median of 0.42° over 499 binned segments at 30 m. Ensemble
    reach: median 13.88 km, max 28.72 km, against a 100 km corridor whose furthest transect is
    at 97.0 km.
41. **The ensemble is not 230 equivalent 30 m runs.** 222 of 230 members ran at 60 m because a
    30 m member costs 9–11×. The 60 → 30 m convergence study justifies it (13 m of reach,
    0.09 %; inundation IoU 1.0 at 1 m) but is one parameter vector.
42. **The surrogate fails one of its five gates.** 5–95 % arrival coverage **0.794** against a
    0.85–0.95 target. The arrival-MAE gate that passes (46.5 s, ≤ 90 s) **rests on 3 held-out
    members at one transect**; three of four transects scored nothing at all.
43. **The solver's arrival times are biased late, twice over.** Operator splitting puts modelled
    terminal velocity **8.7 % below** the analytic Voellmy value at the production CFL of 0.45;
    and the release is emplaced *at rest* at the head of the centreline, so roughly **1,300 m**
    of fall from the Langtang Lirung flank contributes no initial kinetic energy.
44. **Single-phase physics.** Ice fraction enters only through mixture density. The solver
    cannot produce a fluid-rich front running ahead of a solid-rich body, which is the mechanism
    behind the long fast runout of real rock-ice cascades.
45. **`erodible_depth` is a parametric mantle, not a measurement.** 5 m maximum, tapered above
    35° slope. No sediment-thickness survey exists for this corridor.
46. **The sub-60 m gorge is unresolved.** The Bhote Koshi gorge is under 60 m wide in places, so
    it spans fewer than two cells at 30 m. Superelevation, run-up and channel blocking are not
    represented, and the damming numbers are order-of-magnitude indicators.
47. **The damming index is not a probability.** A deposit-to-channel-depth ratio through a
    logistic whose midpoint and scale were chosen, not estimated. No landslide-dam inventory
    exists for this corridor.
48. **The corridor `(x, y) → (s, n)` frame is invalid outside 38.8 % of the buffer mask at
    30 m.** That set is published as `CorridorTerrain.frame_valid` rather than averaged in;
    chainage binning needs only `s` and is unaffected, but any future use of `n` must respect
    it.

### M5 — avoided loss and alerting

49. **Every damage function, replacement value and warning-benefit share is an unsourced
    assumption.** No depth-damage curve for Himalayan run-of-river hydropower, Nepali highway
    bridges or Nepali settlement stock was fetched. 27 assumptions are printed with every
    `underwriting-check` run. The monetary outputs are what the stated parameters imply; they
    are not a loss estimate.
50. **The avoided-loss table costs 0 of 14 Lhende assets.** 10 sit at transects no ensemble
    member reaches; 4 sit at `rasuwagadhi-gyirong`, which 45 of 230 members reach, but the
    committed artifacts record arrival times and not stages, so there is no depth to put into a
    damage function. All 14 are reported `undetermined`, **never zero**.
51. **No exposure values and no populations exist in the AOI layer.** 14 of 14 assets carry no
    replacement value; 3 of 3 settlements carry `population: null`. Lives-in-warned-zone is
    always `null`. This is the cheapest gap to close and the one that most limits what M5 can
    say.
52. **Both end-to-end replays stop at detection, and no CAP message was produced.** The
    committed fixtures carry 2 receivers against the detector's 3-station minimum, so no window
    is ever scored on a fresh clone; and both events would stop again at M2's refusal even with
    the full receiver set. **No end-to-end latency has been measured**, and the 187.6 s
    (Langtang) / 217.1 s (Chamoli) figures in the reports are counterfactuals assembled from
    per-stage measurements, never delivered lead times.
53. **The CAP signature proves bytes, not identity.** No certificate chain, no revocation, no
    trust store. A recipient who treats a valid signature as authorisation is mistaken. serac is
    not a warning authority in any jurisdiction and has no dissemination path.
54. **Totals are comonotonic**, summing interval endpoints with endpoints. That is the widest
    honest bound for parameters that move together; it is not a convolution of independent
    uncertainties and must not be read as a loss distribution.
55. **`validate-e2e` passes while the chain produces nothing.** An early stop is the outcome to
    record rather than a harness failure. The suite's own warnings say so, and so does
    `reports/MODEL_CARD_cascade.md`; a green gate here is not a working system.

### Streaming, infrastructure and process

56. **The live stream lane still runs `DetectorStub`, and replay still defaults to it.**
    `serac replay --detector discriminator` now runs the trained model, but `stub` is the
    default and `cli_stream.py` — the lane a real deployment would use — still imports the
    placeholder unconditionally. `validate-stream`'s 33 checks are therefore checks on the stub.
    Note what selecting the real detector shows: on the Chamoli fixture it emits **0 detections
    against the stub's 4**, because two receivers are available and it needs three. Selectable
    is not retired.
57. **The detector stub fires on pre-event background noise.** On both real fixtures the
    placeholder LP/SP ratio exceeds the placeholder threshold in the first evaluable window
    (Chamoli `NK.KKN..BHZ`, 2021-02-07T04:49:59Z, ratio 233; `IC.LSA.00.BHZ` ratio 2056). The
    threshold was deliberately not tuned to change this.
58. **SeedLink endpoint unverified.** `geofon.gfz.de:18000` is configuration, not a verified
    endpoint. No live SeedLink stream has ever reached this code.
59. **Redis Streams untested against a live server.** `RedisStreamsBus` is unit-tested with
    fakeredis only; the `redis`-marked test has never run.
60. **Docker Compose has never been run.** This entry previously read "no Docker is installed
    on the dev machine"; that stopped being true on 2026-09-04, when the deployment image was
    built and run there (Docker Engine 29.7.2). `compose.yaml` itself has still never been
    brought up, so the Redis service, its healthcheck and its volume are all unverified and
    `RedisStreamsBus` remains fakeredis-only (gap 59).
61. **No job manifest has ever been executed.** Every `estimated_core_hours` figure is an
    estimate with a stated basis. The README's table now lists all seven manifests, and
    `tests/unit/test_job_manifests.py` fails if one is added without being listed, if a cost
    figure carries no basis, or if a manifest claims to have been executed. Since gap 69 the
    same test also resolves every manifest's argv against the installed CLI, so "never
    executed" no longer implies "never checked": the commands are known to parse, and what is
    unknown is what they cost and produce when run.
62. **`SourceRef` exists twice** — `domain/common.py` for the event library and
    `models/lfh/references.py` for the force-history references. The LFH copy carries extra
    resolution provenance, so they are not redundant, but the duplication let a review fix land
    on one copy and silently miss the other.
    `tests/contract/test_source_ref_copies_agree.py` now fails loudly on divergence; merging the
    two properly is outstanding.
63. ~~**`make underwriting-check`'s help string is stale.**~~ Fixed: the Makefile, `README.md`
    and `CLAUDE.md` now describe what the target does (the Lhende avoided-loss computation,
    exit 0) rather than the Prompt 1 stub it replaced.
64. **`reports/validation/latest.json` is stale**, because `validate-serac` reaches no stamp
    while a required suite reports an unmet criterion. It records `0b70091` and seven suites.
    Do not read it as current; the per-suite reports in `reports/validation/` are.
65. **No GitHub issues can be opened until `gh auth login`.** `TODO`s therefore reference
    entries in this list.
66. **Running the gates dirties the tree, and `make promote` requires a clean one.**
    `validate-e2e` rewrites the tracked `reports/e2e/*.json` and `.md` with fresh timestamps and
    wall-clock timings on every run, so `make validate-serac` leaves the working tree modified
    and a `promote` immediately afterwards would refuse on `tree_clean: false`. Masked today
    because promotion is blocked by gap-free reasons above, but it must be resolved — by
    excluding the volatile fields from the committed artefacts, or by not tracking them — before
    any component can be promoted.
67. **The one promotion record on disk names no approver.**
    `reports/promotion/71f3426c0d2316448647e4b9233c10fbed08dc5a.json` was written on
    2026-09-03, before `promote` required `PROMOTE_APPROVED_BY`, so it has no `approved_by`
    field. It is left exactly as written: filling one in now would be inventing a fact about
    who approved that tree. `PromotionRecord` requires the field, so this is the last record
    that can ever lack one, and any reader of that file should treat its promotion as
    unattributed rather than as approved by anybody in particular.
68. **The deployment image has never been published, and has been built for one
    architecture.** Until 2026-09-04 the tree had no `Dockerfile` at all: CLAUDE.md
    non-negotiable 5, ADR-0014 and `docs/ARCHITECTURE.md` all named a plain Docker image as the
    deployment unit, `infra/docker/README.md` said the `Dockerfile` was "not part of Prompt 1",
    and three Prompt 2 manifests named `ghcr.io/dizzy1900/serac:0.1.0`, one of them annotated
    "built from infra/docker/Dockerfile" — a tag nobody had pushed, built from a file that did
    not exist. `infra/docker/Dockerfile` now exists and was built and exercised (see the build
    record in `infra/docker/README.md`): `serac --version` reports `serac 0.1.0` inside the
    container, and `schema export --check` in the container reproduces all 22 committed
    contracts. What is still **not** true, and is what this entry is about:

    * **Nothing has been pushed to any registry.** No job manifest can be submitted without an
      operator building and tagging the image first. The manifests therefore name the
      unresolvable placeholder `<registry>/serac:<git-sha>` rather than a tag that would fail
      with `manifest unknown`.
    * **Only `linux/arm64` was built.** Every `aws:` annotation in `infra/jobs/` assumes amd64,
      and the arm64 build already had to compile `obspy` from its sdist for want of a wheel; an
      amd64 build has never been attempted.
    * **The ML variant has not been built.** The two GPU manifests want the image with
      `--extra ml --extra surrogate`; only the base image exists, and no GPU host has run it.

    `tests/unit/test_deployment_image.py` fails if the `Dockerfile` disappears, if it installs
    anything outside `uv sync --frozen`, if the entrypoint stops being `serac`, if a manifest
    names a resolvable image reference without `image_published:` provenance beside it, or if
    any `infra/` file points at a repository path that does not exist.
69. ~~**Two job manifests named CLI flags that never existed.**~~ Fixed 2026-09-04.
    `hyp3-insar-batch.yaml` told an operator to run `serac ingest hyp3 --watch --confirm-bytes
    N` and `s1-stack.yaml` to run `serac ingest s1 --product GRD --confirm-bytes N`, and both
    repeated the same argv in their `aws.batch_job_definition` sketches. The CLI has never had
    `--watch`, `--product` or `--confirm-bytes`, and neither command carried `--yes`, so all
    four would have exited non-zero on start; `s1-stack.yaml` also passed `--relative-orbit ""`
    to an integer option, and `runout-ensemble-10k.yaml` passed `--workers "$(NPROC)"`, which
    the exec-form `ENTRYPOINT ["serac"]` never expands. Both EO files opened with `# NOTE: the
    CLI flags below are PROPOSED; reconcile with serac ingest when the adapters land`; the
    adapters landed in Prompt 1 and the note stayed. The commands are now the real ones, the
    notes are gone, and `tests/unit/test_job_manifests.py` resolves every `command` and
    `followup_command` in every manifest against the installed CLI — sub-command, flags and
    the manifest's own parameter values — requires the `aws:` sketch to be that same argv with
    `${param}` written `Ref::param`, rejects a substitution no shell is there to expand, and
    requires an ingest job to carry exactly one of `--dry-run`/`--yes`. The manifests remain
    unexecuted (gap 61): this establishes that they would start, not that they would finish.

    Two consequences of the real CLI are now written into the manifests rather than papered
    over by the invented flag. `--confirm-bytes` was standing in for a non-interactive way past
    the 5 GiB ask-first gate; there is none, and adding one would weaken non-negotiable 7. So
    `s1-stack.yaml` records that a task must be sharded until its estimate is under the gate or
    be run with a stdin attached, and `hyp3-insar-batch.yaml` records that HyP3 publishes no
    product size before a job completes — the plan's estimate is `null`, the gate therefore
    asks on **every** run, and only its `followup_command` (`--poll`, which submits nothing and
    downloads jobs already confirmed at submission) can run unattended.
70. ~~**`docs/DATA_SOURCES.md` described a tree that had moved on.**~~ Fixed 2026-09-04. Four
    ledger sources had no section at all — `iris_syngine` (419 rows), `esec_spud` (6),
    `rgi_glaciers` (11) and `simulation_output` (1,591) — as did the burst-InSAR adapter behind
    3,619 fetched `hyp3_insar` rows, and `vendored_schema`. Two of the five FDSN data centres
    serac fetched from (RESIF, INGV) and a third that appears only in the ledger (ORFEUS) were
    absent from the FDSN heading, which named EarthScope and GEOFON only. Nine cells still
    tagged modules and fixtures "(planned)" that had been committed for weeks — the FDSN,
    SeedLink, ComCat, Overpass and `sources fetch` adapters, and the DEM, seismic, ComCat and
    Overpass fixtures. Two sentences said `h5py` was absent from the locked environment while
    `h5py` 3.16.0 was installed (gap 13 had already been corrected and the data-source document
    had not). One cell said "no real HyP3 product in the tree" beside 3,102 retained AOI crops,
    and the Sentinel-1 cell explained the synthetic cube layers by "until someone fetches"
    rather than by gap 11, which is the real reason.

    The document is now checked rather than trusted. `tests/unit/test_data_sources_doc.py`
    fails if a `source` or an `adapter` value that has written a ledger row has no section, if
    `DataSource` and the documented `Ledger source` rows differ in either direction, if a host
    serac fetched bytes from is not named, if a module under `adapters/eo`, `adapters/seismic`
    or `adapters/hydro` is neither documented nor listed as reading no source, if a repository
    path cited there does not exist, if something already in the tree is still tagged as future
    work, or if a claim that the environment lacks a package names one that imports. Every one
    of the failures above is caught by one of those nine checks. What the test cannot check is
    whether prose is *true* — only whether it is consistent with the ledger and the tree.
71. **The `SLC-BURST` listing cache is written under `data/` with no ledger row.**
    `src/serac/adapters/eo/asf_bursts.py` writes
    `data/interim/watch/bursts/<aoi>_<start>_<end>.json` and appends no `ManifestEntry`, while
    CLAUDE.md's fixture policy says nothing may be written under `data/` without one. Its
    docstring claimed the listings were ledgered; that sentence was wrong and has been
    corrected rather than the ledger backfilled, because the listing is the input to the frozen
    M3 track-selection rule and a row written now would carry a retrieval time and a checksum
    for bytes fetched on 2026-09-03. Found while reconciling `docs/DATA_SOURCES.md` against the
    ledger (gap 70). The listing files themselves are DVC-tracked and are not in git.
72. ~~**`docs/ARCHITECTURE.md` described the tree of the previous prompt.**~~ Fixed 2026-09-04.
    Dated 2026-09-03 and never revised when M1–M5 merged, it said the LFH inversion and the
    cascade surrogate were `planned (P2)` with their "module path decided in Prompt 2" while
    `src/serac/models/lfh/`, `src/serac/models/runout/` and `src/serac/cascade/` were on `main`;
    that there were 18 contracts (22) and 6 validation suites (11); that `ForceHistory` was
    interfaces only with `status: not_implemented` and avoided loss would be "populated in P2",
    after M2 and M5 had populated both; that NetCDF-4 reads need `h5py`, "not in the lock",
    which ships `h5py` 3.16.0; that HyP3 had been "exercised with a fake HyP3 only" against
    4,136 real ledger rows and 517 jobs submitted to the live service; and that "Prompt 2 owns"
    a latency measurement Prompt 2 had made. Nine packages — `src/serac/models/` and its four
    component packages, `src/serac/cascade/`, `src/serac/alerting/`,
    `src/serac/adapters/alerting/`, `src/serac/adapters/tracking/` and
    `src/serac/pipelines/layers/` — had no row at all, and six CLI sub-apps were missing from
    the entrypoint's list.

    The document now carries a dated revision, a new section 3.4 for the five model components,
    a rewritten section 4.2 for the lane as it actually runs, and a section 4.3 that reports the
    **measured** detection latency (210 s `sliding_180s`, 540 s `batch_600s`, against a 60 s
    budget and floors of 153 s and 573 s, gap 20) instead of promising the measurement.

    The deeper failure was that gap 70's fix was written for one document. Three of its checks
    were never specific to `docs/DATA_SOURCES.md` — paths resolve, nothing already in the tree
    is tagged as future work, no claim that the environment lacks a package it imports — and
    `docs/ARCHITECTURE.md` drifted all three ways because nothing generic existed. Those three
    now live in `tests/unit/doc_claims.py` and run against **every** prose document from
    `tests/unit/test_docs_consistency.py`, which also fails if a new document appears under
    `docs/` and is not listed. ADRs and model cards are dated records and are checked for paths
    and absence claims but never for deferrals; this file is checked for paths only, because it
    quotes the wording of the claims it has corrected and a checker cannot tell a quoted mistake
    from a fresh one. On top of those, `tests/unit/test_architecture_doc.py` asserts that every
    package under `src/serac/` and every command on the `serac` app is named, that the stated
    contract and suite counts equal `contracts/` and `REQUIRED_SUITES`, that every `Status` cell
    carries a tag the document's own legend defines, that nothing the ledger shows has fetched
    real bytes is described as exercised against a fake, and that the latency figures match
    `reports/m1/latency_chamoli-2021.json`. Running the same checks over the rest of the tree
    also caught a CLAUDE.md path that pointed at src/validation/ rather than
    `src/serac/validation/`, README's "Models come later" and
    its `ADR-0001 … ADR-0015` range, and a README bullet still calling the detector "planned".
    What none of it checks is whether the prose is true — only whether it is consistent with the
    tree.

73. ~~**M4 compared against four transect timings the event record does not hold.**~~ Fixed
    2026-09-04. `src/serac/models/runout/langtang.py` held
    `PUBLIC_TIMINGS_MIN = {rasuwagadhi-gyirong: 7.5, syabrubesi: 13.5, betrawati: 45.0,
    galchhi: 30.0}` with a docstring saying the same figures carried `best: null` in
    `data/events/langtang-lhende-2026.json`, `reports/runout/langtang_sanity.md` called them
    "press-attributed figures", and Known gap 7 above said the four timings M4 compares against
    are press-derived ranges. The record holds **one** of them. Rasuwagadhi and Betrawati carry
    `arrival_time_min: null` with the record's own sentence — the figures circulate publicly
    without attribution and no retrieved source states them — Galchhi's 30 minutes is the window
    of a +9 m stage rise rather than an arrival, and 13.5 was a midpoint of a span the record
    does not carry either (it holds 13). Two unattributed numbers were being reported as
    provenanced observations, and the mismatch table quoted a distance from one of them.

    The literal is gone rather than corrected, because a literal cannot be checked against
    anything. `src/serac/models/runout/observed.py` derives every comparison target from the
    event record through `MassMovementEvent`: a transect becomes a target only where the record
    holds an `arrival_time_min` `Range`, the mismatch is the signed distance from the recorded
    `low`–`high` interval rather than from a midpoint nobody published, and a transect the
    record leaves `null` carries the record's reason into the report instead of a number. Two
    new gates in `validate-runout` keep it that way: `langtang_targets_come_from_the_event_record`
    re-derives the targets and fails on any bound or `source_ref` the record does not hold, and
    `langtang_sanity_md_renders_from_its_json` re-renders the write-up from its own artifact, so
    the prose cannot drift from the gated payload by hand. The comparison now has one target
    (`syabrubesi`, 13 min) that **no member reaches**, which is a weaker result than the one the
    report used to state; `reports/runout/langtang_sanity.md` says so.
