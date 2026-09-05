# Credentials and endpoints

Every variable in `.env.example` is listed here. Copy `.env.example` to `.env`, fill in what
you have, and never commit `.env` (`.gitignore` excludes `.env` and `.env.*` except
`.env.example`). Settings are read by `src/serac/settings.py` (`SeracSettings`, via
pydantic-settings); secrets are `SecretStr` so they never print.

**None of the credentials below are required for the offline test suite (`make test`).**
**Assumed free (as of 2026-09-03; verify on registration).** None of these accounts is known to cost money, but this has not been verified in-session; non-negotiable 7 (ask before any paid API call) still applies.

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
| `SERAC_CAP_SIGNING_KEY` | path to the Ed25519 **private** key that signs CAP messages | generate locally with `serac alerting keygen` (no account, no service) | free | `alerting/keys.py`, `serac alerting cap --sign` |
| `SERAC_CAP_PUBLIC_KEY` | path to the matching **public** key | written beside the private key by `keygen`; publish it to anyone who must verify serac's messages | free | `serac alerting verify` |
| `SERAC_ALERT_HTTP_ENDPOINT` | where the outbound HTTP alert sink would POST | **you supply it.** There is no default and serac ships none | free | `adapters/alerting/http_sink.py` |
| `DVC_REMOTE_URL` | DVC remote (e.g. an S3 URL) | your own bucket or storage; written to the gitignored `.dvc/config.local` by `make dvc-remote` | storage costs are yours, not an API charge | DVC only; never read by `serac` code |
| `SERAC_ONLINE` | `1` enables network tests in `make smoke-online` | n/a | free | `tests/conftest.py` |
| `PROMOTE_APPROVED_BY` | **not a credential.** The name of the person approving a promotion. `make promote` refuses without it, and the name is recorded in `reports/promotion/<sha>.json`. Never set it in `.env`, a Makefile or CI: it exists to make promotion a person's act, so it belongs on the command line of the person doing it (`PROMOTE_APPROVED_BY='A. Name' make promote`). Boolean-ish or job-shaped values (`1`, `yes`, `ci`, `bot`) are refused | n/a | free | `validation/promote.py`, `serac promote` |

Not credentials but read from the environment by `SeracSettings`: `SERAC_DATA_DIR`
(default `data`), `SERAC_REPORTS_DIR` (default `reports`).

## CAP signing keys (M5)

serac signs CAP 1.2 messages with an **enveloped XML-Signature over Ed25519** (RFC 9231),
appended as the last child of `alert` — which the vendored CAP 1.2 XSD admits through its
`xmldsig` wildcard, so a signed message still validates. The key is generated locally; there
is no certificate authority, no service and no account.

### What an operator does

```
serac alerting keygen --out secrets/cap-signing.pem
# private key : secrets/cap-signing.pem (mode 0600, never printed, never committed)
# public key  : secrets/cap-signing.pub.pem (safe to publish)
# fingerprint : sha256:<64 hex>

export SERAC_CAP_SIGNING_KEY=secrets/cap-signing.pem
export SERAC_CAP_PUBLIC_KEY=secrets/cap-signing.pub.pem
```

Then `serac alerting cap --forecast <file> --sign` produces a signed message and
`serac alerting verify <file.cap.xml> --public-key secrets/cap-signing.pub.pem` checks it.

### Handling rules, enforced in code rather than by convention

1. **Never committed.** `write_private_key` runs `git check-ignore` and refuses any path git
   would track. `.gitignore` carries `*.pem` and `secrets/`. The refusal is tested.
2. **Never printed.** No function returns or logs private key bytes; the CLI prints a path and
   the public fingerprint only.
3. **Mode 0600 on write**, created with that mode rather than widened then narrowed. Loading a
   key that group or other can read raises `PrivateKeyPermissionsError`.
4. **No passphrase in v0.** A passphrase serac cannot prompt for is a passphrase stored next
   to the key. The control is filesystem permissions plus an operator's own secret store.
5. **Public key distribution is out of band.** `ds:KeyInfo` carries only a `ds:KeyName` of
   `ed25519:sha256:<fingerprint>` — the fingerprint, not the key — so verification needs the
   public PEM to have arrived by another route. A message that carried its own verification
   key would authenticate nothing.

### What the signature does and does not prove

It proves that the holder of one private key produced these exact bytes, and that the message
has not changed since. It does **not** say who that holder is: there is no certificate chain,
no revocation and no timestamp authority, and serac ships no trust store. Rotation is manual —
generate a new pair, distribute the new public key, and stop using the old one.

### Rotation and compromise

There is no revocation mechanism. If a key is suspected compromised, generate a new pair,
distribute the new public PEM to every recipient, and treat every message bearing the old
fingerprint as unverified from that moment. Recipients should pin the fingerprint they expect.

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
