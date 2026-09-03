# ADR-0016 — Modelled data is `derived`, not `synthetic`

- Status: accepted
- Date: 2026-09-03

## Context

Prompt 2's force-history inversion needs Green's functions: the ground displacement a unit
force would produce, computed from a published 1-D Earth model by IRIS Syngine. They are not
observations. They are also not the thing `provenance: synthetic` was invented to catch.

`ManifestEntry` rejects `provenance: synthetic` for any path outside `tests/fixtures/synthetic/`,
which is the rule that stops fabricated observations being passed off as real (Prompt 1
non-negotiable 1). Green's functions must live under `data/interim/` because they are a cache
shared across grid nodes and events, so that rule would either block them or force the
synthetic label somewhere it does not belong.

## Decision

serac distinguishes two kinds of not-observed data:

- **`synthetic`** — a fabricated stand-in for an observation serac could not obtain. It is a
  placeholder, it carries no information about the world, and it may only live under
  `tests/fixtures/synthetic/`. The HyP3 coherence pair used by the feature cube is an example.
- **`derived`** — computed from stated inputs by a reproducible procedure: a reprojection, a
  feature cube, a simulation output, or physics evaluated from a published Earth model. It
  carries real information, it can be regenerated from its inputs, and it may live under
  `data/`.

Green's functions are recorded as `provenance: derived`, `source: iris_syngine`, with
`params.modelled = true`, the Earth model name and the provider URL. `GreensSet.modelled` is
`Literal[True]` so the fact cannot be dropped in transit.

Two consequences follow, and both are enforced rather than documented:

1. **Green's functions are never published on the bus.** As a `SeismicTrace` they would have
   to claim `TraceSource.synthetic`, and a consumer could not tell a modelled trace from a
   recording. They stay inside `serac.models.lfh`.
2. **Runout simulator output is `derived` too**, with `source: simulation_output`, because it
   is likewise reproducible from a stated parameter vector and a DEM.

## Consequences

- The ledger keeps its strong rule: nothing that stands in for an observation escapes
  `tests/fixtures/synthetic/`.
- A reader of `data/manifest.jsonl` can tell modelled physics from fabricated placeholders,
  which was not possible when both would have been called synthetic.
- `validate-ingest` must not treat `derived` rows as observations; they are re-hashed like any
  other stored file, but they are excluded from counts of "real data serac holds".
