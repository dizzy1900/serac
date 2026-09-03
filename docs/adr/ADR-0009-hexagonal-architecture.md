# ADR-0009: Hexagonal architecture and package layout

Date: 2026-09-03

## Status

Accepted

## Context

serac talks to many external systems (EO providers, seismic services, a broker, object
storage) that are unavailable in tests and may be replaced. The brief fixes ports & adapters.

## Decision

- `src/serac/domain/`: pydantic contracts and pure logic. Imports nothing from
  `src/serac/adapters/` and no numpy/obspy/geo libraries.
- `src/serac/ports/`: abstract base classes (`ManifestLedger`, `IngestAdapter`,
  `MessageBus`, `Clock`, `DemProvider`, `HydrometricSource`, …).
- `src/serac/adapters/{eo,seismic,bus,storage,hydro,cap}/`: one concrete implementation per
  external system; only adapters import provider SDKs.
- `src/serac/pipelines/` (batch), `src/serac/streaming/` (stages), `src/serac/validation/`
  (suites) orchestrate over ports.
- `src/serac/cli.py` only assembles typer sub-apps registered by their owning modules.
- Tests mirror the layout under `tests/unit/`, `tests/integration/`, `tests/contract/`.

## Consequences

- Every external dependency has an in-memory or fixture-backed double, which is what makes
  the offline test rule (ADR-0010) achievable.
- Some duplication between ports and adapters is accepted in exchange for the import rule.
