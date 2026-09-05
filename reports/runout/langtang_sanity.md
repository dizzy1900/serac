# Langtang 2026 — comparison against the recorded transect arrivals

> NOT r.avaflow: flow depths, velocities and arrival times come from serac-swe-voellmy v0.2.0, a single-phase depth-averaged Voellmy-Salm solver implemented in this repository. r.avaflow could not be obtained (see infra/docker/ravaflow/README.md); cross-validation against r.avaflow is outstanding.

**This is a comparison, not an adjustment.** The ensemble design was frozen before this
comparison ran (design hash `ce679a8f93002433a4ca8d8e4608e53208fba023ea1ac4943777f28484dae183`, solver
`0.2.0`), no parameter was changed as a result of it, and no member
was selected, weighted or removed on the basis of these numbers. `validate-runout` greps this
file for the vocabulary that would describe it otherwise.

## What the event record holds

Every observed figure below is read from `data/events/langtang-lhende-2026.json` and none is written
in the code that produced this file. The record holds an arrival time for
**1 of 4 transects**, and that is what the comparison
compares against. Modelled arrivals come from modelled_arrival_min recorded in reports/runout/langtang_sanity.json (the per-member rasters were not on disk; no modelled number was recomputed).

| Transect | Recorded arrival (min after detachment) | `best` | Sources |
|---|---|---|---|
| `syabrubesi` | 13 | null | `kp-2026-09-02-alert` |

`best` is `null` for every one of them: the record carries the figure and its sources without asserting a preferred value, because no source qualifies to set one. The comparison uses the recorded `low`-`high` interval, not a midpoint of it.

### Transects with no recorded arrival time

These are **not** comparison targets. The event library examined the figures that circulate for
them and declined to record them; the reason below is the record's own sentence. Quoting one of
these numbers here as a target would be asserting a provenance the record refuses.

| Transect | What the record does hold | Why there is no arrival time |
|---|---|---|
| `rasuwagadhi-gyirong` | — | USGS: the Rasuwagadhi border post was impacted; Kathmandu Post (27 Aug 2026): 'The Bhotekoshi rose with extraordinary speed at the Rasuwagadhi border crossing on Wednesday morning'. An arrival time of about 7.5 minutes after the ComCat origin circulates publicly without attribution; no retrieved source states it, so arrival_time_min is null. |
| `betrawati` | — | Kathmandu Post (27 Aug 2026): 'Bridges and other infrastructure between Rasuwa and Dhading were swept away'. An arrival time of about 45 minutes after the origin circulates publicly without attribution; no retrieved source states it, so arrival_time_min is null. |
| `galchhi` | stage_rise_m 9 m (sources: icimod-2026-08-26-press-release, kp-2026-08-27-what-happened; best null) | Stage rise of up to 9 m within 30 minutes at the DHM Galchhi station (ICIMOD; Kathmandu Post); Malekhu downstream rose about 7 m over a similar period. The arrival clock time at Galchhi was not read, so arrival_time_min is null. |

## Modelled arrivals across the whole ensemble

Solver output over 230 members. No observation enters this table.

| Transect | Reaching | Modelled arrival range (min) | Median | Compared |
|---|---|---|---|---|
| `betrawati` | 0 / 230 | not reached by any member | — | no (no recorded arrival) |
| `galchhi` | 0 / 230 | not reached by any member | — | no (no recorded arrival) |
| `rasuwagadhi-gyirong` | 45 / 230 | 14.86 to 49.53 | 21.93 | no (no recorded arrival) |
| `syabrubesi` | 0 / 230 | not reached by any member | — | yes |

## Mismatch against the recorded arrivals

Positive means the model arrives **later** than the recorded interval; a modelled arrival inside
the recorded interval scores 0. Only the transects above with a recorded arrival appear here.

| Transect | Recorded | Reaching | Mismatch range (min) | Median | Closest abs |
|---|---|---|---|---|---|
| `syabrubesi` | 13 | 0 / 230 | not reached by any member | — | — |

## Closest member

**No member reached a transect the event record holds an arrival time for** (`syabrubesi`), so there is no closest member and no mismatch to report. A member that reaches a transect with no recorded arrival has produced nothing to compare: the modelled arrivals for those transects are in the table above, and they are the model's own output, not a match to an observation.

## What the mismatch is telling you

Three structural properties of the model bear directly on these numbers, and all three were
known before the comparison ran:

1. **The release is emplaced at rest on the corridor cells inside the release elevation band. The detachment scar, the free fall from the Langtang Lirung flank and the fragmentation that precede entry into the Lhende Khola are outside the model domain, so roughly 1,300 m of drop contributes no initial kinetic energy and modelled arrival times are biased late.** Arrival times are therefore biased late at every transect.
2. **Single-phase: the solver cannot represent two- or three-phase physics or phase separation between rock, ice and fluid. Ice melt, pore-pressure evolution and fluidisation are subsumed into the Voellmy coefficients, not resolved.** The observed cascade travelled roughly 100 km, which a
   water-dominated flood wave does readily and a Coulomb-plus-turbulent avalanche rheology does
   not: 87.4% of this corridor's thalweg is below 4.57 degrees (median 0.42 degrees, 499 binned segments at 30 m), so a Voellmy Coulomb coefficient above 0.08 cannot sustain motion over most of it, whatever else is varied (measured, reports/runout/terrain.json).
3. **30 m DEM: the Bhote Koshi gorge is under 60 m wide in places, so it spans fewer than two cells. Superelevation, run-up on valley walls and channel blocking are unresolved; damming numbers derived from deposit depth against channel geometry are order-of-magnitude indicators, not engineering estimates.**

The operator-splitting error measured in `reports/runout/verification.json` adds a further
known bias: at the production CFL of 0.45 the modelled terminal velocity sits
8.7% below the analytic Voellmy value, which makes arrivals later still.

r.avaflow cross-validation remains outstanding, so there is no independent simulator against
which to separate these structural biases from implementation error.
