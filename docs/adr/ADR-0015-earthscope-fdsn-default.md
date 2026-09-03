# ADR-0015: EarthScope as the FDSN default (IRIS migration)

Date: 2026-09-03

## Status

Accepted

## Context

Recon on 2026-09-03 confirmed that the IRIS DMC FDSN web services have migrated to
EarthScope: ObsPy 1.5.1 maps the `IRIS` alias to `https://service.earthscope.org`. GEOFON is
served at `https://geofon.gfz.de`. Open broadband stations with real data around the target
AOIs: `NK.KKN` (Kakani, Nepal, 27.8N 85.279E, ~55 km from the Lhende source zone), `IO.EVN`
(Everest Pyramid), `IC.LSA` (Lhasa). Chamoli 2021 waveforms exist at `NK.KKN` and `IC.LSA`;
no open broadband station lies within 300 km of Chamoli.

## Decision

- `FdsnWaveformArchive` defaults to EarthScope (via the ObsPy `IRIS` alias) plus GEOFON.
- The adapter records the **resolved base URL** in every ledger entry, never the alias, so
  a future re-mapping of `IRIS` cannot silently change provenance.
- Docs and fixtures name EarthScope explicitly.

## Consequences

- Fixtures for `chamoli-2021` (NK.KKN, IC.LSA) and `langtang-2026` (NK.KKN, IO.EVN) are
  real MiniSEED slices attributed to the resolved service.
- The 300 km gap around Chamoli is a known limitation for any Chamoli-based latency claim
  and is listed in `RELEASE_STATUS.md`.
