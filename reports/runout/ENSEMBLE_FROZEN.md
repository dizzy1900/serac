# Runout ensemble — FROZEN

Frozen 2026-09-03. **Do not edit.** `validate-runout` recomputes
the design hash and the solver version and fails if either has moved, so an edit here
invalidates the ensemble rather than quietly redefining it.

| Field | Value |
|---|---|
| Solver | `serac-swe-voellmy` |
| `SOLVER_VERSION` | `0.2.0` |
| Design hash | `ce679a8f93002433a4ca8d8e4608e53208fba023ea1ac4943777f28484dae183` |
| Members | 230 |
| Latin-hypercube seed | 20260903 |

## Resolution blocks

| Resolution | Members |
|---|---|
| 60 m | 222 |
| 30 m | 8 |

## Sampled dimensions

| Parameter | Low | High | Sampling |
|---|---|---|---|
| `release_volume_m3` | 5e+06 | 3e+08 | log-uniform |
| `ice_fraction` | 0.2 | 0.8 | uniform |
| `release_band_base_m` | 3600 | 4600 | uniform |
| `release_band_width_m` | 400 | 1200 | uniform |
| `entrainment_coefficient` | 0.0001 | 0.03 | log-uniform |
| `mu` | 0.02 | 0.3 | log-uniform |
| `xi_m_s2` | 200 | 3000 | log-uniform |

`critical_shear_pa` is held at 500 Pa.

## Solver settings

```json
{
  "cfl": 0.45,
  "dry_depth_m": 0.02,
  "max_time_s": 3600.0,
  "output_interval_s": 60.0,
  "stop_kinetic_fraction": 0.001
}
```

## Notes

Sized against MEASURED per-member cost on the machine this ran on, and re-frozen once for a solver fix.

SOLVER FIX. The first freeze ran against serac-swe-voellmy v0.1.0, whose kinetic stopping criterion could never trigger: it compared against the first non-zero kinetic energy, and a release emplaced at rest has essentially none after one step. Every one of the first six members ran to the simulated-time limit, which made runout distance a statement about the compute budget rather than about the rheology. v0.2.0 measures against the peak instead. Re-measured at 60 m with the fix: mu=0.217 stops itself at 2698 s (10.83 km, 58.9 s of wall clock), mu=0.091 at 1262 s (13.91 km, 85.0 s), mu=0.023 runs to the 7200 s probe limit (21.19 km, 229.7 s). The version bump invalidates the earlier ensemble by design; mixing pre-fix and post-fix members would mix physics.

COST. reports/runout/timing.json: a 30 m member costs 1077.6 s at mu=0.03 and 326.8 s at mu=0.10, so cost ~ 32.5/mu s; 30 m is 9-11x the cost of 60 m. This machine is shared with sibling agents and another project, and 9 workers were measured holding about 35% of a core each (load average 26-39 on 10 cores).

RESOLUTION SPLIT. 200 members at 30 m would take 3.1 h uncontended and about 6.8 h at the observed contention. The brief's stated fallback is fewer at 30 m plus more at 60 m with the resolution effect quantified, so this design is 222 members at 60 m and 8 at 30 m. The resolution effect is quantified by the three-point study in reports/runout/grid_convergence.json (90 / 60 / 30 m at one parameter vector) rather than by the ensemble, because 8 members is too few to carry it.

BLOCK ORDER. The 60 m block runs first, so that stopping the driver early still leaves an ensemble above the 200-valid floor rather than a partial 30 m block and nothing else.

SIMULATED-TIME LIMIT. 3600 s. Every public timing this ensemble is compared against is at most 45 min, and the re-measured runs above show that members above mu ~ 0.09 stop themselves well inside it. Members still in motion at the cap are flagged hit_time_limit and retained.

---

> **NOT r.avaflow: flow depths, velocities and arrival times come from serac-swe-voellmy v0.2.0, a single-phase depth-averaged Voellmy-Salm solver implemented in this repository. r.avaflow could not be obtained (see infra/docker/ravaflow/README.md); cross-validation against r.avaflow is outstanding.**

> Single-phase: the solver cannot represent two- or three-phase physics or phase separation between rock, ice and fluid. Ice melt, pore-pressure evolution and fluidisation are subsumed into the Voellmy coefficients, not resolved.

> 30 m DEM: the Bhote Koshi gorge is under 60 m wide in places, so it spans fewer than two cells. Superelevation, run-up on valley walls and channel blocking are unresolved; damming numbers derived from deposit depth against channel geometry are order-of-magnitude indicators, not engineering estimates.
