# Credentials and endpoints

Every variable in `.env.example` is listed here. Copy `.env.example` to `.env`, fill in what
you have, and never commit `.env` (`.gitignore` excludes `.env` and `.env.*` except
`.env.example`). Settings are read by `src/serac/settings.py` (`SeracSettings`, via
pydantic-settings); secrets are `SecretStr` so they never print.

**None of the credentials below are required for the offline test suite (`make test`).**
**None of them cost money.** Every account listed is free to register.

## Ask-first rules

Before doing either of the following, stop and ask the human running the session:

1. Any download larger than **5 GB** (adapters print estimated bytes in `--dry-run` and
   gate the real fetch behind a confirmation).
2. Any credentialed API call that costs money. As of 2026-09-03 no adapter in this repo
   calls a paid API; if one is ever added, it must be documented here first.

## Variables

| Variable | What it is | Where to obtain it | Cost | Adapters that need it |
|---|---|---|---|---|
| `EARTHDATA_USERNAME`, `EARTHDATA_PASSWORD` | NASA Earthdata Login | `https://urs.earthdata.nasa.gov` (free account) | free | Sentinel-1 downloads via ASF (`adapters/eo/asf.py`), HyP3 InSAR jobs (`adapters/eo/hyp3.py`), NISAR products (`adapters/eo/nisar.py`). ASF **search** is public and needs no login. |
| `CDSE_CLIENT_ID`, `CDSE_CLIENT_SECRET` | Copernicus Data Space Ecosystem OAuth client credentials | register at `https://dataspace.copernicus.eu`, then create an OAuth client in the account settings | free | Sentinel-2 L2A downloads via CDSE (`adapters/eo/cdse.py`). CDSE STAC **search** is public. |
| `CDSAPI_URL` | Copernicus Climate Data Store API endpoint | default `https://cds.climate.copernicus.eu/api` | free | ERA5 (`adapters/eo/era5.py`) |
| `CDSAPI_KEY` | CDS API key | `https://cds.climate.copernicus.eu` account page (free account; dataset licences must be accepted on the site once) | free | ERA5 (`adapters/eo/era5.py`) |
| `GACOS_EMAIL` | email address used for GACOS requests | any address you control; GACOS delivers corrections by email after a web-form request | free | GACOS request/poll (`adapters/eo/gacos.py`) |
| `SERAC_REDIS_URL` | Redis connection URL for the Streams bus | default `redis://localhost:6379/0`; provided by `infra/docker/compose.yaml` | free | `adapters/bus/redis_streams.py`; tests marked `redis` |
| `SERAC_SEEDLINK_SERVER` | SeedLink `host:port` | default `geofon.gfz.de:18000` (unverified live; see `RELEASE_STATUS.md`) | free | `streaming/seedlink_ingestor.py`, `adapters/seismic/seedlink.py` |
| `DVC_REMOTE_URL` | DVC remote (e.g. an S3 URL) | your own bucket or storage; written to the gitignored `.dvc/config.local` by `make dvc-remote` | storage costs are yours, not an API charge | DVC only; never read by `serac` code |
| `SERAC_ONLINE` | `1` enables network tests in `make smoke-online` | n/a | free | `tests/conftest.py` |

Not credentials but read from the environment by `SeracSettings`: `SERAC_DATA_DIR`
(default `data`), `SERAC_REPORTS_DIR` (default `reports`).

## Sources that need no credential at all

- Copernicus GLO-30 DEM COGs on `copernicus-dem-30m.s3.amazonaws.com`.
- Sentinel-2 L2A COGs via Earth Search STAC (`earth-search.aws.element84.com/v1`) — the
  fixture/secondary source (ADR-0006).
- FDSN web services at EarthScope (`https://service.earthscope.org`) and GEOFON
  (`https://geofon.gfz.de`).
- USGS ComCat.
- OSM Overpass (ODbL attribution required).
- Crossref API (for resolving DOIs).

## What happens when a credential is missing

An adapter that needs a credential and does not find it writes a `status: not_fetched`
entry to `data/manifest.jsonl` and raises `CredentialsMissingError`
(`src/serac/errors.py`) so the CLI exits non-zero. It never substitutes data. With
`--dry-run` it prints what it would fetch and writes nothing.

## Rules

- Read from `.env` (or the process environment); never commit.
- Never paste a credential into a test, a fixture, a manifest entry, a log line or a report.
- Rotate any credential that was ever committed by mistake and record the incident in
  `RELEASE_STATUS.md` Known gaps until rotated.
