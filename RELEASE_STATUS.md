# Release status

Honest maturity ledger. Updated: 2026-09-03 (architect branch, before any of the parallel
Phase 1 branches merged). The orchestrator flips cells as phases land; a cell is `yes` only
when the corresponding command has actually been run and passed on `main`.

Columns: **designed** (an ADR/architecture entry exists), **implemented** (code in the
tree), **tested-offline** (covered by `make test`), **tested-online** (exercised by
`make smoke-online` against the real service), **validated-against-events** (checked
against event-library records by a `validate-*` suite or a replay), **production** (deployed
and operated; nothing is).

| Component | designed | implemented | tested-offline | tested-online | validated-against-events | production |
|---|---|---|---|---|---|---|
| Repo skeleton (uv, ruff, mypy strict, pytest-socket, CI) | yes | yes | yes | n/a | n/a | no |
| Provenance ledger (`ManifestEntry`, `JsonlManifestLedger`) | yes | yes | yes | n/a | no | no |
| Settings / `.env` (`SeracSettings`) | yes | yes | yes (import) | no | n/a | no |
| CLI skeleton (`serac --help`, `--version`) | yes | yes | yes | n/a | n/a | no |
| Domain contracts (`Range`, `SourceRef`, `FieldNote`, `MassMovementEvent`, geo, forecast, avoided-loss) | yes | yes | yes (validators, both paths) | n/a | no | no |
| `serac schema export` + `contracts/*.v0.json` + drift test | yes | yes (18 contracts) | yes | n/a | n/a | no |
| Event library (11 records / 9 items) + `serac events add/report/build-index` | yes | yes | yes (`validate-events` 64 checks) | sources fetched live 2026-09-03 | n/a | no |
| AOIs (lhende-khola-trishuli, chamoli-rishiganga, blatten-lotschental) | yes | yes | yes (`validate-aoi` 100 checks, 3 warnings) | OSM/agency sources fetched 2026-09-03 | n/a | no |
| Sentinel-1 ASF adapter | yes | yes | yes | search only | no | no |
| HyP3 InSAR adapter + pair planner | yes | yes | yes (fakes; synthetic pair fixture) | no (no Earthdata credentials) | no | no |
| Sentinel-2 CDSE adapter | yes | yes | yes (fakes) | search only | no | no |
| Sentinel-2 Earth Search adapter (fixture source) | yes | yes | yes (fake STAC, real crops) | yes (smoke 2026-09-03) | no | no |
| NISAR adapter + level constraints | yes | yes | yes (BETA/PROVISIONAL split on `collectionName`) | search only | no | no |
| Copernicus GLO-30 DEM adapter | yes | yes | yes (real crops) | yes (smoke 2026-09-03) | no | no |
| ERA5 adapter | yes | yes | yes (fakes) | no (no CDS key) | no | no |
| GACOS request/poll adapter | yes | yes | yes (fakes) | no (email workflow) | no | no |
| Feature cube (`build_cube`, Zarr v3, STAC) + `serac cube build/describe` | yes | yes | yes (`validate-cube` 23 checks on the Chamoli fixture cube) | n/a | no | no |
| DVC pipeline (`dvc.yaml`, `make dvc-remote`) | yes | yes | yes (`dvc stage list`, 14 stages) | no (no remote configured) | n/a | no |
| Message bus port + `InMemoryBus` | yes | yes | yes (contract test) | n/a | n/a | no |
| `RedisStreamsBus` | yes | yes | yes (fakeredis) | no (no Redis on dev machine; `redis`-marked test skips) | n/a | no |
| FDSN waveform adapter (EarthScope, GEOFON) | yes | yes | yes (fixtures) | fixtures fetched live 2026-09-03 | no | no |
| SeedLink feed + ingestor | yes | yes | yes (fake client) | no (endpoint unverified) | n/a | no |
| USGS ComCat adapter | yes | yes | yes (57-event fixture) | fetched live 2026-09-03 | no | no |
| Hydrometric port + ICIMOD fixture adapter | yes | yes | yes | n/a | 2 reported stage changes, no clock time | no |
| Detector **stub** | yes | yes (placeholder LP/SP ratio, threshold 10 untuned) | yes (golden on chamoli-2021) | n/a | **no — fires on pre-event background noise in both real fixtures** | no |
| CAP 1.2 renderer + **stub** + XSD validation | yes | yes | yes (offline XSD) | n/a | n/a (Test/Private, no area) | no |
| Replay + latency report | yes | yes | yes (chamoli-2021, langtang-2026, synthetic-lp-burst) | no (redis path untested live) | plumbing only | no |
| Validation harness (`validate-*`, `validate-serac`, `promote`) | yes | yes | yes (6 suites, 256 checks) | n/a | n/a | no |
| `underwriting-check` (exits 2 "not implemented: Prompt 2") | yes | yes | yes | n/a | n/a | no |
| Ingest port + `BaseIngestAdapter` (dry-run, 5 GiB gate, credentials path, ledger) | yes | yes | yes | n/a | n/a | no |
| Seismic contracts (`SeismicTrace`, `Envelope`+codec, `DetectionCandidate`, `ForceHistory`=not_implemented, `CAPMessage`, `ReplayReport`) | yes | yes | yes | n/a | no | no |
| ObsPy MiniSEED codec + `Stage`/`Pipeline`/`Clock` skeleton | yes | yes | yes (round-trips the 4 real fixtures) | n/a | no | no |
| Fixtures: seismic (chamoli-2021, langtang-2026), ComCat, CAP XSDs, DEM crops ×3, S2 crops ×3 dates, S1/NISAR/CDSE listings | fetched 2026-09-03, sha256 in ledger | real | integrity tests | n/a | n/a | n/a |
| Docker Compose (`infra/docker/compose.yaml`) | yes | yes (file) | no | no | n/a | no |
| Job manifests (`infra/jobs/*.yaml`) | yes | yes (files) | n/a | no | n/a | no |
| Governance docs (CLAUDE.md, ARCHITECTURE, ADRs, CREDENTIALS, DATA_SOURCES, EVENT_LIBRARY) | yes | yes | n/a | n/a | n/a | n/a |
| LFH single-force inversion (Prompt 2) | no | no | no | no | no | no |
| Cascade / runout surrogate (Prompt 2) | no | no | no | no | no | no |
| Avoided-loss computation (Prompt 2) | contract only | no | no | no | no | no |

"designed: yes" for the Phase 1 rows means the plan and `docs/ARCHITECTURE.md` describe
them; the parallel branches (domain-modeller, eo-data-engineer, seismic-engineer) are
building them and the orchestrator will flip `implemented` / `tested-offline` on merge.

## Known gaps

Numbered so `TODO` comments can reference them (`TODO(RELEASE_STATUS#n)`).

1. **No calibrated pre-Langtang NISAR series.** Calibrated PROVISIONAL products exist only
   for acquisitions from 17 Jun 2026 (released 20 Jul 2026); BETA products
   Oct 2025–Jan 2026 are not inter-comparable; instrument gap 27 Jul–10 Aug 2026. As of
   2026-09-03 `asf_search` returns only ancillary SCLKSCET files; NISAR is `not_fetched`.
2. **No open real-time Nepal/China hydrometric feed.** Nepal DHM gauges have no stable open
   API. The hydrometric adapter reads a fixture built from ICIMOD public reporting; anything
   else raises `DatasetNotFetchedError`.
3. **The detector is a stub.** A placeholder long-period/short-period energy ratio with an
   untuned threshold, no discriminator, no location. It emits `DetectionCandidate`s that
   carry no location and CAP messages with `status: Test`. Prompt 2 replaces it.
4. **No runout model yet.** `CascadeForecast` and `ForceHistory` are interfaces only;
   `underwriting-check` exits 2 with "not implemented: Prompt 2" by design.
5. **SeedLink endpoint unverified.** `geofon.gfz.de:18000` is configuration, not a verified
   endpoint. `make smoke-online` on 2026-09-03 could not stream from it (the client raised
   `'<' not supported between instances of 'float' and 'NoneType'`) and the test skipped, as
   designed. No live SeedLink stream has ever reached this code.
6. **Redis Streams untested against a live server.** `RedisStreamsBus` is unit-tested with
   fakeredis only; the `redis`-marked test has never run.
7. **Docker Compose untested on the dev machine.** No Docker is installed there;
   `infra/docker/compose.yaml` has not been brought up.
8. **ComCat landslide set sparse and lacks Chamoli.** `eventtype=landslide` returns only 57
   events since 2000, mostly Alaska ml 1–2; Chamoli 2021 is absent. The labelled positive set
   for Prompt 2 is small.
9. **Replay latency figures prove plumbing only.** `reports/replay/<event>.json` shows that
   messages traverse the bus and stages; the ≤ 180 s target in `docs/ARCHITECTURE.md` is a
   design budget, not a measurement. Wall-clock latencies are meaningful only at
   `--speed 1.0`.
10. **S1/HyP3, ERA5, GACOS fixtures synthetic or absent.** No Earthdata, CDS or GACOS
    credentials were available in the founding session. S1/HyP3 cube layers are labelled
    synthetic placeholders under `tests/fixtures/synthetic/`; ERA5 and GACOS are
    `not_fetched`.
11. **CDSE fetch path exercised only with fakes.** CDSE search is public; downloads need
    OAuth credentials that were not available.
12. **No open broadband seismic station within 300 km of Chamoli.** Chamoli replay uses
    `NK.KKN` and `IC.LSA`; any Chamoli-based latency figure inherits that geometry.
13. **No GitHub issues can be opened until `gh auth login`.** `TODO`s therefore reference
    entries in this list.
14. **Langtang 2026 figures are largely press-attributed.** No peer-reviewed source exists
    yet; volume is `null`, and press-derived ranges carry `best: null`.

14. **The detector stub fires on pre-event background noise.** On both real fixtures the
    placeholder long-period/short-period ratio exceeds the placeholder threshold in the first
    evaluable window (Chamoli `NK.KKN..BHZ`, 2021-02-07T04:49:59Z, ratio 233; `IC.LSA.00.BHZ`
    ratio 2056). Its detections are an observation about a placeholder, not evidence of
    anything. The threshold was deliberately not tuned to change this.
15. **Terrain layers in the Chamoli fixture cube are partial.** The committed GLO-30 crop
    covers the source zone, not the whole corridor AOI, so `dem`/`slope`/`aspect` cover 11.7 %
    of the cube grid and are `status: partial`, NaN elsewhere.
16. **`h5py` is absent from the locked environment**, so the NISAR GCOV (HDF5) and ERA5
    NetCDF-4 readers are written to spec but untested against real products.
17. **No `dvc.lock`**: producing one requires running an ingest stage over the network.
18. **The seismic and ComCat fixture ledger rows carry no `event_id`/`aoi_id`**, so those
    columns show `-` in `serac events report`; the ledger is append-only and was not rewritten.
