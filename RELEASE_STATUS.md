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
| Event library (11 records / 9 items) + `serac events add/report/build-index` | yes | no | no | no | no | no |
| AOIs (lhende-khola-trishuli, chamoli-rishiganga, blatten-lotschental) | yes | no | no | no | no | no |
| Sentinel-1 ASF adapter | yes | no | no | no | no | no |
| HyP3 InSAR adapter + pair planner | yes | no | no | no | no | no |
| Sentinel-2 CDSE adapter | yes | no | no | no | no | no |
| Sentinel-2 Earth Search adapter (fixture source) | yes | yes | yes (fake STAC, real crops) | yes (smoke 2026-09-03) | no | no |
| NISAR adapter + level constraints | yes | no | no | no | no | no |
| Copernicus GLO-30 DEM adapter | yes | yes | yes (real crops) | yes (smoke 2026-09-03) | no | no |
| ERA5 adapter | yes | no | no | no | no | no |
| GACOS request/poll adapter | yes | no | no | no | no | no |
| Feature cube (`build_cube`, Zarr v3, STAC) + `serac cube build/describe` | yes | no | no | n/a | no | no |
| DVC pipeline (`dvc.yaml`, `make dvc-remote`) | yes | no | no | no | n/a | no |
| Message bus port + `InMemoryBus` | yes | yes | yes (contract test) | n/a | n/a | no |
| `RedisStreamsBus` | yes | yes | yes (fakeredis) | no (no Redis on dev machine; `redis`-marked test skips) | n/a | no |
| FDSN waveform adapter (EarthScope, GEOFON) | yes | no | no | no | no | no |
| SeedLink feed + ingestor | yes | no | no | no | no | no |
| USGS ComCat adapter | yes | no | no | no | no | no |
| Hydrometric port + ICIMOD fixture adapter | yes | no | no | n/a | no | no |
| Detector **stub** | yes | no | no | n/a | no | no |
| CAP 1.2 renderer + **stub** + XSD validation | yes | no | no | n/a | no | no |
| Replay + latency report | yes | no | no | no | no | no |
| Validation harness (`validate-*`, `validate-serac`, `promote`) | yes | no | no | n/a | no | no |
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
5. **SeedLink endpoint unverified.** `geofon.gfz.de:18000` is configuration; no live
   connection has been made.
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
