# Data sources

One section per source serac reads, plus the two ledger sources serac writes itself. Facts
here come from the founding brief, from the recon of 2026-09-03, or from the fetches of
2026-09-03/04 that the provenance ledger records. Anything not verified is written as "to be
recorded at fetch time". Licence text is copied into `data/manifest.jsonl` (`licence`,
`licence_source_url`) by the adapter at the moment bytes are retrieved; this document does not
restate licences it could not verify.

**This document is checked, not trusted.** `tests/unit/test_data_sources_doc.py` reads
`data/manifest.jsonl` and the tree and fails if a `source` or an `adapter` that has written a
ledger row has no section here, if a host serac fetched bytes from is not named, if an adapter
module under `adapters/eo`, `adapters/seismic` or `adapters/hydro` is undocumented, if a
repository path cited here does not exist, if something already in the tree is still tagged as
future work, or if a sentence claims the environment lacks a package that imports. Every module
and fixture named below exists in the tree today.

Cross-cutting gaps (repeated in `RELEASE_STATUS.md`):

- No calibrated pre-Langtang NISAR series exists (constraints below).
- The USGS ComCat `eventtype=landslide` set is sparse and does not contain Chamoli 2021.
- No open broadband seismic station lies within 300 km of Chamoli.
- No open real-time hydrometric feed exists for the Nepal/China corridor.

## Sentinel-1 SLC/GRD via ASF (search) and Earthdata (download)

| | |
|---|---|
| URL | ASF search via `asf_search` against `api.daac.asf.alaska.edu` (public); downloads via Earthdata-authenticated ASF endpoints |
| Licence | to be recorded at fetch time |
| Cadence | 6–12 d (brief) |
| Latency | to be recorded at fetch time |
| Credentials | search: none; download: `EARTHDATA_USERNAME/PASSWORD` |
| Adapter | `src/serac/adapters/eo/asf_sentinel1.py` (`Sentinel1AsfAdapter`; `serac ingest s1`) |
| Ledger source | `sentinel1_asf` |
| Fixture | `data/fixtures/asf/chamoli_s1_2021-01-01_2021-02-28.geojson` (real ASF listing, 53 IW granules: 29 SLC / 24 GRD_HD on paths 56, 63, 129, 165) |
| Ledger state | 1 row, `status: listed`. No SLC or GRD bytes have ever been downloaded through this adapter |
| Known gaps | the SLC/GRD download path has never run: `Sentinel1AsfAdapter` reads `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD` only, and no such pair was available in any session (the Earthdata credential in this environment is a bearer token, which this adapter does not accept). The Sentinel-1 signal serac actually holds is HyP3 burst InSAR (below), not SLC or GRD scenes |

## Sentinel-1 `SLC-BURST` listings via ASF

| | |
|---|---|
| URL | `asf_search.geo_search(dataset="SLC-BURST")` against `api.daac.asf.alaska.edu`; documented at `https://docs.asf.alaska.edu/asf_search/basics/` |
| Licence | Copernicus Sentinel data, free and open under the Sentinel Data Legal Notice; burst extraction and hosting by ASF DAAC |
| Cadence | as Sentinel-1 |
| Latency | search only; no bytes beyond the listing |
| Credentials | none (search is public; only processing needs a credential) |
| Adapter | `src/serac/adapters/eo/asf_bursts.py` (`AsfBurstSearchClient`, `search_in_chunks`, `write_listing`); the burst vocabulary the M3 track-selection rule and the HyP3 pair planner both read |
| Ledger source | none — this is a search listing, not a product |
| Fixture | none committed; listings are cached under `data/interim/watch/bursts/` (DVC-tracked, not in git) |
| Known gaps | the listing cache under `data/interim/watch/bursts/` carries **no ledger row**, though nothing may be written under `data/` without one (`RELEASE_STATUS.md` known gap 71). The module docstring claimed the listings were ledgered; that sentence has been corrected rather than the ledger backfilled, because a row written now would carry a retrieval time and a checksum for bytes fetched on 2026-09-03, and the listing is the input to the frozen M3 track-selection rule |

## HyP3 InSAR — full-frame `INSAR_GAMMA`

| | |
|---|---|
| URL | ASF HyP3 via `hyp3-sdk` 7.7 |
| Licence | HyP3 products are derived from Copernicus Sentinel data (Sentinel Data Legal Notice) and distributed by ASF free of charge; attribution "ASF DAAC HyP3 [year]; contains modified Copernicus Sentinel data [year], processed by ESA" |
| Cadence | derived from Sentinel-1 pairs; the pair planner uses 12-day same-orbit pairs |
| Latency | asynchronous job; `status: requested` in the ledger until downloaded |
| Credentials | `EARTHDATA_USERNAME/PASSWORD` |
| Adapter | `src/serac/adapters/eo/hyp3_insar.py` (`Hyp3InsarAdapter`, `InSARPairPlanner`; `serac ingest hyp3 [--poll|--wait]`; jobs ledger `data/raw/hyp3_insar/<aoi>/jobs.jsonl`) |
| Ledger source | `hyp3_insar` |
| Sharing | the burst adapter below writes the same `Ledger source`; the ledger's adapter field separates them |
| Fixture | synthetic 32×32 px (80 m, EPSG:32644) coherence/LOS pair `tests/fixtures/synthetic/hyp3/chamoli-rishiganga/S1_063_20210130_20210211/` (`provenance: synthetic`, adapter `synthetic-fixture`; the pair name is a real ASF-listed pair, the pixels are not observations) |
| Ledger state | 2 rows, both the synthetic fixture pair (`product_level: INSAR_GAMMA`) |
| Known gaps | no full-frame GAMMA product was ever ordered: at 10 credits and 300 MB–1 GB per pair it does not fit the disk or credit budget of this machine, and burst InSAR replaced it (see below) |

## HyP3 burst InSAR — `INSAR_ISCE_MULTI_BURST`

| | |
|---|---|
| URL | HyP3 REST API `https://hyp3-api.asf.alaska.edu`; delivered products are served from the ASF CloudFront distribution `d3gm2hf49xd6jj.cloudfront.net` |
| Licence | HyP3 products are derived from Copernicus Sentinel data (Sentinel Data Legal Notice) and distributed by ASF free of charge; attribution "ASF DAAC HyP3 [year]; contains modified Copernicus Sentinel data [year], processed by ESA" |
| Cadence | one interferogram per Sentinel-1 burst pair; the M3 network is laid out by the frozen track-selection rule |
| Latency | asynchronous job, hours; `status: requested` until the zip is delivered |
| Credentials | `EARTHDATA_TOKEN` (a bearer token, not a username/password — this adapter talks to the HyP3 REST API directly for that reason) |
| Adapter | `src/serac/adapters/eo/hyp3_burst.py` (adapter name `hyp3_burst_insar`; `serac watch submit-insar` / `serac watch poll-insar`), planner `src/serac/models/watch/insar_jobs.py` |
| Ledger source | `hyp3_insar` |
| Fixture | none synthetic of its own; the offline tests drive the adapter with fakes and use the crops under `data/raw/hyp3_burst_insar/` when present |
| Ledger state | 4,136 rows on 2026-09-03: 517 `requested` (260 chamoli-rishiganga, 257 lhende-khola-trishuli), 517 `fetched` product zips with `retention: transient`, and 3,102 `fetched` AOI crops with `retention: retained` under `data/raw/hyp3_burst_insar/<aoi>/<pair>/` (1,560 chamoli-rishiganga, 1,542 lhende-khola-trishuli) |
| Known gaps | **the 517 delivered zips were hashed on arrival, cropped to the AOI and deleted** (20.05 GB); those rows carry `retention: transient` and `validate-ingest` reports them as a named warning because they can never be re-hashed. `data/raw/` is DVC-tracked and absent from a fresh git clone. The cube's `s1_coherence_t` / `s1_los_velocity_t` layers still prefer the synthetic placeholder over these real interferograms (`RELEASE_STATUS.md` known gap 11): the layer builder scans the ledger rather than selecting by `raw_root`, and `tests/unit/pipelines/test_layers_s2_s1.py` pins that behaviour until it is fixed |

## Sentinel-2 L2A via CDSE (production path)

| | |
|---|---|
| URL | Copernicus Data Space Ecosystem STAC (`stac.dataspace.copernicus.eu`) / OData; OAuth at `identity.dataspace.copernicus.eu` |
| Licence | to be recorded at fetch time |
| Cadence | 2–5 d, cloud-permitting (brief) |
| Latency | to be recorded at fetch time |
| Credentials | search: none; download: `CDSE_CLIENT_ID/SECRET` (OAuth client credentials) |
| Adapter | `src/serac/adapters/eo/cdse_sentinel2.py` (`CdseSentinel2Adapter`; `serac ingest s2-cdse`); shares `src/serac/adapters/eo/s2_cloud.py` with the Earth Search adapter |
| Ledger source | `sentinel2_cdse` |
| Fixture | `data/fixtures/cdse/chamoli_s2_search_2021-02.json` (real CDSE STAC search page, 5 items) |
| Ledger state | 1 row, `status: listed` |
| Known gaps | the download path is exercised only with fakes; no CDSE bytes have been fetched |

## Sentinel-2 L2A via Earth Search (secondary source, real crops)

| | |
|---|---|
| URL | `https://earth-search.aws.element84.com/v1` (STAC); public COGs from `sentinel-cogs.s3.us-west-2.amazonaws.com` |
| Licence | to be recorded at fetch time |
| Cadence | as Sentinel-2 |
| Latency | to be recorded at fetch time |
| Credentials | none |
| Adapter | `src/serac/adapters/eo/earthsearch_sentinel2.py` (`EarthSearchSentinel2Adapter`, adapter name `sentinel2_earthsearch`; `serac ingest s2-earthsearch`) |
| Ledger source | `sentinel2_earthsearch` |
| Fixture | 3 real Chamoli scene crops of B03/B11/SCL (256×256 px at 10 m / 128×128 px at 20 m) under `data/fixtures/sentinel2/chamoli-rishiganga/` |
| Ledger state | 73 rows: 1 `listed` search page and 72 `fetched` band crops (`data/raw/sentinel2_earthsearch/`, `data/fixtures/sentinel2/`) |
| Known gaps | documented as secondary; CDSE remains the production adapter (ADR-0006) |

## NISAR L-band via asf_search

| | |
|---|---|
| URL | `asf_search`, platform NISAR, against `api.daac.asf.alaska.edu`; downloads via Earthdata |
| Licence | to be recorded at fetch time |
| Cadence | 12 d (brief) |
| Latency | to be recorded at fetch time |
| Credentials | search: none; download: `EARTHDATA_USERNAME/PASSWORD` |
| Adapter | `src/serac/adapters/eo/nisar.py` + `src/serac/adapters/eo/nisar_constraints.py` (`NisarAdapter`, `classify_collection`, `MixedProductLevelError`; `serac ingest nisar [--level beta|provisional]`) |
| Ledger source | `nisar_asf` |
| Fixture | `data/fixtures/asf/nisar_probe_2026-09-03.json` (real `asf_search` probe over Lhende: 159 science granules; per-file URL/size lists stripped) |
| Ledger state | 1 row, `status: listed` |
| Known gaps | calibrated PROVISIONAL products exist only for acquisitions from 17 Jun 2026 (released 20 Jul 2026); BETA products Oct 2025–Jan 2026 are not inter-comparable; permanent instrument gap 27 Jul–10 Aug 2026; the adapter refuses to mix BETA and PROVISIONAL silently. Nothing has been downloaded (no Earthdata Login username/password), so NISAR is `listed` only and **no calibrated pre-Langtang series exists**. The GCOV HDF5 reader has `h5py` 3.16.0 available but has never parsed a real product (`RELEASE_STATUS.md` known gap 13) |

### Product-level rule (verified on the probe, 2026-09-03)

The level discriminator is the CMR **`collectionName`**: `NISAR_L<n>_<LEVEL>_BETA_V1` versus
`NISAR_L<n>_<LEVEL>_PROVISIONAL_V1`. `productionConfiguration` is `"PR"` on all 159 science
granules of the probe and is **not** a discriminator; the probe's own
`serac_probe.by_level_and_production_configuration` bucket (`GCOV/PR: 21`, ...) therefore says
nothing about maturity and must not be read as such (the fixture was not re-fetched to remove
it). `crid` is a consistency check only: `X05009`/`X05010` on BETA, `P05023` on PROVISIONAL.

Probe facts encoded as tests (`tests/unit/adapters/eo/test_nisar.py`): 72 BETA granules,
acquisitions 2025-11-25 .. 2026-01-15; 87 PROVISIONAL granules, acquisitions 2026-06-20 ..
2026-08-31 (consistent with "from 17 Jun 2026"); no acquisition 2026-07-27 .. 2026-08-10
(the instrument gap); science levels present: RSLC, GSLC, GCOV, RIFG, RUNW, GUNW, ROFF, GOFF,
SME2. Raw listings are dominated by ancillary products (`ECMWF_SMST`, `RRSD`, SCLKSCET, orbit
files); `NisarAdapter.search` keeps only `SCIENCE_LEVELS`. A request that matches both BETA
and PROVISIONAL is refused with `MixedProductLevelError` unless `--level` is explicit; a
granule whose level cannot be established (`NisarLevel.unknown`) is always refused.

## Copernicus GLO-30 DEM

| | |
|---|---|
| URL | public COGs on `copernicus-dem-30m.s3.amazonaws.com` |
| Licence | to be recorded at fetch time |
| Cadence | static |
| Latency | n/a |
| Credentials | none |
| Adapter | `src/serac/adapters/eo/dem_glo30.py` (`Glo30DemAdapter`, adapter name `dem_glo30`; windowed reads; `src/serac/ports/dem.py` hook for licensed DEMs; `serac ingest dem`) |
| Ledger source | `dem_glo30` |
| Fixture | real DEM crops for all three AOIs under `data/fixtures/dem_glo30/` |
| Ledger state | 6 rows, all `fetched` (3 crops under `data/fixtures/dem_glo30/`, 3 under `data/raw/dem_glo30/`) |
| Known gaps | 30 m only; higher-resolution DEMs need a licence and go through the `DemProvider` hook. The committed Chamoli crop covers the source zone, not the whole corridor AOI, so the cube's terrain layers are `status: partial` there (`RELEASE_STATUS.md` known gap 12) |

## ERA5 via cdsapi

| | |
|---|---|
| URL | `https://cds.climate.copernicus.eu/api` (`CDSAPI_URL`) |
| Licence | to be recorded at fetch time (dataset licence accepted on the CDS site) |
| Cadence | reanalysis; product cadence to be recorded at fetch time |
| Latency | to be recorded at fetch time |
| Credentials | `CDSAPI_KEY` |
| Adapter | `src/serac/adapters/eo/era5_cds.py` (`Era5Adapter`; `serac ingest era5`) |
| Ledger source | `era5_cds` |
| Fixture | synthetic regridding sample `tests/fixtures/synthetic/era5/regrid_sample.nc` (NetCDF-3, fictional AOI id `synthetic-regrid-sample`) |
| Ledger state | 1 row, `status: synthetic` — the regridding sample |
| Known gaps | no CDS key; `era5_t2m_t` is `not_fetched` in fixture cubes. CDS delivers NetCDF-4, which needs `h5py`; `h5py` 3.16.0 is in the locked environment (pulled in by `mintpy` and `neuraloperator`), so the reader can run, but it has never been given a real ERA5 file (`RELEASE_STATUS.md` known gap 13) |

## GACOS tropospheric corrections

| | |
|---|---|
| URL | GACOS web form; results delivered by email |
| Licence | to be recorded at fetch time |
| Cadence | on request |
| Latency | request/poll; human-in-the-loop |
| Credentials | `GACOS_EMAIL` |
| Adapter | `src/serac/adapters/eo/gacos.py` (`GacosAdapter.request()` records `status: requested` with the form values and a `request_id`; `serac ingest gacos --poll --request-id ID`; `serac ingest gacos --receive URL --request-id ID` downloads the e-mailed archive and records `fetched`). The GACOS form endpoint is not verified: by default the operator submits the printed form values on the site by hand and the ledger row is the receipt |
| Ledger source | `gacos` |
| Fixture | none real; absent without a request |
| Ledger state | no rows |
| Known gaps | nothing requested and nothing fetched; the request/poll/receive pattern is tested with fakes only |

## FDSN waveforms (EarthScope, GEOFON, RESIF, ORFEUS, INGV)

| | |
|---|---|
| URL | EarthScope `https://service.earthscope.org` (ObsPy 1.5.1 maps the `IRIS` alias here; IRIS DMC services migrated); GEOFON `https://geofon.gfz.de`; RESIF `https://ws.resif.fr`; ORFEUS `https://www.orfeus-eu.org`; INGV `https://webservices.ingv.it` |
| Licence | `null: see licence_source_url`, per data centre — the centres consulted publish terms of service rather than a licence. EarthScope's terms require acknowledging EarthScope Consortium (NSF award 2435260) and citing network DOIs; GEOFON data are served under the GEOFON data policy. Attribution requirements go in the ledger `notes` |
| Cadence | continuous archive |
| Latency | archive; not real-time |
| Credentials | none for open networks |
| Adapter | `src/serac/adapters/seismic/fdsn.py` (`FdsnWaveformArchive`; records the resolved base URL, never the alias — ADR-0015). Bulk fetchers: `scripts/fetch_seismic_fixtures.py` (adapter name `fixture-fetch`), `scripts/fetch_lfh_fixtures.py` (adapter name `fetch_lfh_fixtures`, data centres `EARTHSCOPE, GEOFON, ORFEUS, RESIF, INGV`), and `src/serac/pipelines/discriminator_build.py` (`DiscriminatorSetBuilder`) |
| Ledger source | `fdsn_waveforms` |
| Fixture | real MiniSEED + StationXML slices under `data/fixtures/seismic/` — `chamoli-2021` (NK.KKN, IC.LSA) and `langtang-2026` (NK.KKN, IO.EVN) — and the M2 LH? sets under `data/fixtures/lfh/` for `bingham-canyon-2013-1`, `taan-fiord-2015`, `lamplugh-glacier-2016`, `chamoli-2021`, `blatten-2025`, `langtang-lhende-2026`. Per-file URLs, checksums and sizes are in `data/fixtures/FIXTURES.md` |
| Ledger state | 632 rows: 366 `fetched` (6 phase-1 fixture files by `fixture-fetch`, 358 M2 LH? files by `fetch_lfh_fixtures`, 2 discriminator index files by `DiscriminatorSetBuilder`) and 266 `not_fetched`. 265 of those are M1 windows dataselect returned no data for — **not** substituted, backfilled or replaced by another event; the 266th records that one M2 StationXML file was superseded by its gzip and is no longer on disk (the ledger is append-only, so its original `fetched` row still stands) |
| Known gaps | open broadband stations verified with data: `NK.KKN` (Kakani, Nepal, 27.8N 85.279E, ~55 km from the Lhende source zone), `IO.EVN` (Everest Pyramid), `IC.LSA` (Lhasa). **No open broadband station lies within 300 km of Chamoli.** The M1 fixture rows carry no `event_id`/`aoi_id` (`RELEASE_STATUS.md` known gap 8) |

## SeedLink (real-time waveforms)

| | |
|---|---|
| URL | `SERAC_SEEDLINK_SERVER`, default `geofon.gfz.de:18000` |
| Licence | as FDSN, per network |
| Cadence | real-time records |
| Latency | to be recorded with `make smoke-online` |
| Credentials | none |
| Adapter | `src/serac/adapters/seismic/seedlink.py`, `src/serac/streaming/seedlink_ingestor.py` |
| Ledger source | `seedlink` |
| Fixture | none (replay uses FDSN fixtures) |
| Ledger state | no rows |
| Known gaps | the endpoint is **unverified**; no live connection has ever been made, and the ingestor is tested with a fake client only |

## EarthScope Syngine Green's functions

| | |
|---|---|
| URL | `https://service.iris.edu/irisws/syngine/1/query` |
| Licence | `null: see licence_source_url` → `https://ds.iris.edu/ds/products/syngine/`. Cite Krischer et al. (2017) and the Syngine data product DOI when reusing |
| Cadence | on demand (synthesis, not an archive) |
| Latency | seconds to minutes per request |
| Credentials | none |
| Adapter | `src/serac/adapters/seismic/syngine.py` (`SyngineGreensLibrary` behind the `GreensLibrary` port), plus `serac lfh fixtures` for the committed convention probes |
| Ledger source | `iris_syngine` |
| Fixture | `data/fixtures/greens/lfh/prem_a_20s/` (the committed, byte-stable library M2 inverts against) and `data/fixtures/greens/convention/` (`az90_probe.npz`, `rotation.npz`, `symmetry.npz` — the per-azimuth probes that pin the force convention in `tests/unit/adapters/test_greens_convention.py`) |
| Ledger state | 419 rows, all `fetched`: 405 under `data/interim/greens/prem_a_20s/` and 14 committed fixtures |
| Provenance | **`derived`, never `synthetic`** (ADR-0016). Green's functions are physics evaluated from a published 1-D Earth model (PREM, `prem_a_20s`) by AxiSEM/Instaseis — modelled, reproducible from stated inputs, and not a fabricated stand-in. Every row carries `params.modelled = true` |
| Known gaps | the M2 cold-latency figure depends on this third-party service being reachable (`RELEASE_STATUS.md` known gap 29); Green's functions are never published on the bus |

## ESEC — Exotic Seismic Events Catalog (IRIS/EarthScope SPUD)

| | |
|---|---|
| URL | `https://ds.iris.edu/spudservice/esec` — the endpoint content-negotiates, and only `Accept: application/xml` yields the real document (with no `Accept` header it returns the XML HTML-escaped inside a `<pre>` block) |
| Licence | public domain (US federally funded data product); acknowledge EarthScope. `https://ds.iris.edu/ds/products/esec/` |
| Cadence | a published catalogue, revised occasionally; 319 events, 1977–2024 |
| Latency | n/a |
| Credentials | none |
| Adapter | `src/serac/adapters/seismic/esec.py` (`EsecSpudCatalog`), and `scripts/build_lfh_references.py` (adapter name `build_lfh_references`) for the M2 reference set |
| Ledger source | `esec_spud` |
| Fixture | `data/fixtures/esec/esec_events_2026-09-03.xml` (the real document, byte-for-byte) and `data/fixtures/esec/esec_catalogue.xml.gz` |
| Ledger state | 6 rows, all `fetched` |
| Known gaps | ESEC states a unit only in a handful of tag names (`MaxdisthfKm`, `LocuncertKm`); `H`, `L`, `Volume`, `AreaTotal`, `Mass` and `PeakDischarge` carry **no unit** and the service publishes no schema, so those fields are parsed as bare floats with `*_unit: None` rather than a guessed metre or cubic metre. ESEC publishes no magnitude, so the M1 negative set is **not magnitude-matched** (`RELEASE_STATUS.md` known gap 22). 161 of 319 events have a crown location; the rest fall back to the nominal epicentre, recorded in `location_basis` |

## USGS ComCat

| | |
|---|---|
| URL | USGS earthquake catalogue (ComCat) FDSN event service at `earthquake.usgs.gov`, geojson |
| Licence | US-PD |
| Cadence | continuous; events revised over time |
| Latency | minutes to days for revisions |
| Credentials | none |
| Adapter | `src/serac/adapters/seismic/usgs_comcat.py` (`ComCatCatalog`, `eventtype=landslide`) |
| Ledger source | `usgs_comcat` |
| Fixture | the real 57-event landslide response and the `us7000tbwb` / `us7000tc90` / `us20002926` geojson under `data/fixtures/usgs_comcat/` |
| Ledger state | 4 rows, all `fetched` |
| Known gaps | `eventtype=landslide` returns only **57 events since 2000, mostly Alaska ml 1–2**; **Chamoli 2021 is absent**; the labelled set is small, and zero of those 57 events are in the built M1 store (`RELEASE_STATUS.md` known gap 6). Verified: `us7000tbwb` M5.2 `ms_vx`, type landslide, 2026-08-26T02:52:10Z, 28.271N 85.515E; `us7000tc90` M4.2 `ms_vx` landslide, 2026-08-26T06:00:35Z |

## RGI 7.0 glacier outlines (Bremen mirror)

| | |
|---|---|
| URL | `https://cluster.klima.uni-bremen.de/~fmaussion/misc/rgi7_data/rgi70_official/RGI2000-v7.0-G-global/` |
| Licence | CC-BY-4.0 (Randolph Glacier Inventory 7.0, RGI Consortium); `https://www.glims.org/rgi_user_guide/welcome.html` |
| Cadence | static (a versioned release: `RGI2000-v7.0-G`) |
| Latency | n/a |
| Credentials | none for the mirror |
| Adapter | `src/serac/models/watch/glaciers.py` (adapter name `rgi7_glaciers`); fills `SlopeUnit.glacier_cover`, which is a non-nullable bool |
| Ledger source | `rgi_glaciers` |
| Fixture | none committed; the regional archives are DVC-tracked under `data/raw/rgi_glaciers/` |
| Ledger state | 11 rows, all `fetched`: the RGI 7.0 regional archives for regions 13 (Central Asia), 14 (South Asia West) and 15 (South Asia East), plus the per-AOI clipped GeoParquet under `data/interim/watch/` (`provenance: derived`) |
| Known gaps | **this is a mirror, not the NSIDC original.** The authoritative distribution is NSIDC `nsidc0770_rgi_v7`, whose HTTPS endpoint needs an interactive Earthdata OAuth redirect that a bearer token cannot satisfy (it answers 401 to `Authorization: Bearer`). The files fetched are the official RGI 7.0 regional archives re-hosted by the University of Bremen climate group, who produced RGI 7.0, from the `rgi70_official/` path — the released version, not one of the beta levels also on that server. If the outlines cannot be fetched, `GlacierOutlines.available` is False, the slope-unit parquet is written with `glacier_cover = null`, and **no `SlopeUnit` records are emitted**; the contract is not relaxed. Region 11 (Central Europe) is in the lookup table for Blatten but has not been fetched |

## Hydrometric (Nepal DHM not open; ICIMOD public reporting as fixture)

| | |
|---|---|
| URL | Nepal DHM gauges have no stable open API; ICIMOD public reporting (`www.icimod.org`) is used as a fixture source |
| Licence | all-rights-reserved; cited-only, per document |
| Cadence | n/a (reported values, not a feed) |
| Latency | n/a |
| Credentials | none |
| Adapter | `src/serac/ports/seismic.py` (`HydrometricSource`), `src/serac/adapters/hydro/icimod_fixture.py` (`IcimodReportedHydrometric`) |
| Ledger source | `hydrometric_icimod` |
| Fixture | `data/fixtures/hydro/icimod_trishuli_2026-08-26.json`: Galchhi (+9 m in 30 min) and Malekhu (+7 m) stage changes transcribed from the ICIMOD media advisory of 26 Aug 2026, each observation quoting its sentence; no clock time is stated in the source, so `time_utc` is null; the page is all-rights-reserved and cited only |
| Ledger state | 1 row, `fetched` |
| Known gaps | **no open real-time Nepal/China hydrometric feed**; anything not in the fixture is `status: not_fetched` and raises `DatasetNotFetchedError` |

## OSM Overpass

| | |
|---|---|
| URL | Overpass API, `https://overpass-api.de/api/interpreter` (POST; the query is stored in the ledger `params`) |
| Licence | ODbL-1.0, © OpenStreetMap contributors (attribution required) |
| Cadence | n/a |
| Latency | n/a |
| Credentials | none |
| Adapter | `src/serac/pipelines/aoi_build.py` (adapter name `aoi-build`; `serac aoi build`) |
| Ledger source | `osm_overpass` |
| Fixture | the raw Overpass responses for all three AOI bboxes under `data/fixtures/osm/` |
| Ledger state | 3 rows, all `fetched` |
| Known gaps | verified 2026-09-03: `waterway=river` ways exist for Lhende Khola / Bhote Koshi / Trishuli; if Overpass is unavailable the fallback is a hand-digitised line flagged `hand_digitised_approximate`. `aoi-build` also writes `source_document` rows for the agency and gazetteer pages it reads (below) |

## Primary literature via Crossref

| | |
|---|---|
| URL | `https://api.crossref.org/works/<doi>`, publisher landing pages, and the agency/press pages the AOI and event libraries cite; the host of each is in its own ledger row |
| Licence | per article; paywalled PDFs are never stored (`licence: all-rights-reserved; cited-only`, sha256 of the landing page). Recorded licences range from CC-BY-4.0 to "No licence stated" |
| Cadence | n/a |
| Latency | n/a |
| Credentials | none |
| Adapter | `src/serac/pipelines/sources.py` (`serac sources fetch`) → `ManifestEntry(source=source_document)` + `SourceRef`; also `aoi-build` and `scripts/build_lfh_references.py` |
| Ledger source | `source_document` |
| Fixture | fetched landing pages under `data/raw/sources/`; the M2 published-reproduction table is `data/references/lfh_published.json` |
| Ledger state | 60 rows: 18 `fetched`, 42 `listed` (metadata resolved, bytes deliberately not stored) |
| Known gaps | DOIs recalled from memory were frequently wrong in recon; a DOI is cited only after it resolved in-session (citation rule in `CLAUDE.md`). Wikipedia, blogs and social media are never sources. Langtang 2026 figures are largely press-attributed, with `best: null` (`RELEASE_STATUS.md` known gap 7) |

## Vendored schemas (OASIS CAP 1.2, W3C XML Signature)

| | |
|---|---|
| URL | `https://docs.oasis-open.org/emergency/cap/v1.2/CAP-v1.2.xsd` and the W3C XML Signature core schema from `www.w3.org` |
| Licence | CAP: Copyright OASIS Open 2010 All Rights Reserved (OASIS IPR Policy), which permits verbatim copying with the notice retained. XML Signature: W3C Software Notice and License (19980720) |
| Cadence | static (a published standard) |
| Latency | n/a |
| Credentials | none |
| Adapter | `scripts/fetch_seismic_fixtures.py` (adapter name `fixture-fetch`); the schemas are consumed by `src/serac/adapters/cap/cap12.py` for offline XSD validation |
| Ledger source | `vendored_schema` |
| Fixture | `contracts/vendor/cap/CAP-v1.2.xsd` and `contracts/vendor/cap/xmldsig-core-schema.xsd`, committed verbatim so CAP validation runs with the network blocked |
| Ledger state | 2 rows, both `fetched` |
| Known gaps | none; both files are re-hashed by `tests/unit/test_fixture_integrity.py` |

## serac simulation outputs (not an external source)

| | |
|---|---|
| URL | none — these bytes are produced in this repository |
| Licence | Apache-2.0 (serac model output), derived from Copernicus GLO-30 (see the `dem_glo30` rows for the DEM's own licence) and OpenStreetMap corridor geometry (ODbL) |
| Cadence | per model run |
| Latency | n/a |
| Credentials | none |
| Adapter | `src/serac/models/runout/runner.py` (adapter name `runout_swe_voellmy`, versions 0.1.0 and 0.2.0) |
| Ledger source | `simulation_output` |
| Fixture | none; outputs live under `data/interim/runout/<aoi>/<run_id>/` (DVC-tracked) |
| Ledger state | 1,591 rows, all `fetched` / `provenance: derived`: 1,590 M4 runout artifacts for `lhende-khola-trishuli` (210 at solver 0.1.0, 1,380 at 0.2.0) and one mislabelled row, below |
| Known gaps | **NOT r.avaflow.** Flow depths, velocities and arrival times come from `serac-swe-voellmy`, a single-phase depth-averaged Voellmy-Salm solver implemented in this repository; r.avaflow could not be obtained and cross-validation against it is outstanding. Every one of the 1,590 runout rows carries that disclaimer in `notes`. One row is mislabelled: `data/regions/discriminator_regions.geojson` (written by `DiscriminatorSetBuilder`) is a serac-authored artefact, not a simulation output — see the next section |

## serac-authored artefacts

| | |
|---|---|
| URL | none — these bytes are produced in this repository |
| Licence | Apache-2.0 (this repository) |
| Cadence | n/a |
| Latency | n/a |
| Credentials | none |
| Adapter | none yet |
| Ledger source | `serac_artefact` |
| Fixture | n/a |
| Ledger state | **no rows.** The value exists in `DataSource` for a file serac authors that is neither an observation nor a simulation output |
| Known gaps | the one file in that class, `data/regions/discriminator_regions.geojson` — hand-drawn rectangles used to stratify the M1 evaluation, explicitly *not* an authoritative tectonic, physiographic or political boundary — was ledgered as `simulation_output` before this value existed, with a `SOURCE LABEL CAVEAT` in its `notes` saying so. The ledger is append-only and was not rewritten, so the mislabel stands and is recorded rather than hidden |

## Labelled synthetic stand-ins

| | |
|---|---|
| URL | none — a synthetic double stands in for an observation serac could not obtain |
| Licence | Apache-2.0 (this repository) |
| Cadence | n/a |
| Latency | n/a |
| Credentials | none |
| Adapter | `scripts/make_synthetic_fixtures.py` (adapter name `synthetic-fixture`) |
| Ledger source | `synthetic` |
| Fixture | everything under `tests/fixtures/synthetic/`, and nowhere else — `ManifestEntry` rejects a synthetic entry with any other path, and rejects anything synthetic under `data/` |
| Ledger state | **no rows carry this source.** A synthetic double is recorded under the source it stands in for, with `status: synthetic` and `provenance: synthetic`; three such rows exist (two HyP3 rasters, one ERA5 regridding sample) |
| Known gaps | `synthetic` means a fabricated stand-in and never modelled physics: Syngine Green's functions and runout outputs are `derived` (ADR-0016). Any layer or fixture flagged synthetic must be described as synthetic wherever it is mentioned |
