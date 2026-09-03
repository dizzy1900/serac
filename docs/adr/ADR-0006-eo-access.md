# ADR-0006: EO access — asf_search, CDSE, HyP3, Earth Search as fixture source

Date: 2026-09-03

## Status

Accepted

## Context

The brief fixes `asf_search` for Sentinel-1 and NISAR, CDSE for Sentinel-2 L2A, and ASF
HyP3 for on-demand Sentinel-1 InSAR. Recon (2026-09-03) found: ASF search is public but S1
downloads, HyP3 and NISAR need Earthdata Login; CDSE STAC search is public but downloads need
OAuth; Sentinel-2 L2A COGs are readable credential-free via Earth Search STAC
(`earth-search.aws.element84.com/v1`); Copernicus GLO-30 COGs are public on
`copernicus-dem-30m.s3.amazonaws.com`; `asf_search` with platform NISAR currently returns
only ancillary SCLKSCET files. No credentials were available in the session.

## Decision

- Sentinel-1 listing: `asf_search` (`Sentinel1AsfAdapter`). InSAR: HyP3 via `hyp3-sdk`
  (`Hyp3InsarAdapter`), jobs ledgered as `status: requested` until downloaded.
- NISAR: `asf_search` with the product-level constraints from the brief encoded as
  constants in `nisar_constraints.py`; the adapter refuses to mix BETA and PROVISIONAL
  unless `--level` is explicit, warns on the instrument gap, and denylists ancillary files.
- Sentinel-2 L2A in production: CDSE STAC + OAuth client credentials
  (`CdseSentinel2Adapter`). Earth Search (`EarthSearchSentinel2Adapter`) is a documented
  **fixture and secondary source** sharing the same cloud-selection logic (`s2_cloud.py`);
  it is what will produce the committed Chamoli fixtures.
- DEM: Copernicus GLO-30 public COGs with windowed reads; a `DemProvider` port hook for
  licensed higher-resolution DEMs.

## Consequences

- Without credentials, S1/HyP3 layers are labelled synthetic placeholders under
  `tests/fixtures/synthetic/`, ERA5/GACOS/NISAR are `not_fetched`, and the release ledger
  says so. Dropping credentials into `.env` switches the adapters to real fetches.
- The CDSE fetch path is exercised only against fakes until someone runs
  `make smoke-online` with CDSE credentials.
