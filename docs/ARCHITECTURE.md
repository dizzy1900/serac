# serac architecture

Date of record: **2026-09-04**, revising the record of 2026-09-03. The revision is the whole of
Prompt 2: five model components (M1 discriminator, M2 force-history inversion, M3 slope watch,
M4 runout, M5 cascade/avoided loss) merged to `main`, and with them nine packages, six CLI
sub-apps, five validation suites and four contracts that the 2026-09-03 record did not know
about. Sections 3.4, 4.2 and 4.3 are new or rewritten; every count in this document is now
derived from the tree by `tests/unit/test_architecture_doc.py` rather than remembered.

Status legend used throughout:

| Tag | Meaning |
|---|---|
| `present` | in the tree on `main` today and exercised offline by `make test` |
| `unproven` | in the tree, but the external thing it talks to has never been reached: no credential, no live endpoint, no registry |

Numbers in this document are either quoted from the founding brief, measured and cited to the
report that holds the measurement, or labelled as **design targets**. A design target is not a
measurement. `RELEASE_STATUS.md` is the ledger of what has actually been built, tested and
gated; where a row here is qualified, it names the RELEASE_STATUS entry that explains it.

## 1. C4 level 1 — system context

```
 +------------------------+        +----------------------------+        +------------------------+
 |  Data providers        |        |  serac                     |        |  Downstream consumers  |
 |                        |        |                            |        |                        |
 |  ASF / Earthdata (S1,  | -----> |  batch EO lane             | -----> |  decision layer        |
 |    NISAR, HyP3)        |        |    inventory + watch (L0/1)|        |  financial / avoided-  |
 |  CDSE (S2 L2A)         |        |                            |        |    loss layer          |
 |  Copernicus GLO-30     |        |  real-time seismic lane    |        |  dashboards, CAP       |
 |  CDS (ERA5), GACOS     |        |    detect + cascade (L2/3) |        |    consumers           |
 |  FDSN / SeedLink       | -----> |                            |        |                        |
 |  IRIS Syngine, ESEC    |        |  model components M1-M5    |        |  integrate ONLY via    |
 |  USGS ComCat           |        |  event library + AOIs      |        |  contracts/*.json      |
 |  OSM, ICIMOD, Crossref |        |  provenance ledger         |        |                        |
 +------------------------+        +----------------------------+        +------------------------+
```

- Providers are read through adapters that record every retrieval (or refusal) in
  `data/manifest.jsonl`. No provider is trusted to be present: a missing dataset is
  `status: not_fetched` and the code fails loudly rather than substituting values.
- Consumers see only the JSON Schemas in `contracts/` (`avoided-loss.v0.json`,
  `cap-message.v0.json`, `cascade-forecast.v0.json`, `force-history.v0.json`,
  `slope-watch-state.v0.json`, `replay-report.v0.json`, …; `present` (22 contracts), each
  exported from its pydantic model by `serac schema export`). They never import `serac`.
- serac owns no satellites and no seismic network. **The constellation is bought, not
  built.** Observation cadence is whatever the providers deliver (section 5).

## 2. C4 level 2 — containers

| Container | Role | Runs as | Status |
|---|---|---|---|
| CLI `serac` | single entrypoint (typer) for `ingest`, `stream`, `events`, `cascade`, `alerting`, `cube`, `sources`, `aoi`, `data`, `models`, `runout`, `lfh`, `validate`, `schema`, `replay`, `promote`, `underwriting-check` | `uv run serac …`, or the deployment image (`infra/docker/Dockerfile`) | `present`; the image builds and runs, but **no image has been pushed to any registry** (gap 68) |
| Batch EO lane | STAC search → `data/raw` → `data/interim` → Zarr feature cube per AOI → STAC catalog | `make`/DVC stages locally; `infra/jobs/*.yaml` at scale | `present`; ERA5, GACOS and NISAR data are still `not_fetched` (gaps 1, 3) |
| Real-time seismic lane | SeedLink → bus → detector → inversion → cascade surrogate → CAP | long-running processes reading the bus | `present`; `serac stream run` still mounts `DetectorStub`, and only `serac replay --detector discriminator` runs the trained model (gap 56) |
| Model components M1–M5 | `src/serac/models/` and `src/serac/cascade/`: discriminator, force-history inversion, slope watch, runout ensemble + surrogate, avoided loss | `serac models`, `serac lfh`, `serac runout`, `serac cascade`; `src/serac/cli_watch.py` for M3 | `present`; **none is validated against events and none has been promoted** (Gate status in `RELEASE_STATUS.md`) |
| Message bus | Redis Streams behind a synchronous `MessageBus` port; in-memory adapter for tests | `infra/docker/compose.yaml` (`redis:7-alpine`) | `unproven`; fakeredis only, no live server and Compose has never been brought up (gaps 59, 60) |
| Storage | Zarr v3 cubes, GeoParquet vectors, pystac catalogs under `data/`; DVC remote from env | filesystem + DVC | `present`; no DVC remote configured and no `dvc.lock` (gap 10) |
| Provenance ledger | `data/manifest.jsonl`, append-only JSON Lines | file | `present` |
| Validation harness | `validate-*` suites writing `reports/validation/<suite>.json`; `promote`; `underwriting-check` | `make` | `present` (11 suites); `make validate-serac` is RED on an unmet criterion of the brief (gap 14) |

The eleven suites are `validate-events`, `validate-aoi`, `validate-ingest`, `validate-cube`,
`validate-stream`, `validate-contracts`, `validate-lfh`, `validate-discriminator`,
`validate-runout`, `validate-watch` and `validate-e2e`; `REQUIRED_SUITES` in
`src/serac/validation/promote.py` is the list, and `serac validate all` runs all of them even
when one fails.

Deployment unit: one plain Docker image containing the `serac` package, built by
`infra/docker/Dockerfile` (ADR-0014). Every install in it goes through `uv sync --frozen`, so
the image cannot hold a dependency set `uv.lock` does not describe; the GPU variant is the same
file built with `--build-arg EXTRAS="--extra ml --extra surrogate"` and has never been built.
**Nothing has been pushed to a registry**, so `infra/jobs/*.yaml` name the unresolvable
placeholder `<registry>/serac:<git-sha>` rather than a tag an operator would fail to pull.
Local development uses Docker Compose (`infra/docker/`); scaled runs are job manifests in
`infra/jobs/` written for a generic container host and annotated for AWS Batch / EC2.

## 3. C4 level 3 — components

### 3.1 Cross-cutting

| Component | Path | Status |
|---|---|---|
| Errors | `src/serac/errors.py` (`SeracError`, `DatasetNotFetchedError`, `CredentialsMissingError`, `NotImplementedYetError`) | `present` |
| Settings (`.env`) | `src/serac/settings.py` (`SeracSettings`, `get_settings()`) | `present` |
| Provenance contract | `src/serac/domain/manifest.py` (`ManifestEntry`, `DataSource`, `ManifestStatus`, `Retention`, `Provenance ∈ {real, derived, synthetic}`, ADR-0016) | `present` |
| Ledger port | `src/serac/ports/ledger.py` (`ManifestLedger`: `append`, `entries`, `query`, `latest`) | `present` |
| Ledger adapter | `src/serac/adapters/storage/manifest_ledger.py` (`JsonlManifestLedger`, `sha256_of_file`) | `present` |
| Common contracts | `src/serac/domain/common.py` (`Range`, `SourceRef`, `FieldNote`, `AttributedEstimate`); `domain/geometry.py` (`Point`, `LineString`, `Polygon`, `MultiPolygon`) | `present`; `SourceRef` exists twice and a contract test pins the copies together (gap 62) |
| Event contract | `src/serac/domain/events.py` (`MassMovementEvent`, `EventTime`, `SeismicAttribution`, `Precursor`, …) | `present` |
| Geo contracts | `src/serac/domain/geo.py` (`AOI`, `GridSpec`, `SlopeUnit`, `Transect`, `ExposedAsset`) | `present` |
| Watch contract | `src/serac/domain/watch.py` (`SlopeWatchState`, `WatchTier`, `WatchInsufficientReason`) → `contracts/slope-watch-state.v0.json` | `present`; carries no failure date and `validate-watch` fails the build if one appears |
| Force-history contract | `src/serac/domain/force_history.py` (`ForceHistory` 0.2.0, `MassEstimate` as a strict interval, `AEff`) → `contracts/force-history.v0.json` | `present` and populated by M2; `status` is one of `not_implemented`, `computed`, `failed`, and a refusal is a result (gap 24) |
| Forecast contract | `src/serac/domain/forecast.py` (`CascadeForecast` 0.2.0, `TransectArrival`, `SecondarySurge`, `DammingEstimate`, `ConfidenceTier`) | `present`; no forecast has yet been produced for a real event (gap 52) |
| Avoided-loss contract | `src/serac/domain/avoided_loss.py` (`AvoidedLossRequest`/`Response`, `AssetScenarioLoss`, `LossBlockedBy`) → `contracts/avoided-loss.v0.json` | `present` and populated by M5; every Lhende asset comes back `undetermined` for want of exposure values, never zero (gaps 50, 51) |
| Detection contract | `src/serac/domain/detection.py` (`DetectionCandidate` 0.2.0, `DetectionLocation`, `ContributingStation`) | `present` |
| Bus/codec contracts | `src/serac/domain/envelope.py`, `domain/codec.py` (schema name + version registry), `domain/topics.py` | `present` |
| Schema export | `src/serac/domain/schema_export.py`, `serac schema export`, drift test in `tests/contract/` | `present` (22 contracts) |
| Run tracking | `src/serac/ports/tracker.py` (`Tracker`, `RunRecord`), `src/serac/adapters/tracking/local.py` (JSON on disk; nothing is sent anywhere) | `present` |
| Validation suites | `src/serac/validation/` (`result`, `events`, `aoi`, `ingest`, `cube`, `stream`, `cap`, `contracts`, `lfh`, `discriminator`, `runout`, `watch`, `e2e`, `promote`, `underwriting`) | `present` (11 suites) |
| Documentation checks | `tests/unit/test_docs_consistency.py`, `tests/unit/test_architecture_doc.py`, `tests/unit/test_data_sources_doc.py` | `present`; this document's counts, paths, statuses and CLI list are asserted against the tree |

### 3.2 Batch EO lane

| Component | Path | Status |
|---|---|---|
| Ingest port | `src/serac/ports/ingest.py` (`IngestAdapter.search / plan / fetch`, `DryRunPlan`) | `present` |
| Base adapter | `src/serac/adapters/eo/_base.py` (streaming download + sha256, `data/raw/<source>/<aoi>/<product>/` layout, > 5 GB confirmation gate, credentials-missing → `not_fetched` + raise) | `present` |
| Sentinel-1 search | `src/serac/adapters/eo/asf_sentinel1.py` (`Sentinel1AsfAdapter`, asf_search geo_search, IW SLC/GRD; `_asf.py` shared Protocols) | `unproven`; download path exercised with fakes only (no Earthdata Login) |
| Sentinel-1 burst listing | `src/serac/adapters/eo/asf_bursts.py` (`SLC-BURST` granule search; the vocabulary M3 plans its network from) | `present`; its listing cache under `data/interim/watch/bursts/` carries no ledger row (gap 71) |
| HyP3 InSAR | `src/serac/adapters/eo/hyp3_insar.py` (`Hyp3InsarAdapter`, `InSARPairPlanner`) and `hyp3_burst.py` (`INSAR_ISCE_MULTI_BURST`, stream-crop-delete retention) | `present`; run against the live HyP3 service — 517 jobs submitted, 3,619 fetched rows of which 3,102 are retained AOI crops and 517 transient (gap 9) |
| Sentinel-2 L2A (production) | `src/serac/adapters/eo/cdse_sentinel2.py` (`CdseSentinel2Adapter`, CDSE STAC + OAuth) | `unproven`; search is public, the fetch path is exercised with fakes only (gap 4) |
| Sentinel-2 L2A (fixtures) | `src/serac/adapters/eo/earthsearch_sentinel2.py` (`EarthSearchSentinel2Adapter`, public COGs) + shared `s2_cloud.py` | `present`; 72 real rows in the ledger |
| NISAR | `src/serac/adapters/eo/nisar.py` + `nisar_constraints.py` (BETA/PROVISIONAL by `collectionName`, instrument gap, `MixedProductLevelError`) | `unproven`; data `listed`, never `fetched` (gap 1) |
| DEM | `src/serac/adapters/eo/dem_glo30.py` (`Glo30DemAdapter`, windowed reads of public COGs; `ports/dem.py` `DemProvider` hook for licensed DEMs) | `present` |
| ERA5 | `src/serac/adapters/eo/era5_cds.py` (`Era5Adapter`, cdsapi) | `unproven`; needs a CDS key, which was never available (gap 3) |
| GACOS | `src/serac/adapters/eo/gacos.py` (`GacosAdapter` request/poll/receive; `serac ingest gacos --receive`) | `unproven`; form endpoint unverified, manual submission by default (gap 3) |
| Zarr store | `src/serac/adapters/storage/zarr_store.py` (Zarr v3, `ZARR_FORMAT` constant, 1×512×512 chunks, zstd; roundtrip test) | `present` |
| GeoParquet index | `src/serac/pipelines/events_index.py` (writes `data/events/events.parquet`) | `present` |
| STAC catalog | `src/serac/adapters/storage/stac_catalog.py` (pystac 1.1.0 Collection per AOI, Item per slice; schemas vendored under `tests/fixtures/stac_schemas/` for offline validation) | `present` |
| Cube builder | `src/serac/pipelines/build_cube.py` + `pipelines/grid.py` + `src/serac/pipelines/layers/` (`LayerBuilder`, `build_empty()` for missing layers), `src/serac/cli_cube.py` (`serac cube build / describe`) | `present`; the S1 layers still prefer the synthetic placeholder over the real burst products (gap 11) |
| AOI + coverage pipelines | `src/serac/pipelines/aoi_build.py`, `aoi_specs.py`, `coverage.py`, `_geojson_io.py`, `sources.py`, `event_entry.py` | `present` |
| DVC pipeline | `dvc.yaml`, `.dvc/config` (no URL), `.dvcignore`, `make dvc-remote` | `present`; no `dvc.lock` (needs a network run of the DEM stage) |

Cube layers per AOI on a fixed 30 m grid (UTM 45N for Lhende): static `dem`, `slope`,
`aspect`; temporal `s1_coherence_t`, `s1_los_velocity_t`, `s2_ndsi_t`, `s2_cloud_t`,
`nisar_hh_t` (placeholder until data), `era5_t2m_t`; each with a `<layer>_valid(time)` flag and
per-layer provenance attrs (`source`, `product_ids`, `manifest_entry_ids`, `retrieved_at`,
`provenance ∈ {real, derived, synthetic, none}`, `status`, `licence`, `units`, `processing`,
`native_resolution_m`); global attr `contains_synthetic`.

#### Feature cube

`serac cube build --aoi ID --from --to [--raw-root data/fixtures] [--out] [--bbox] [--epsg]
[--dry-run]` reads `data/aoi/<id>/{aoi.json,grid.json}` when present (CLI overrides win) and
builds `data/features/<aoi>/cube.zarr` (Zarr v3), `data/features/<aoi>/stac/` (STAC 1.1.0,
one Item per time slice, validated offline) and `reports/cube/<aoi>.json` (`CubeBuildReport`).
Only ledger rows with `status: fetched` under `--raw-root`, or `status: synthetic` under
`tests/fixtures/synthetic/`, contribute pixels; every other layer is `build_empty()`: all-NaN,
`status: not_fetched`. The cube time axis is the union of the imaging layers' acquisition
instants; ERA5 is sampled at those steps. The Chamoli acceptance build from `data/fixtures`
gives real `dem/slope/aspect` (GLO-30 crop), real `s2_ndsi_t/s2_cloud_t` for three dates,
synthetic `s1_coherence_t/s1_los_velocity_t` (flagged, `contains_synthetic: true`) and
`not_fetched` `nisar_hh_t/era5_t2m_t`; `serac cube describe` prints the layer table.
`validate-cube` (`src/serac/validation/cube.py`) and `validate-ingest`
(`src/serac/validation/ingest.py`) are the suites behind the Makefile targets.

### 3.3 Real-time seismic lane

| Component | Path | Status |
|---|---|---|
| Bus port | `src/serac/ports/bus.py` (`MessageBus`: `publish`, `ensure_group`, `read`, `ack`, `pending`, `close`; `Envelope`) | `present` |
| In-memory bus | `src/serac/adapters/bus/in_memory.py` (`InMemoryBus`, deterministic `Pipeline.drain`) | `present` |
| Redis Streams bus | `src/serac/adapters/bus/redis_streams.py` (`RedisStreamsBus`: XADD/XREADGROUP/XACK/XPENDING) | `unproven`; unit-tested with fakeredis, never against a live server (gap 59) |
| Clock port | `src/serac/ports/clock.py` (`WallClock`, `VirtualClock`) | `present` |
| Trace contracts | `src/serac/domain/seismic.py` (`Sncl`, `TraceProvenance`, `SeismicTrace`), `domain/cap.py`, `domain/replay.py` | `present` |
| ObsPy codec | `src/serac/adapters/seismic/obspy_codec.py` (the only module importing obspy for MiniSEED encode/decode) | `present` |
| FDSN archive | `src/serac/adapters/seismic/fdsn.py` (`FdsnWaveformArchive`; default EarthScope + GEOFON, ADR-0015; radius station search; `plan()` dry-run) | `present`; 364 real waveform rows across five data centres |
| SeedLink feed | `src/serac/adapters/seismic/seedlink.py` (`SeedLinkFeed` over `EasySeedLinkClient`) | `unproven`; `geofon.gfz.de:18000` is configuration, no live stream has reached this code (gap 58) |
| USGS ComCat | `src/serac/adapters/seismic/usgs_comcat.py` (`ComCatCatalog`, `eventtype=landslide`) | `present`; the catalogue is sparse and contributed nothing to M1 (gap 6) |
| ESEC catalogue | `src/serac/adapters/seismic/esec.py` (`EsecSpudCatalog`, the M1 positive set) | `present` |
| Green's-function library | `src/serac/ports/greens.py` (`GreensLibrary`, `EarthModel`, `GreensRequest.cache_key`), `src/serac/adapters/seismic/syngine.py` (IRIS Syngine; modelled physics, ledgered `provenance: derived`) | `present`; the endpoint proved intermittent and a local library is a deployment prerequisite (gap 29, `infra/jobs/m2-greens-library.yaml`) |
| Hydrometric | `HydrometricSource` in `src/serac/ports/seismic.py`, `src/serac/adapters/hydro/icimod_fixture.py` (fixture-only; no live feed) | `present`; no open real-time Nepal/China feed exists (gap 2) |
| SeedLink ingestor | `src/serac/streaming/seedlink_ingestor.py` → topic `serac.waveforms` | `present` |
| Detector port + stages | `src/serac/ports/detector.py` (`Detector`: `info`/`ingest`/`poll`/`reset`), `src/serac/streaming/detector_stage.py`, `stage.py`, `pipeline.py`, `replay_source.py`, `synthetic.py`, `golden.py` | `present` |
| Detector stub | `src/serac/streaming/detector_stub.py` → topic `serac.detections` | `present`; still the default, and it fires on pre-event background noise (gaps 56, 57) |
| M1 discriminator | `src/serac/models/discriminator/streaming.py` (`DiscriminatorDetector` behind the same port) | `present`; selectable as `serac replay --detector discriminator`, not mounted by `serac stream run` (gap 56) |
| LFH inversion | `src/serac/models/lfh/` (`pipeline.invert_event`, `gsf.py` grid search, `inversion.py`, `bootstrap.py`, `mass.py`, `trajectory.py`, `waveforms.py`, `cache.py`, `config.py`, `references.py`, `report.py`) → `ForceHistory` | `present`; refuses every recent event serac cares about, and the refusals are results (gap 24) |
| Cascade surrogate | `src/serac/models/runout/` (`solver.py` `serac-swe-voellmy`, `ensemble.py`, `surrogate.py` corridor FNO with 5/50/95 quantile heads, `forecast.py`, `corridor.py`, `terrain.py`, `training.py`, `study.py`) → `CascadeForecast` | `present`; **NOT r.avaflow**, no independent simulator has cross-validated it (gap 39) |
| CAP renderer + stub | `src/serac/adapters/cap/cap12.py`, `src/serac/streaming/cap_stub.py` → topic `serac.alerts` (CAP 1.2, `status: Test`) | `present` |
| CAP alerting (M5) | `src/serac/alerting/` (`generator.py`, `signing.py` Ed25519 enveloped XML-Signature, `keys.py`, `example.py`), `src/serac/ports/alert_sink.py`, `src/serac/adapters/alerting/` (`file_sink.py`, `http_sink.py`) | `present`; the signature proves bytes, not identity, and nothing sends anywhere by default (gap 53) |
| Replay | `src/serac/pipelines/replay.py`, `serac replay --event <id> --speed 1.0|max`, `reports/replay/<event>.json` | `present` |
| End-to-end chain | `src/serac/pipelines/e2e.py` (`serac cascade e2e`: waveform → detection → LFH → surrogate → CAP → avoided loss, stopping at the first stage that cannot feed its successor) | `present`; both committed replays stop at detection (gap 52) |

### 3.4 Model components

Each component has a model card in `reports/`, a validation suite, and its own maturity row in
`RELEASE_STATUS.md`. **None is validated against events and none has been promoted.**

| Component | Path | Gate | Status |
|---|---|---|---|
| M1 seismic discriminator | `src/serac/models/discriminator/` (`catalog.py`, `dataset.py`, `windows.py`, `features.py`, `baseline.py`, `deep.py`, `evaluate.py`, `regions.py`, `latency.py`, `model_card.py`), `src/serac/pipelines/discriminator_build.py`, `src/serac/cli_data.py`, `src/serac/cli_models.py` | `validate-discriminator` (`src/serac/validation/discriminator.py`) | `present`; the suite reports an unmet criterion of the brief — Langtang is not detected — and M1 therefore stays unvalidated (gap 14) |
| M2 force-history inversion | `src/serac/models/lfh/`, `src/serac/cli_lfh.py` | `validate-lfh` (`src/serac/validation/lfh.py`) | `present`; three published reproductions pass by interval overlap, and every recent event is refused (gaps 24, 26, 27) |
| M3 slope watch | `src/serac/models/watch/` (`slope_units.py`, `track_select.py`, `insar_jobs.py`, `mintpy_prep.py`, `mintpy_run.py`, `optical.py`, `anomaly.py`, `aggregate.py`, `backtest.py`, `network.py`, `plan.py`, `raster.py`, `crop.py`, `geometry.py`, `glaciers.py`, `optical_io.py`, `writeup.py`), `src/serac/cli_watch.py` | `validate-watch` (`src/serac/validation/watch.py`) | `present`; a ranking layer, never a failure date, and the measurability thresholds are not pre-registered (gap 31) |
| M4 runout | `src/serac/models/runout/` (solver, 230-member ensemble, surrogate, `langtang.py`, `release.py`, `params.py`, `driver.py`, `runner.py`, `summary.py`, `cascade.py`), `src/serac/cli_runout.py` | `validate-runout` (`src/serac/validation/runout.py`) | `present`; one of five surrogate gates fails on arrival coverage 0.794 (gap 42) |
| M5 cascade / avoided loss | `src/serac/cascade/` (`compute.py`, `damage.py`, `exposure.py`, `evidence.py`, `prior.py`, `table.py`, `underwriting.py`), `src/serac/cli_cascade.py`, `src/serac/cli_alerting.py`, `src/serac/cli_underwriting.py` | `validate-e2e` (`src/serac/validation/e2e.py`), `make underwriting-check` | `present`; no damage-function parameter has a cited source, every one is declared `provenance=assumption`, and all 14 Lhende assets are `undetermined` (gaps 49, 50, 51) |

## 4. The two lanes

### 4.1 Batch EO lane (L0 inventory, L1 watch)

```
provider STAC / search  -->  data/raw/<source>/<aoi>/<product>/   (bytes + manifest entry)
                       -->  data/interim/<aoi>/                    (reprojected, windowed, masked)
                       -->  data/features/<aoi>/cube.zarr          (Zarr v3 feature cube, 30 m grid)
                       -->  data/features/<aoi>/stac/              (pystac Collection + Items)
                       -->  slope-unit watch states (M3)           (SlopeWatchState, ordinal tier)
```

- Every adapter supports `--dry-run` (prints what it would fetch and estimated bytes, writes
  nothing, not even a ledger line) and records a `ManifestEntry` on any real action.
- The cube is rebuilt from `data/raw` deterministically; missing layers are all-NaN with
  `status: not_fetched`, never a fake value.
- Rasters are versioned by DVC (`data/raw`, `data/interim`, `data/features`); the ledger is
  committed to git.
- L0/L1 outputs are inventories and probabilistic watch states. They never emit a
  time-of-failure, and `validate-watch` fails the build if a field that could carry one appears.

### 4.2 Real-time seismic lane (L2 detect, L3 cascade)

```
SeedLink server  -->  seedlink_ingestor  -->  bus topic serac.waveforms   (SeismicTrace, MiniSEED bytes)
                                          -->  detector stage             (DetectorStub, or DiscriminatorDetector)
                                          -->  bus topic serac.detections  (DetectionCandidate, no location)
                                          -->  LFH inversion (models/lfh)  -> ForceHistory | refusal
                                          -->  cascade surrogate (models/runout) -> CascadeForecast
                                          -->  CAP generator (serac.alerting) --> bus topic serac.alerts (CAP 1.2)
                                          -->  avoided loss (serac.cascade)  -> AvoidedLossResponse
```

- Every message is an `Envelope` (message id, topic, schema name + version, producer,
  `produced_at_utc`, `stream_time_utc`, causation id, replay run id, payload). Payloads are
  validated by schema name; major-version mismatches are rejected (ADR-0007).
- The chain **stops at the first stage that cannot give its successor an honest input**, and
  the stop is the recorded outcome. On both committed replays it stops at detection: the
  fixtures carry two receivers against the discriminator's three-station minimum, and M2 would
  refuse the window even with the full receiver set (gap 52). No CAP message has been produced
  from a real event.
- The lane a deployment would run, `serac stream run`, still mounts `DetectorStub`; its CAP
  output is `status: Test`, `scope: Private`, with no `area` element because the stub has no
  location. Nothing in that path claims a detection capability (gap 56).

### 4.3 Latency — real-time lane

End-to-end target from the brief: **≤ 180 s from detachment to first CAP message**, of which
**60 s** is the detection budget. The first of these is unmeasured and the second has been
measured and is **not met**.

**Detection, measured.** `src/serac/models/discriminator/latency.py` replays the Chamoli 2021
window through the trained detector in two modes and records
`reports/m1/latency_chamoli-2021.json`:

| Mode | First detection after origin | Theoretical floor | Meets the 60 s budget |
|---|---|---|---|
| `sliding_180s` | **210 s** | 153 s | no |
| `batch_600s` | **540 s** | 573 s | no |

The floors are travel time to a ≥ 100 km receiver plus the record length a 20–100 s band needs,
minus the 60 s pre-origin lead-in. **No amount of compute moves them** (gap 20): reaching 60 s
would need receivers inside 100 km and a shorter-period discriminant, which is a different
component with different physics. The measurement is computation only — chunks are fed at
maximum speed, so no acquisition, telemetry or transport delay is included, and the real figure
can only be larger.

**End to end, not measured.** No CAP message has been produced on either replay, so no
detachment-to-alert latency exists. `reports/e2e/latency.json` assembles a **counterfactual**
from per-stage measurements — 217.1 s for Chamoli, 187.6 s for Langtang, both using M1's
*theoretical floor* rather than its measured latency because the detector did not fire on those
fixtures, and both assuming 30 s of dissemination. Those are not delivered lead times and must
not be quoted as such (gap 52).

The allocation the brief's 180 s implies is below. Only the detection row has been measured; it
alone exceeds the entire budget.

| Stage | Budget (s) | Basis |
|---|---|---|
| Seismic travel to nearest usable station + SeedLink transport to serac | 20 | design allocation; depends on station geometry (a verified open broadband station, `NK.KKN`, lies ~55 km from the Lhende source zone) and on the SeedLink server's buffering |
| Chunking + bus publish/consume | 5 | design allocation |
| Detection window | 60 | **refuted**: 210 s measured, 153 s floor (`reports/m1/latency_chamoli-2021.json`) |
| LFH single-force inversion | 40 | design allocation; measured wall clock on refusals is 4.2 s (Langtang) and 33.8 s (Chamoli), and a run that produced a mass would take longer (gap 29) |
| Cascade surrogate → arrival hydrographs at transects | 30 | design allocation; surrogate inference p95 is 0.002 s, and the ensemble behind it is not run in the lane |
| CAP assembly, validation, publish | 5 | design allocation |
| Headroom | 20 | reserved |
| **Total** | **180** | the target; **the detection stage alone exceeds it in `batch_600s` and consumes most of it in `sliding_180s`** |

Observed transect arrival times for the 26 Aug 2026 event are recorded in the event library
with their sources (see `docs/EVENT_LIBRARY.md`); they are not restated here.

## 5. Observation cadence — batch lane

The constellation is bought, not built. serac tasks nothing; it consumes what providers
publish.

| Source | Revisit / cadence | Notes |
|---|---|---|
| Sentinel-1 (via ASF; HyP3 InSAR) | 6–12 d | from the brief; 517 burst-InSAR jobs have been run through the live HyP3 service |
| Sentinel-2 L2A (via CDSE; Earth Search for fixtures) | 2–5 d, cloud-permitting | from the brief |
| NISAR L-band (via asf_search) | 12 d | from the brief; calibrated PROVISIONAL products only for acquisitions from 17 Jun 2026 (released 20 Jul 2026); BETA products Oct 2025–Jan 2026 are not inter-comparable; instrument gap 27 Jul–10 Aug 2026 |
| Commercial X-band SAR | sub-daily **if procured** | not procured; no adapter |
| Copernicus GLO-30 DEM | static | credential-free public COGs |
| ERA5 | reanalysis; latency to be recorded at fetch time | needs a CDS API key |
| GACOS | on request (email workflow) | request/poll pattern |

Provider latencies (time from acquisition to availability) are not stated here because
they were not measured in recon; `docs/DATA_SOURCES.md` records them as "to be recorded at
fetch time".

## 6. Provenance data-flow and the replay path

```
adapter.fetch()  --> bytes under data/raw/...  --> ManifestEntry(status=fetched, sha256, size, retrieved_at, licence, url)
adapter.plan()   --> nothing on disk           --> (dry-run: no ledger line)
credentials missing / > 5 GB declined          --> ManifestEntry(status=not_fetched) then raise
async job (HyP3, GACOS)                        --> ManifestEntry(status=requested) ... later status=fetched
stream-crop-delete (burst InSAR)               --> ManifestEntry(status=fetched, retention=transient)
modelled output (Green's functions, solver)    --> ManifestEntry(provenance=derived)
fixture under data/fixtures/                   --> ManifestEntry(status=fetched, provenance=real)
double under tests/fixtures/synthetic/         --> ManifestEntry(status=synthetic, provenance=synthetic, notes)
```

`derived` is not `synthetic` (ADR-0016): a reprojection, a feature cube, a simulation output or
physics evaluated from a published Earth model is reproducible from stated inputs, whereas a
`synthetic` row stands in for something serac could not obtain and may live only under
`tests/fixtures/synthetic/`.

`make validate-ingest` re-hashes every retained `fetched` entry and refuses NISAR
BETA/PROVISIONAL mixing; transient rows can never be re-hashed and are reported as a warning
(gap 9). `serac events report` joins the ledger to the event library to show which sources
exist for which event and time window.

Replay (`serac replay --event chamoli-2021 --speed max`): reads the event's origin time from
the event record (never hard-coded), slices archived MiniSEED fixtures from
`data/fixtures/seismic/<event>/` into stream-time-ordered chunks, publishes them on the
chosen bus (`in_memory` by default, `redis` with Compose), drains detector and CAP stages,
and writes `reports/replay/<event>.json` (`ReplayReport`: chunks published/consumed, pending
after drain, first detection, first CAP, stream-time and wall-clock latencies, detector
params, `is_stub` for the default detector, mandatory caveats). Whether the stub fires on real
Chamoli data is not asserted and the threshold is not tuned to make it fire; the
detection→CAP path is proven on a labelled synthetic burst that is never written under
`data/`.

## 7. Hexagonal rule and package layout (ADR-0009)

- `src/serac/domain/`: pydantic models and pure logic. Imports nothing from
  `src/serac/adapters/`, and no numpy/obspy/geo libraries.
- `src/serac/ports/`: abstract base classes the domain, models and pipelines talk to.
- `src/serac/adapters/<kind>/`: one concrete implementation per external system. Only
  adapters import provider SDKs.
- `src/serac/models/<component>/` and `src/serac/cascade/`: the model components. They depend
  on ports and domain contracts, never on a concrete adapter.
- `src/serac/alerting/`: the forecast lane's CAP path, behind `src/serac/ports/alert_sink.py`.
- `src/serac/pipelines/` and `src/serac/streaming/`: orchestration over ports.
- `src/serac/cli.py`: assembles typer sub-apps registered by their owning modules.

## 8. Compute-target portability (ADR-0014)

- Local: `uv run serac …`; `infra/docker/compose.yaml` provides Redis, plus a commented-out
  GRASS service block that no code talks to — r.avaflow could not be obtained
  (`infra/docker/ravaflow/README.md`) and M4 uses `serac-swe-voellmy` instead. Compose has
  never been brought up (gap 60).
- Deployment: `infra/docker/Dockerfile`, built from the repository root. It has been built on
  `linux/arm64` and its `serac` entrypoint exercised; it has never been pushed anywhere and no
  GPU variant has ever been built, so there is no image to pull (gap 68).
- Scaled: `infra/jobs/*.yaml` describe seven container jobs generically (image, command, env,
  cpu, memory, gpu, storage, estimated core-hours with the estimate basis stated,
  inputs/outputs by DVC path) with `aws:` annotation blocks. Every command is resolved against
  the installed CLI by `tests/unit/test_job_manifests.py`, and **none has ever been executed**
  (gaps 61, 69). Nothing in the manifests or the code depends on a managed platform.
