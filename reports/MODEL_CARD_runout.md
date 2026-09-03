# Model card — M4 runout: `serac-swe-voellmy` v0 and its neural surrogate

> **NOT r.avaflow.** Flow depths, velocities and arrival times come from `serac-swe-voellmy`
> v0.1.0, a single-phase depth-averaged Voellmy-Salm shallow-water solver implemented in this
> repository. r.avaflow could not be obtained — no official GRASS addon (404), no canonical
> public repository, and avaflow.org's download behind a registration wall. The acquisition
> attempt is recorded with dates and URLs in `infra/docker/ravaflow/README.md`.
> **Cross-validation against r.avaflow is outstanding.** There is no independent simulator
> against which to separate this model's structural biases from implementation error.

| | |
|---|---|
| Component | M4 — L3 cascade runout |
| Simulator | `serac-swe-voellmy` v0.1.0 (`src/serac/models/runout/solver.py`) |
| Surrogate | 1-D corridor FNO with 5/50/95 quantile heads + transect regressor, v0.1.0 |
| AOI | `lhende-khola-trishuli` (Langtang Lirung → Lhende Khola → Bhote Koshi → Trishuli, 100 km) |
| Terrain | Copernicus GLO-30, reprojected to EPSG:32645 at 30 m |
| Contract | `CascadeForecast` (`contracts/cascade-forecast.v0.json`) |

## Intended use

Given a source location, mass, rock/ice fraction and friction parameters, produce a
**probabilistic** estimate of downstream flow depth along the corridor, arrival time and peak
stage at the committed transects, and a v0 indicator of channel damming — fast enough
(≤ 2 s) to sit inside an alerting lane. Every output is a `Range`, and the interval is the
model's own 5th-to-95th percentile spread over the frozen ensemble.

## Out-of-scope use

* **Not an engineering design tool.** Nothing here sizes a structure, a spillway or a levee.
* **Not a damming prediction.** The damming index is a dimensionless deposit-to-channel-depth
  ratio mapped through a stated, uncalibrated logistic. It is not a probability estimated from
  an inventory, because no inventory of landslide dams exists for this corridor.
* **Not a substitute for r.avaflow or any validated multi-phase code.**
* **Not transferable off this corridor** without rebuilding the terrain and re-running the
  ensemble: the surrogate's static features are this corridor's bed profile.

## The solver

Conservative depth-averaged shallow-water equations on the map-space 30 m grid, with a
Voellmy-Salm basal resistance (`mu` Coulomb + `xi` turbulent) and a parametric bed-entrainment
closure. Governing equations, scheme, CFL condition and boundary handling are in the module
docstring of `src/serac/models/runout/solver.py`.

Three numerical choices were made against measurements and are worth stating, because each
replaced an approach that looked more standard on paper:

1. **The solver runs in map space, not in a corridor ribbon.** A `(s, n)` ribbon would have been
   about 14x cheaper. The committed OSM centreline's minimum radius of curvature is **750 m even
   after a 900 m Gaussian smooth** (which by then has moved the line up to 574 m off the real
   channel), so an orthogonal ribbon of useful width folds.
2. **Gravity and pressure enter as a slope-aligned surface-gradient source**
   `-g cos(theta) h grad(h+b)`, not as map-space hydrostatic reconstruction. Audusse
   reconstruction under-drove a 20 degree slope **by a factor of 7**, because at 20 m cells the
   bed drops 7.3 m against a 2 m flow depth and the reconstruction clips the driving force at
   `0.5 g h^2`.
3. **A conservative positivity limiter** scales each donor cell's outgoing fluxes. Without it,
   four faces could each drain ~0.8 h in a step and the negative-depth repair fabricated **32%
   of the mass** on the entrainment test. Fabricated volume is now 3.7e-13 m³ and is accounted
   as a *source* in the mass balance rather than hidden in the residual.

### Verification — measured, not asserted

| Test | Result |
|---|---|
| Mass conservation, closed domain | relative error 1.8e-16 |
| Lake at rest over random topography | surface deviation **0.0 m**, max speed **0.0 m/s**, over 137 steps |
| Lake at rest with dry banks | dry cells stay dry to < 1e-10 m |
| Ritter dam break, n=100 / 200 / 400 | L1 relative error 10.44% / 7.32% / 4.72% |
| Ritter under refinement | L1 falls monotonically: 115.0 → 77.3 → 50.8 m² |
| Voellmy terminal velocity | converges **first order in dt**: 7.591% low at CFL 0.4, 3.506% at 0.2, 1.666% at 0.1, 0.807% at 0.05 |
| Entrainment | conserves mass exactly; cannot over-draw the bed; costs momentum |
| Mask walls | no leakage; volume conserved to 1e-12 |
| Outflow | mass balance closes to < 1e-10 |

**Known bias:** at the production CFL of 0.45 the modelled terminal velocity sits about **7.6%
below** the analytic Voellmy value, because gravity and friction are applied as separate
operators within a step. That is a first-order splitting error, it halves whenever the step
does, and it makes every modelled arrival time *late*. It was not tuned away.

## What this model cannot represent

* **Two- and three-phase physics, and phase separation between rock, ice and fluid.** The solver
  is single-phase. Ice fraction enters only through the mixture density; it cannot produce a
  fluid-rich front running ahead of a solid-rich body, which is the mechanism behind the long
  fast runout of real rock-ice cascades. Ice melt, pore-pressure evolution and fluidisation are
  subsumed into the Voellmy coefficients, not resolved.
* **The sub-60 m gorge.** The Bhote Koshi gorge is under 60 m wide in places, so it spans fewer
  than two cells at 30 m. Superelevation, run-up on valley walls and channel blocking are
  unresolved. This is stated again next to the damming numbers, not only here.
* **The detachment and the fall.** The release is emplaced *at rest* at the head of the
  committed centreline. The Langtang source zone sits about 4.5 km east of it, roughly 1,300 m
  higher; that fall contributes no initial kinetic energy, so arrival times are biased late.
* **Momentum across shocks.** The surface-gradient form is not conservative in momentum at a
  shock. The dry-bed Ritter solution has no shock, so the verification does not probe it; a
  wet-bed bore's speed would be slightly wrong.

## Data and provenance

| Layer | Source | Provenance |
|---|---|---|
| Corridor DEM | Copernicus GLO-30 public COGs, windowed read | `real`, ledgered, sha256 `c00e9032…` |
| Corridor geometry, transects | committed AOI, OSM Overpass (ODbL) | `real` |
| Conditioned elevation, `domain_mask` | priority-flood fill seeded from the outflow edge | `derived` |
| `erodible_depth` | **a parametric mantle, not a measurement** — 5 m maximum, tapered above 35° slope and by a 150 m Gaussian in cross-channel offset. No sediment-thickness survey exists for this corridor. | `derived` (assumption) |
| Ensemble outputs | `simulation_output` | `derived`, one ledger row per artifact |

Nothing synthetic is written under `data/`.

## The corridor frame, and where it does not work

The chainage/offset frame round-trips **exactly** (1.6e-11 px) on every cell whose closest-point
projection falls inside a segment, and is wrong by up to 71 px on every cell whose projection
snaps to a *vertex*. The second set is 61% of the 1.5 km buffer mask at 30 m: it is the buffer's
rounded end-caps and the outer side of every bend, beyond the curve's medial axis, where the map
`(x, y) → (s, n)` is genuinely many-to-one and **no** forward map can invert it.
`CorridorTerrain.frame_valid` publishes that set rather than averaging the failure in. Chainage
binning — all the surrogate and the reports use the frame for — needs only `s` and is unaffected.

## Failure modes

* A member whose friction exceeds the local driving force **stops**, and on this corridor that
  is the common case rather than the exception (see below). A stopped member is valid data.
* The 30 m DEM's spurious sills are removed by depression filling, which also removes real
  closed basins; at 30 m the two are not separable. Filling leaves flat reaches on which a
  Coulomb rheology cannot move.
* The surrogate is trained on one corridor, one solver and one frozen design. It has no
  out-of-corridor validity and its intervals do not include structural error in the solver, the
  DEM or the corridor geometry — only its own spread over the ensemble.

## The result that matters most

**92% of this corridor's thalweg is below 6.8 degrees** (median 0.42 degrees). A Voellmy Coulomb
coefficient above roughly 0.08 therefore cannot sustain motion over most of it, whatever else is
varied — and 0.08 is well inside the published range for rock-ice avalanches and debris flows.
A single-phase Voellmy rheology stops far short of the ~100 km the 26 August 2026 cascade is
reported to have travelled. That is a finding about the rheology, reported in
`reports/runout/langtang_sanity.md` as a comparison against public timings and **not** used to
adjust anything.
