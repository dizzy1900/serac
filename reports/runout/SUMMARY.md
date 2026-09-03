# M4 runout — summary of what was built and what it shows

> **NOT r.avaflow.** Every depth, velocity and arrival time below comes from
> `serac-swe-voellmy` v0.2.0, a single-phase depth-averaged
> Voellmy-Salm solver implemented in this repository. r.avaflow could not be obtained; see
> `infra/docker/ravaflow/README.md` for the acquisition record with dates and URLs.
> **Cross-validation against r.avaflow is outstanding.**

Generated 2026-09-03 from the machine-readable records in this
directory. Nothing here is retyped by hand.

## 1. Solver verification

| Case | Result |
|---|---|
| Mass conservation, closed domain | relative error 1.77e-16 |
| Lake at rest, random topography | surface deviation 0.0 m, max speed 0.0 m/s over 137 steps |

### Ritter dam break against the analytic solution

| Cells | dx (m) | L1 (m^2) | L1 relative |
|---|---|---|---|
| 100 | 10.0 | 115.0 | 10.44% |
| 200 | 5.0 | 77.3 | 7.32% |
| 400 | 2.5 | 50.8 | 4.71% |

### Voellmy terminal velocity, relative error by CFL

| CFL | Relative error |
|---|---|
| 0.4 | 7.591% |
| 0.2 | 3.506% |
| 0.1 | 1.666% |
| 0.05 | 0.807% |

The scheme applies gravity and friction as separate operators within a step, so the balance is
recovered only to first order in `dt`. At the production CFL the modelled terminal velocity sits
about 7.6% **below** the analytic value, which makes every modelled arrival time late. It is
reported here rather than removed.

## 2. Measured cost, and how the ensemble was sized

| Resolution | mu | Wall (s) | Steps | ms/step | Reach (km) | Active cells |
|---|---|---|---|---|---|---|


Cost follows `wall_s ~ k / mu` with
k = 32.5 s. The ensemble size was chosen against these
numbers and against the contention actually observed on this machine; the reasoning is written
into `ENSEMBLE_FROZEN.md` and is not repeated here.

## 3. Grid convergence

| Pair | delta  reach (m) | delta  reach (rel) | Depth profile rel. L1 | Inundation IoU at 1 m |
|---|---|---|---|---|
| 90 → 60 m | -788 | -0.051 | 0.078 | 0.935 |
| 60 → 30 m | 13 | 0.001 | 0.065 | 1.000 |


## 4. The ensemble

| | |
|---|---|
| Design hash | `ce679a8f93002433a4ca8d8e4608e53208fba023ea1ac4943777f28484dae183` |
| Members recorded | 230 |
| **Valid** | **230** |
| Flagged but retained | 230 |
| Bytes on disk | 38.6 MB (cap 3 GB) |
| Total core-seconds | 33396.5 |

Runout distance reached, over valid members:

| p5 | p25 | median | p75 | p95 | max |
|---|---|---|---|---|---|
| 11.09 | 11.87 | 13.88 | 15.28 | 24.46 | 28.72 |

(kilometres along the corridor; the corridor is 100 km long and the furthest transect is at
97.0 km.)

Flags on retained members — a flag is information, not a failure:

| Reason | Members |
|---|---|
| N negative-depth repairs | 230 |
| stopped at max_time_s | 64 |
| velocity clipped on N steps | 169 |

## 5. The surrogate

| Gate | Measured | Target | Pass |
|---|---|---|---|
| Median inundation IoU at 1 m | 0.966 | >= 0.70 | True |
| Worst per-transect arrival MAE | 46.5 s | <= 90 s | True |
| p95 inference latency | 0.0017 s | <= 2 s | True |
| 5-95% depth coverage | 0.914 | 0.85-0.95 | True |
| 5-95% arrival coverage | 0.794 | 0.85-0.95 | False |

Splits are by `run_id` and disjoint: True
({'test': 35, 'train': 161, 'val': 34}).

| Transect | Test members reaching | Arrival MAE (s) | Peak-stage rel. error |
|---|---|---|---|
| `betrawati` | 0 | — | — |
| `galchhi` | 0 | — | — |
| `rasuwagadhi-gyirong` | 3 | 46.5 | 0.081 |
| `syabrubesi` | 0 | — | — |


## 6. The finding that dominates everything above

92% of this corridor's thalweg is below 6.8 degrees, median 0.42 degrees. A Voellmy Coulomb
coefficient above roughly 0.08 cannot sustain motion over most of it — and 0.08 sits well inside
the published range for rock-ice avalanches and debris flows. A single-phase Voellmy rheology
therefore stops far short of the ~100 km the 26 August 2026 cascade is reported to have
travelled. The comparison against the public timings, and the full mismatch distribution, are in
`langtang_sanity.md`; nothing was adjusted as a result of it.
