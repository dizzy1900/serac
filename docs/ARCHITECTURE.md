# serac architecture

Date of record: 2026-09-03. Status legend used throughout:

| Tag | Meaning |
|---|---|
| `present` | in the tree on `main` today |
| `planned (P1)` | part of Prompt 1 (foundations), being built on parallel branches; not merged yet |
| `planned (P2)` | Prompt 2 (models): LFH inversion, runout surrogate, avoided-loss, GRASS |

Numbers in this document are either quoted from the founding brief, from the recon of
2026-09-03, or labelled as **design targets**. A design target is not a measurement.
`RELEASE_STATUS.md` is the ledger of what has actually been built and tested.

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
 |  USGS ComCat           |        |  event library + AOIs      |        |  integrate ONLY via    |
 |  OSM, ICIMOD, Crossref |        |  provenance ledger         |        |  contracts/*.json      |
 +------------------------+        +----------------------------+        +------------------------+
```

- Providers are read through adapters that record every retrieval (or refusal) in
  `data/manifest.jsonl`. No provider is trusted to be present: a missing dataset is
  `status: not_fetched` and the code fails loudly rather than substituting values.
- Consumers see only the JSON Schemas in `contracts/` (`avoided-loss.v0.json`,
  `cap-message.v0.json`, `cascade-forecast.v0.json`, `replay-report.v0.json`, …; `planned (P1)`
  by the domain-modeller). They never import `serac`.
- serac owns no satellites and no seismic network. **The constellation is bought, not
  built.** Observation cadence is whatever the providers deliver (section 5).

## 2. C4 level 2 — containers

| Container | Role | Runs as | Status |
|---|---|---|---|
| CLI `serac` | single entrypoint (typer) for ingest, events, aoi, cube, stream, replay, validate, promote, schema | `uv run serac …`, or the Docker image | `present` (skeleton: `--help`, `--version`); sub-apps `planned (P1)` |
| Batch EO lane | STAC search → `data/raw` → `data/interim` → Zarr feature cube per AOI → STAC catalog | `make`/DVC stages locally; `infra/jobs/*.yaml` at scale | `planned (P1)` |
| Real-time seismic lane | SeedLink → bus → detector → (inversion → cascade, P2) → CAP | long-running processes reading the bus | `planned (P1)` skeleton with stubs |
| Message bus | Redis Streams behind a synchronous `MessageBus` port; in-memory adapter for tests | `infra/docker/compose.yaml` (`redis:7-alpine`) | `planned (P1)`; live Redis unverified |
| Storage | Zarr v3 cubes, GeoParquet vectors, pystac catalogs under `data/`; DVC remote from env | filesystem + DVC | ledger `present`; stores `planned (P1)` |
| Provenance ledger | `data/manifest.jsonl`, append-only JSON Lines | file | `present` |
| Validation harness | `validate-*` suites writing `reports/validation/<suite>.json`; `promote`; `underwriting-check` | `make` | `planned (P1)` |

Deployment unit: one plain Docker image containing the `serac` package (ADR-0014). Local
development uses Docker Compose (`infra/docker/`); scaled runs are job manifests in
`infra/jobs/` written for a generic container host and annotated for AWS Batch / EC2.

## 3. C4 level 3 — components

### 3.1 Cross-cutting

| Component | Path | Status |
|---|---|---|
| Errors | `src/serac/errors.py` (`SeracError`, `DatasetNotFetchedError`, `CredentialsMissingError`, `NotImplementedYetError`) | `present` |
| Settings (`.env`) | `src/serac/settings.py` (`SeracSettings`, `get_settings()`) | `present` |
| Provenance contract | `src/serac/domain/manifest.py` (`ManifestEntry`, `DataSource`, `ManifestStatus`, `Provenance`) | `present` |
| Ledger port | `src/serac/ports/ledger.py` (`ManifestLedger`: `append`, `entries`, `query`, `latest`) | `present` |
| Ledger adapter | `src/serac/adapters/storage/manifest_ledger.py` (`JsonlManifestLedger`, `sha256_of_file`) | `present` |
| Common contracts | `src/serac/domain/common.py` (`Range`, `SourceRef`, `FieldNote`, `AttributedEstimate`) | `planned (P1)` |
| Event contract | `src/serac/domain/events.py` (`MassMovementEvent`, `EventTime`, `SeismicAttribution`, `Precursor`, …) | `planned (P1)` |
| Geo contracts | `src/serac/domain/geo.py` (`AOI`, `GridSpec`, `SlopeUnit`, `Transect`, `ExposedAsset`) | `planned (P1)` |
| Forecast contracts | `src/serac/domain/forecast.py` (`CascadeForecast`, `ForceHistory` with `status: not_implemented`) | `planned (P1)` interfaces only |
| Avoided-loss contract | `src/serac/domain/avoided_loss.py` → `contracts/avoided-loss.v0.json` | `planned (P1)` schema; populated in P2 |
| Schema export | `src/serac/domain/schema_export.py`, `serac schema export`, drift test in `tests/contract/` | `planned (P1)` |
| Validation suites | `src/serac/validation/{result,events,ingest,cube,stream,cap,contracts,promote,underwriting}.py` | `planned (P1)` |

### 3.2 Batch EO lane

| Component | Path | Status |
|---|---|---|
| Ingest port | `src/serac/ports/ingest.py` (`IngestAdapter.search / plan / fetch`, `DryRunPlan`) | `planned (P1)` |
| Base adapter | `src/serac/adapters/eo/base.py` (streaming download + sha256, `data/raw/<source>/<aoi>/<product>/` layout, > 5 GB confirmation gate, credentials-missing → `not_fetched` + raise) | `planned (P1)` |
| Sentinel-1 search | `src/serac/adapters/eo/asf.py` (`Sentinel1AsfAdapter`, asf_search geo_search, IW SLC/GRD) | `planned (P1)` |
| HyP3 InSAR | `src/serac/adapters/eo/hyp3.py` (`Hyp3InsarAdapter`, `InSARPairPlanner`; jobs ledgered `status: requested`) | `planned (P1)` |
| Sentinel-2 L2A (production) | `src/serac/adapters/eo/cdse.py` (`CdseSentinel2Adapter`, CDSE STAC + OAuth) | `planned (P1)`; fetch path exercised only with fakes |
| Sentinel-2 L2A (fixtures) | `src/serac/adapters/eo/earthsearch.py` (`EarthSearchSentinel2Adapter`, public COGs) + shared `s2_cloud.py` | `planned (P1)` |
| NISAR | `src/serac/adapters/eo/nisar.py` + `nisar_constraints.py` (BETA/PROVISIONAL windows, instrument gap, `MixedProductLevelError`) | `planned (P1)`; data `not_fetched` |
| DEM | `src/serac/adapters/eo/dem.py` (`Glo30DemAdapter`, windowed reads of public COGs; `ports/dem.py` hook for licensed DEMs) | `planned (P1)` |
| ERA5 | `src/serac/adapters/eo/era5.py` (`Era5Adapter`, cdsapi) | `planned (P1)`; needs CDS key |
| GACOS | `src/serac/adapters/eo/gacos.py` (`GacosAdapter` request/poll; `serac ingest gacos --receive`) | `planned (P1)` |
| Zarr store | `src/serac/adapters/storage/zarr_store.py` (Zarr v3, 1×512×512 chunks, zstd) | `planned (P1)` |
| GeoParquet store | `src/serac/adapters/storage/geoparquet_store.py` | `planned (P1)` |
| STAC catalog | `src/serac/adapters/storage/stac_catalog.py` (pystac; vendored schemas for offline validation) | `planned (P1)` |
| Cube builder | `src/serac/pipelines/build_cube.py` (`GridSpec`, `LayerBuilder`, `build_empty()` for missing layers), `serac cube build / describe` | `planned (P1)` |
| DVC pipeline | `dvc.yaml`, `.dvc/config` (no URL), `make dvc-remote` | `planned (P1)` |

Cube layers per AOI on a fixed 30 m grid (UTM 45N for Lhende): static `dem`, `slope`,
`aspect`; temporal `s1_coherence_t`, `s1_los_velocity_t`, `s2_ndsi_t`, `s2_cloud_t`,
`nisar_hh_t` (placeholder until data), `era5_t2m_t`; each with a `<layer>_valid(time)` flag and
per-layer provenance attrs (`source`, `product_ids`, `manifest_entry_ids`, `retrieved_at`,
`provenance ∈ {real, synthetic, none}`, `status`, `licence`, `units`, `processing`,
`native_resolution_m`); global attr `contains_synthetic`.

### 3.3 Real-time seismic lane

| Component | Path | Status |
|---|---|---|
| Bus port | `src/serac/ports/bus.py` (`MessageBus`: `publish`, `ensure_group`, `read`, `ack`, `pending`, `close`; `Envelope`) | `planned (P1)` |
| In-memory bus | `src/serac/adapters/bus/in_memory.py` (`InMemoryBus`, deterministic `Pipeline.drain`) | `planned (P1)` |
| Redis Streams bus | `src/serac/adapters/bus/redis_streams.py` (`RedisStreamsBus`: XADD/XREADGROUP/XACK/XPENDING) | `planned (P1)`; unit-tested with fakeredis only |
| Clock port | `src/serac/ports/clock.py` (`WallClock`, `VirtualClock`) | `planned (P1)` |
| Trace contracts | `src/serac/domain/seismic.py` (`Sncl`, `TraceProvenance`, `SeismicTrace`), `domain/envelope.py`, `domain/detection.py`, `domain/cap.py`, `domain/replay.py` | `planned (P1)` |
| ObsPy codec | `src/serac/adapters/seismic/obspy_codec.py` (the only module importing obspy for MiniSEED encode/decode) | `planned (P1)` |
| FDSN archive | `src/serac/adapters/seismic/fdsn.py` (`FdsnWaveformArchive`; default EarthScope + GEOFON; radius station search; `plan()` dry-run) | `planned (P1)` |
| SeedLink feed | `src/serac/adapters/seismic/seedlink.py` (`SeedLinkFeed` over `EasySeedLinkClient`) | `planned (P1)`; endpoint unverified |
| USGS ComCat | `src/serac/adapters/seismic/usgs_comcat.py` (`ComCatCatalog`, `eventtype=landslide`) | `planned (P1)` |
| Hydrometric | `src/serac/ports/hydro.py`, `src/serac/adapters/hydro/icimod_reported.py` (fixture-only; no live feed) | `planned (P1)` |
| SeedLink ingestor | `src/serac/streaming/seedlink_ingestor.py` → topic `serac.waveforms` | `planned (P1)` |
| Detector stub | `src/serac/streaming/detector_stub.py` → topic `serac.detections` ("STUB — replaced in Prompt 2") | `planned (P1)` |
| LFH inversion | single-force inversion; module path decided in Prompt 2 (`seisbench` reserved for it, ADR-0005) | `planned (P2)` |
| Cascade surrogate | neural runout surrogate → arrival hydrographs at transects; module path decided in Prompt 2 | `planned (P2)` |
| CAP renderer + stub | `src/serac/adapters/cap/render.py`, `src/serac/streaming/cap_stub.py` → topic `serac.alerts` (CAP 1.2, `status: Test`) | `planned (P1)` |
| Replay | `src/serac/pipelines/replay.py`, `serac replay --event <id> --speed 1.0|max`, `reports/replay/<event>.json` | `planned (P1)` |

## 4. The two lanes

### 4.1 Batch EO lane (L0 inventory, L1 watch)

```
provider STAC / search  -->  data/raw/<source>/<aoi>/<product>/   (bytes + manifest entry)
                       -->  data/interim/<aoi>/                    (reprojected, windowed, masked)
                       -->  data/features/<aoi>/cube.zarr          (Zarr v3 feature cube, 30 m grid)
                       -->  data/features/<aoi>/stac/              (pystac Collection + Items)
```

- Every adapter supports `--dry-run` (prints what it would fetch and estimated bytes, writes
  nothing, not even a ledger line) and records a `ManifestEntry` on any real action.
- The cube is rebuilt from `data/raw` deterministically; missing layers are all-NaN with
  `status: not_fetched`, never a fake value.
- Rasters are versioned by DVC (`data/raw`, `data/interim`, `data/features`); the ledger is
  committed to git.
- L0/L1 outputs are inventories and probabilistic watch states. They never emit a
  time-of-failure.

### 4.2 Real-time seismic lane (L2 detect, L3 cascade)

```
SeedLink server  -->  seedlink_ingestor  -->  bus topic serac.waveforms   (SeismicTrace, MiniSEED bytes)
                                          -->  detector_stub               (P1: energy-ratio placeholder)
                                          -->  bus topic serac.detections  (DetectionCandidate, no location)
                                          -->  LFH inversion               (P2)  -> ForceHistory
                                          -->  cascade surrogate           (P2)  -> CascadeForecast
                                          -->  cap_stub / CAP renderer     -->  bus topic serac.alerts (CAP 1.2)
```

- Every message is an `Envelope` (message id, topic, schema name + version, producer,
  `produced_at_utc`, `stream_time_utc`, causation id, replay run id, payload). Payloads are
  validated by schema name; major-version mismatches are rejected (ADR-0007).
- In Prompt 1 the detector is a **stub** and the CAP output is `status: Test`,
  `scope: Private`, with no `area` element because the stub has no location. Nothing in the
  lane claims a detection capability.

### 4.3 Latency budget — real-time lane (design target, not proven)

End-to-end target from the brief: **≤ 180 s from detachment to first CAP message.** The
allocation below is a design budget. Prompt 1 does not measure it; replay reports prove that
messages traverse the plumbing, and wall-clock latencies in them are only meaningful at
`--speed 1.0`. Prompt 2 owns the measurement.

| Stage | Budget (s) | Basis |
|---|---|---|
| Seismic travel to nearest usable station + SeedLink transport to serac | 20 | design allocation; depends on station geometry (nearest verified open broadband station to the Lhende source zone, `NK.KKN`, is ~55 km away) and on the SeedLink server's buffering |
| Chunking + bus publish/consume | 5 | design allocation |
| Detection window (long-period energy accumulation before a candidate can be emitted) | 60 | design allocation; the P2 discriminator sets the real window |
| LFH single-force inversion (P2) | 40 | design allocation |
| Cascade surrogate → arrival hydrographs at transects (P2) | 30 | design allocation |
| CAP assembly, validation, publish | 5 | design allocation |
| Headroom | 20 | reserved |
| **Total** | **180** | equals the target with 20 s headroom |

Observed transect arrival times for the 26 Aug 2026 event are recorded in the event library
with their sources (see `docs/EVENT_LIBRARY.md`); they are not restated here.

## 5. Observation cadence — batch lane

The constellation is bought, not built. serac tasks nothing; it consumes what providers
publish.

| Source | Revisit / cadence | Notes |
|---|---|---|
| Sentinel-1 (via ASF; HyP3 InSAR) | 6–12 d | from the brief |
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
fixture under data/fixtures/                   --> ManifestEntry(status=fetched, provenance=real)
double under tests/fixtures/synthetic/         --> ManifestEntry(status=synthetic, provenance=synthetic, notes)
```

`make validate-ingest` re-hashes every `fetched` entry and refuses NISAR BETA/PROVISIONAL
mixing. `serac events report` joins the ledger to the event library to show which sources
exist for which event and time window.

Replay (`serac replay --event chamoli-2021 --speed max`): reads the event's origin time from
the event record (never hard-coded), slices archived MiniSEED fixtures from
`data/fixtures/seismic/<event>/` into stream-time-ordered chunks, publishes them on the
chosen bus (`in_memory` by default, `redis` with Compose), drains detector and CAP stages,
and writes `reports/replay/<event>.json` (`ReplayReport`: chunks published/consumed, pending
after drain, first detection, first CAP, stream-time and wall-clock latencies, detector
params, `is_stub: true`, mandatory caveats). Whether the stub fires on real Chamoli data is
not asserted and the threshold is not tuned to make it fire; the detection→CAP path is
proven on a labelled synthetic burst that is never written under `data/`.

## 7. Hexagonal rule and package layout (ADR-0009)

- `src/serac/domain/`: pydantic models and pure logic. Imports nothing from
  `src/serac/adapters/`, and no numpy/obspy/geo libraries.
- `src/serac/ports/`: abstract base classes the domain and pipelines talk to.
- `src/serac/adapters/<kind>/`: one concrete implementation per external system. Only
  adapters import provider SDKs.
- `src/serac/pipelines/` and `src/serac/streaming/`: orchestration over ports.
- `src/serac/cli.py`: assembles typer sub-apps registered by their owning modules.

## 8. Compute-target portability (ADR-0014)

- Local: `uv run serac …`; `infra/docker/compose.yaml` provides Redis (and a commented GRASS
  placeholder for Prompt 2). Compose is untested on the dev machine (no Docker).
- Scaled: `infra/jobs/*.yaml` describe container jobs generically (image, command, env, cpu,
  memory, gpu, storage, estimated core-hours with the estimate basis stated, inputs/outputs
  by DVC path) with `aws:` annotation blocks (Batch job-definition sketch, instance family
  suggestion). Nothing in the manifests or the code depends on a managed platform.
