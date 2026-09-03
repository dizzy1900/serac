# Langtang 2026 — comparison against public timings

> NOT r.avaflow: flow depths, velocities and arrival times come from serac-swe-voellmy v0.2.0, a single-phase depth-averaged Voellmy-Salm solver implemented in this repository. r.avaflow could not be obtained (see infra/docker/ravaflow/README.md); cross-validation against r.avaflow is outstanding.

**This is a comparison, not an adjustment.** The ensemble design was frozen before this
comparison ran (design hash `ce679a8f93002433a4ca8d8e4608e53208fba023ea1ac4943777f28484dae183`, solver
`0.2.0`), no parameter was changed as a result of it, and no member
was selected, weighted or removed on the basis of these numbers. `validate-runout` greps this
file for the vocabulary that would describe it otherwise.

## The public figures

The four timings below are **press-attributed** figures for an event with no peer-reviewed
source as of September 2026. In `data/events/langtang-lhende-2026.json` the corresponding
fields carry `best: null` for exactly that reason. They are quoted here as the comparison
target.

| Transect | Public timing (min after detachment) |
|---|---|
| `rasuwagadhi-gyirong` | ~7.5 |
| `syabrubesi` | ~13-14 (midpoint 13.5 used) |
| `betrawati` | ~45 |
| `galchhi` | +9 m stage in ~30 |

## Mismatch across the whole ensemble

Positive means the model arrives **later** than the public figure.

| Transect | Public | Reaching | Mismatch range (min) | Median | Closest abs |
|---|---|---|---|---|---|
| `rasuwagadhi-gyirong` | 7.5 | 45 / 230 | +7.36 to +42.03 | +14.43 | 7.36 |
| `syabrubesi` | 13.5 | 0 / 230 | not reached by any member | — | — |
| `betrawati` | 45.0 | 0 / 230 | not reached by any member | — | — |
| `galchhi` | 30.0 | 0 / 230 | not reached by any member | — | — |

## Closest member

Run `m0067-r060`, which reached 1 of the four transects
with a mean absolute mismatch of 7.36 minutes.

| Transect | Modelled arrival (min) | Mismatch (min) |
|---|---|---|
| `rasuwagadhi-gyirong` | 14.86 | +7.36 |
| `syabrubesi` | not reached | — |
| `betrawati` | not reached | — |
| `galchhi` | not reached | — |

Its parameters, for the record and for no other purpose:

```json
{
  "critical_shear_pa": 500.0,
  "entrainment_coefficient": 0.0009670823467262679,
  "ice_fraction": 0.39434782608695657,
  "mu": 0.04808186389857888,
  "release_elevation_band_m": [
    3645.6521739130435,
    4433.478260869565
  ],
  "release_volume_m3": 272019630.62978584,
  "xi_m_s2": 349.8794280098329
}
```

## What the mismatch is telling you

Three structural properties of the model bear directly on these numbers, and all three were
known before the comparison ran:

1. **The release is emplaced at rest on the corridor cells inside the release elevation band. The detachment scar, the free fall from the Langtang Lirung flank and the fragmentation that precede entry into the Lhende Khola are outside the model domain, so roughly 1,300 m of drop contributes no initial kinetic energy and modelled arrival times are biased late.** Arrival times are therefore biased late at every transect.
2. **Single-phase: the solver cannot represent two- or three-phase physics or phase separation between rock, ice and fluid. Ice melt, pore-pressure evolution and fluidisation are subsumed into the Voellmy coefficients, not resolved.** The observed cascade travelled roughly 100 km, which a
   water-dominated flood wave does readily and a Coulomb-plus-turbulent avalanche rheology does
   not: 92% of this corridor's thalweg is below 6.8 degrees, so a Voellmy `mu` above about 0.08
   cannot sustain motion there at all, whatever else is varied.
3. **30 m DEM: the Bhote Koshi gorge is under 60 m wide in places, so it spans fewer than two cells. Superelevation, run-up on valley walls and channel blocking are unresolved; damming numbers derived from deposit depth against channel geometry are order-of-magnitude indicators, not engineering estimates.**

The operator-splitting error measured in `reports/runout/verification.json` adds a further
known bias: at the production CFL the modelled terminal velocity sits about 7.6% below the
analytic Voellmy value, which makes arrivals later still.

r.avaflow cross-validation remains outstanding, so there is no independent simulator against
which to separate these structural biases from implementation error.
