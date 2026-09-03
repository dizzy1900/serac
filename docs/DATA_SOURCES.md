# Data sources

One section per source serac reads. Facts here come from the founding brief or from the
recon of 2026-09-03; anything not verified is written as "to be recorded at fetch time".
Licence text is copied into `data/manifest.jsonl` (`licence`, `licence_source_url`) by the
adapter at the moment bytes are retrieved; this document does not restate licences it
could not verify. Adapter module paths are those in the plan and are marked `planned`
where not yet in the tree (see `docs/ARCHITECTURE.md` for the status legend).

Cross-cutting gaps (repeated in `RELEASE_STATUS.md`):

- No calibrated pre-Langtang NISAR series exists (constraints below).
- The USGS ComCat `eventtype=landslide` set is sparse and does not contain Chamoli 2021.
- No open broadband seismic station lies within 300 km of Chamoli.
- No open real-time hydrometric feed exists for the Nepal/China corridor.

## Sentinel-1 via ASF (search) and Earthdata (download)

| | |
|---|---|
| URL | ASF search via `asf_search` (public); downloads via Earthdata-authenticated ASF endpoints |
| Licence | to be recorded at fetch time |
| Cadence | 6–12 d (brief) |
| Latency | to be recorded at fetch time |
| Credentials | search: none; download: `EARTHDATA_USERNAME/PASSWORD` |
| Adapter | `src/serac/adapters/eo/asf.py` (`Sentinel1AsfAdapter`; planned) |
| Ledger source | `sentinel1_asf` |
| Fixture | a real recorded ASF listing for Chamoli Jan–Feb 2021 (planned, `data/fixtures/`) |
| Known gaps | no credentials in the founding session, so no SLC/GRD bytes were fetched; S1 cube layers are synthetic placeholders under `tests/fixtures/synthetic/` until someone fetches |

## HyP3 InSAR (ASF on-demand Sentinel-1 InSAR)

| | |
|---|---|
| URL | ASF HyP3 via `hyp3-sdk` 7.7 |
| Licence | to be recorded at fetch time (product-level) |
| Cadence | derived from Sentinel-1 pairs; the pair planner uses 12-day same-orbit pairs |
| Latency | asynchronous job; `status: requested` in the ledger until downloaded |
| Credentials | `EARTHDATA_USERNAME/PASSWORD` |
| Adapter | `src/serac/adapters/eo/hyp3.py` (`Hyp3InsarAdapter`, `InSARPairPlanner`; planned) |
| Ledger source | `hyp3_insar` |
| Fixture | synthetic 32×32 coherence/LOS pair under `tests/fixtures/synthetic/` (`provenance: synthetic`) |
| Known gaps | no real HyP3 product in the tree; `s1_coherence_t` / `s1_los_velocity_t` are flagged synthetic in any cube built from fixtures |

## Sentinel-2 L2A via CDSE (production path)

| | |
|---|---|
| URL | Copernicus Data Space Ecosystem STAC / OData |
| Licence | to be recorded at fetch time |
| Cadence | 2–5 d, cloud-permitting (brief) |
| Latency | to be recorded at fetch time |
| Credentials | search: none; download: `CDSE_CLIENT_ID/SECRET` (OAuth client credentials) |
| Adapter | `src/serac/adapters/eo/cdse.py` (`CdseSentinel2Adapter`; planned) |
| Ledger source | `sentinel2_cdse` |
| Fixture | a real recorded CDSE search page (planned, `data/fixtures/`) |
| Known gaps | the download path is exercised only with fakes; no CDSE bytes fetched in the founding session |

## Sentinel-2 L2A via Earth Search (fixture / secondary source)

| | |
|---|---|
| URL | `https://earth-search.aws.element84.com/v1` (STAC); public COGs |
| Licence | to be recorded at fetch time |
| Cadence | as Sentinel-2 |
| Latency | to be recorded at fetch time |
| Credentials | none |
| Adapter | `src/serac/adapters/eo/earthsearch.py` (`EarthSearchSentinel2Adapter`; planned); shares `s2_cloud.py` with the CDSE adapter |
| Ledger source | `sentinel2_earthsearch` |
| Fixture | 2–3 real Chamoli date crops of B03/B11/SCL, 64×64 px (planned, `data/fixtures/`) |
| Known gaps | documented as secondary; CDSE remains the production adapter (ADR-0006) |

## NISAR L-band via asf_search

| | |
|---|---|
| URL | `asf_search`, platform NISAR; downloads via Earthdata |
| Licence | to be recorded at fetch time |
| Cadence | 12 d (brief) |
| Latency | to be recorded at fetch time |
| Credentials | search: none; download: `EARTHDATA_USERNAME/PASSWORD` |
| Adapter | `src/serac/adapters/eo/nisar.py` + `nisar_constraints.py` (planned) |
| Ledger source | `nisar_asf` |
| Fixture | the real search probe response (planned, `data/fixtures/`) |
| Known gaps | calibrated PROVISIONAL products exist only for acquisitions from 17 Jun 2026 (released 20 Jul 2026); BETA products Oct 2025–Jan 2026 are not inter-comparable; permanent instrument gap 27 Jul–10 Aug 2026; the adapter refuses to mix BETA and PROVISIONAL silently. As of 2026-09-03 the search returns only ancillary SCLKSCET files, so NISAR is `status: not_fetched` and **no calibrated pre-Langtang series exists**. |

## Copernicus GLO-30 DEM

| | |
|---|---|
| URL | public COGs on `copernicus-dem-30m.s3.amazonaws.com` |
| Licence | to be recorded at fetch time |
| Cadence | static |
| Latency | n/a |
| Credentials | none |
| Adapter | `src/serac/adapters/eo/dem.py` (`Glo30DemAdapter`; windowed reads; `ports/dem.py` hook for licensed DEMs; planned) |
| Ledger source | `dem_glo30` |
| Fixture | real DEM crops for all three AOIs (planned, `data/fixtures/`) |
| Known gaps | 30 m only; higher-resolution DEMs need a licence and go through the `DemProvider` hook |

## ERA5 via cdsapi

| | |
|---|---|
| URL | `https://cds.climate.copernicus.eu/api` (`CDSAPI_URL`) |
| Licence | to be recorded at fetch time (dataset licence accepted on the CDS site) |
| Cadence | reanalysis; product cadence to be recorded at fetch time |
| Latency | to be recorded at fetch time |
| Credentials | `CDSAPI_KEY` |
| Adapter | `src/serac/adapters/eo/era5.py` (`Era5Adapter`; planned) |
| Ledger source | `era5_cds` |
| Fixture | synthetic regridding sample under `tests/fixtures/synthetic/` |
| Known gaps | no key in the founding session; `era5_t2m_t` is `not_fetched` in fixture cubes |

## GACOS tropospheric corrections

| | |
|---|---|
| URL | GACOS web form; results delivered by email |
| Licence | to be recorded at fetch time |
| Cadence | on request |
| Latency | request/poll; human-in-the-loop |
| Credentials | `GACOS_EMAIL` |
| Adapter | `src/serac/adapters/eo/gacos.py` (`GacosAdapter.request()` records `status: requested`; `serac ingest gacos --receive URL --request-id …` completes it; planned) |
| Ledger source | `gacos` |
| Fixture | none real; absent without a request |
| Known gaps | nothing fetched; the request pattern is tested on recorded responses only |

## FDSN waveforms (EarthScope, GEOFON)

| | |
|---|---|
| URL | EarthScope `https://service.earthscope.org` (ObsPy 1.5.1 maps the `IRIS` alias here; IRIS DMC services migrated); GEOFON `https://geofon.gfz.de` |
| Licence | to be recorded at fetch time, per network |
| Cadence | continuous archive |
| Latency | archive; not real-time |
| Credentials | none for open networks |
| Adapter | `src/serac/adapters/seismic/fdsn.py` (`FdsnWaveformArchive`; records the resolved base URL, never the alias; planned) |
| Ledger source | `fdsn_waveforms` |
| Fixture | real MiniSEED + StationXML slices: `chamoli-2021` (NK.KKN, IC.LSA), `langtang-2026` (NK.KKN, IO.EVN) (planned, `data/fixtures/seismic/`) |
| Known gaps | open broadband stations verified with data: `NK.KKN` (Kakani, Nepal, 27.8N 85.279E, ~55 km from the Lhende source zone), `IO.EVN` (Everest Pyramid), `IC.LSA` (Lhasa). **No open broadband station lies within 300 km of Chamoli.** |

## SeedLink (real-time waveforms)

| | |
|---|---|
| URL | `SERAC_SEEDLINK_SERVER`, default `geofon.gfz.de:18000` |
| Licence | as FDSN, per network |
| Cadence | real-time records |
| Latency | to be recorded with `make smoke-online` |
| Credentials | none |
| Adapter | `src/serac/adapters/seismic/seedlink.py`, `src/serac/streaming/seedlink_ingestor.py` (planned) |
| Ledger source | `seedlink` |
| Fixture | none (replay uses FDSN fixtures) |
| Known gaps | the endpoint is **unverified**; no live connection was made in the founding session |

## USGS ComCat

| | |
|---|---|
| URL | USGS earthquake catalogue (ComCat) FDSN event service, geojson |
| Licence | to be recorded at fetch time |
| Cadence | continuous; events revised over time |
| Latency | minutes to days for revisions |
| Credentials | none |
| Adapter | `src/serac/adapters/seismic/usgs_comcat.py` (`ComCatCatalog`, `eventtype=landslide`; planned) |
| Ledger source | `usgs_comcat` |
| Fixture | the real 57-event landslide response and the `us7000tbwb` / `us7000tc90` geojson (planned, `data/fixtures/`) |
| Known gaps | `eventtype=landslide` returns only **57 events since 2000, mostly Alaska ml 1–2**; **Chamoli 2021 is absent**; the labelled set is small. Verified: `us7000tbwb` M5.2 `ms_vx`, type landslide, 2026-08-26T02:52:10Z, 28.271N 85.515E; `us7000tc90` M4.2 `ms_vx` landslide, 2026-08-26T06:00:35Z. |

## Hydrometric (Nepal DHM not open; ICIMOD public reporting as fixture)

| | |
|---|---|
| URL | Nepal DHM gauges have no stable open API; ICIMOD public reporting is used as a fixture source |
| Licence | to be recorded at fetch time, per document |
| Cadence | n/a (reported values, not a feed) |
| Latency | n/a |
| Credentials | none |
| Adapter | `src/serac/ports/seismic.py` (`HydrometricSource`), `src/serac/adapters/hydro/icimod_fixture.py` (`IcimodReportedHydrometric`) |
| Ledger source | `hydrometric_icimod` |
| Fixture | `data/fixtures/hydro/icimod_trishuli_2026-08-26.json`: Galchhi (+9 m in 30 min) and Malekhu (+7 m) stage changes transcribed from the ICIMOD media advisory of 26 Aug 2026, each observation quoting its sentence; no clock time is stated in the source, so `time_utc` is null; the page is all-rights-reserved and cited only |
| Known gaps | **no open real-time Nepal/China hydrometric feed**; anything not in the fixture is `status: not_fetched` and raises `DatasetNotFetchedError` |

## OSM Overpass

| | |
|---|---|
| URL | Overpass API |
| Licence | ODbL (attribution required) |
| Cadence | n/a |
| Latency | n/a |
| Credentials | none |
| Adapter | AOI build step (planned, domain-modeller branch) |
| Ledger source | `osm_overpass` |
| Fixture | the raw Overpass response for the corridor bbox (planned, `data/fixtures/`) |
| Known gaps | verified 2026-09-03: `waterway=river` ways exist for Lhende Khola / Bhote Koshi / Trishuli; if Overpass is unavailable the fallback is a hand-digitised line flagged `hand_digitised_approximate` |

## Primary literature via Crossref

| | |
|---|---|
| URL | `https://api.crossref.org/works/<doi>` and publisher landing pages |
| Licence | per article; paywalled PDFs are never stored (`licence: all-rights-reserved; cited-only`, sha256 of the landing page) |
| Cadence | n/a |
| Latency | n/a |
| Credentials | none |
| Adapter | `serac sources fetch` (planned) → `ManifestEntry(source=source_document)` + `SourceRef` |
| Ledger source | `source_document` |
| Known gaps | DOIs recalled from memory were frequently wrong in recon; a DOI is cited only after it resolved in-session (citation rule in `CLAUDE.md`). Wikipedia, blogs and social media are never sources. |
