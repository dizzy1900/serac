# ADR-0013: fixture policy — real in `data/fixtures`, synthetic only in `tests/fixtures/synthetic`

Date: 2026-09-03

## Status

Accepted

## Context

Non-negotiable 1 forbids passing synthetic data off as real. Tests need small samples, and
some layers (S1/HyP3, ERA5, GACOS) could not be fetched without credentials in the founding
session.

## Decision

- `data/fixtures/`: tiny, real, licence-clean samples (DEM crops, windowed Sentinel-2
  scenes, an ASF listing, a CDSE search page, the NISAR probe, the Overpass response, the
  ComCat geojson, MiniSEED slices, StationXML, vendored XSDs). Each is described in
  `data/fixtures/FIXTURES.md` and has a `status: fetched` ledger entry with URL, retrieval
  time, sha256, size and licence.
- `tests/fixtures/synthetic/`: the **only** place synthetic data may live, each with a ledger
  entry `provenance: synthetic`, `status: synthetic`, and `notes` explaining the placeholder.
- `ManifestEntry` enforces this: a synthetic entry must have `status: synthetic`, its path
  must start with `tests/fixtures/synthetic/`, and nothing synthetic may be written under
  `data/`. `make validate-ingest` re-hashes every `fetched` entry.
- Stages that consume traces refuse synthetic chunks unless explicitly allowed.

## Consequences

- A fixture cube built from `data/fixtures` can still contain synthetic layers; the cube's
  `contains_synthetic` attr and the release ledger say so.
- Adding a fixture is a two-step act (bytes + ledger entry); a bare file under `data/` is a
  validation failure.
