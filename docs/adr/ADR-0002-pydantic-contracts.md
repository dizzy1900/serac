# ADR-0002: pydantic v2 contracts, `Range.source_refs` as a list, `FieldNote` for nulls

Date: 2026-09-03

## Status

Accepted

## Context

Every domain object (event records, AOIs, traces, forecasts, CAP messages, the avoided-loss
request/response) is a published contract. The failure mode of this domain is fabricated
precision: a plausible number with no source. The brief's `Range(low, high, best|null,
source_ref)` has a singular reference, but in practice a range's `low` and `high` often come
from different papers.

## Decision

- All contracts are pydantic v2 models (`extra="forbid"`), one module per family under
  `src/serac/domain/`, each exposing a `CONTRACTS` registry and a contract version constant.
  `serac schema export` writes `contracts/<name>.v<major>.json`; a test in `tests/contract/`
  fails on drift.
- `Range` = `{low, high, best|null, unit, source_refs: list[str] (min 1), disputed: bool,
  estimates: list[AttributedEstimate], notes}`. `source_refs` is a **list**, deliberately
  departing from the brief's singular `source_ref`. Validators: `low <= high`; `best` inside
  the bounds; `best` non-null only if some referenced source is `peer_reviewed`,
  `usgs_comcat`, `agency_official` or `dataset`; `disputed=True` requires `best=None`, at
  least two attributed estimates and `notes`.
- `SourceRef` requires `accessed_utc` and the `sha256` of the bytes actually retrieved; a
  DOI only when it resolved (see the citation rule in `CLAUDE.md`).
- `FieldNote` = `{reason ∈ {no_peer_reviewed_estimate, not_applicable, not_yet_researched,
  disputed_beyond_range, not_public}, public_estimates, notes}`. Every `Range | None` field
  that is `None` must have a `field_notes[<field>]` entry. "Unknowns are null, not guesses"
  is thereby enforced by the model, not by convention.

## Consequences

- Press-only figures can never become a `best`; they live in `estimates` or in a
  `FieldNote.public_estimates` list, each attributed.
- Records are verbose. That is the point.
- Consumers read JSON Schema, not Python; the pydantic classes are an implementation detail.
