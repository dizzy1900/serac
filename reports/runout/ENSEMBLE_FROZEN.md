# Runout ensemble — FROZEN

Frozen 2026-09-03. **Do not edit.** `validate-runout` recomputes
the design hash and the solver version and fails if either has moved, so an edit here
invalidates the ensemble rather than quietly redefining it.

| Field | Value |
|---|---|
| Solver | `serac-swe-voellmy` |
| `SOLVER_VERSION` | `0.1.0` |
| Design hash | `84821dbcaaaf793cdf386f5fe61ae0f58c0c50567f140a93f22a51ee6f4a75c0` |
| Members | 230 |
| Latin-hypercube seed | 20260903 |

## Resolution blocks

| Resolution | Members |
|---|---|
| 30 m | 30 |
| 60 m | 200 |

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

Sized against MEASURED cost on the machine this actually ran on.

MEASUREMENTS (reports/runout/timing.json): one 30 m member costs 1077.6 s at mu=0.03 and 326.8 s at mu=0.10, so cost ~ k/mu with k = 32.5 s; a 60 m member costs 120.4 s and 29.8 s for the same two, i.e. 30 m is 9-11x the cost of 60 m. Over the log-uniform mu range that gives a 560 s mean per 30 m member at a 7200 s simulated-time limit and about 330 s at the 3600 s limit used here. The first two members actually run took 290 s and 376 s, confirming it.

CONTENTION: this machine is shared with sibling agents and another project. Measured while the ensemble was running, each of 9 workers held about 35% of a core (load average 26 on 10 cores), so a 30 m member costs about 950 s of wall clock rather than 330 s.

DESIGN CHOICE: 200 members at 30 m would take 3.1 h uncontended and about 6.8 h at the observed contention. The brief's stated fallback is fewer at 30 m plus more at 60 m with the resolution effect quantified, so this design is 30 members at 30 m and 200 at 60 m: about 1.5 h at the observed contention, clearing the 200-valid floor. The resolution effect is quantified twice over: by the 3-point study in reports/runout/grid_convergence.json, and inside the ensemble by comparing the 30 m block against the 60 m block at matched parameters.

RE-SIZED ONCE. An earlier freeze of the same 230 members used 130 at 30 m and 100 at 60 m; it was replaced after measuring the contended throughput and before any member output was analysed. The Latin hypercube is unchanged (same seed, same member count, same bounds), so every member's parameter vector is identical between the two; only the resolution block boundary moved. The two members already run keep their identity and are reused from cache.

---

> **NOT r.avaflow: flow depths, velocities and arrival times come from serac-swe-voellmy v0.1.0, a single-phase depth-averaged Voellmy-Salm solver implemented in this repository. r.avaflow could not be obtained (see infra/docker/ravaflow/README.md); cross-validation against r.avaflow is outstanding.**

> Single-phase: the solver cannot represent two- or three-phase physics or phase separation between rock, ice and fluid. Ice melt, pore-pressure evolution and fluidisation are subsumed into the Voellmy coefficients, not resolved.

> 30 m DEM: the Bhote Koshi gorge is under 60 m wide in places, so it spans fewer than two cells. Superelevation, run-up on valley walls and channel blocking are unresolved; damming numbers derived from deposit depth against channel geometry are order-of-magnitude indicators, not engineering estimates.
