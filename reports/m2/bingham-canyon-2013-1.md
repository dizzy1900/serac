# M2 force-history inversion — Bingham Canyon Mine rock avalanche 1, 11 April 2013

- Target id: `bingham-canyon-2013-1` (reproduction)
- Origin: 2013-04-11T03:30:24+00:00
- Nominal source: 40.5319, -112.1488
- Config hash: `4762e79e5879b7b2559a0f7d476dddd45fc2dd869858f7a5e023535ff1a0d988`
- Wall clock: 417.3 s
- Status: **computed**

## Station geometry

9 stations / 27 channels, azimuthal gap 64 deg, distance 0.61-5.04 deg, median pre-event SNR 2.12.

| channel | distance (deg) | azimuth (deg) | peak displacement (m) | SNR |
|---|---:|---:|---:|---:|
| `US.DUG.00.LH1` | 0.61 | 237 | 6.62e-06 | 2.80 |
| `US.DUG.00.LH2` | 0.61 | 237 | 4.19e-06 | 3.47 |
| `US.DUG.00.LHZ` | 0.61 | 237 | 5.70e-06 | 3.08 |
| `US.HWUT.00.LHE` | 1.16 | 22 | 3.36e-06 | 1.61 |
| `US.HWUT.00.LHN` | 1.16 | 22 | 2.20e-06 | 4.82 |
| `US.HWUT.00.LHZ` | 1.16 | 22 | 2.35e-06 | 1.52 |
| `US.ELK.00.LH1` | 2.36 | 276 | 2.10e-06 | 1.16 |
| `US.ELK.00.LH2` | 2.36 | 276 | 1.96e-06 | 6.13 |
| `US.ELK.00.LHZ` | 2.36 | 276 | 1.38e-06 | 6.98 |
| `US.BW06.00.LHE` | 2.96 | 40 | 3.92e-06 | 1.97 |
| `US.BW06.00.LHN` | 2.96 | 40 | 6.68e-06 | 5.96 |
| `US.BW06.00.LHZ` | 2.96 | 40 | 1.34e-06 | 1.90 |
| `TA.O20A..LHE` | 3.01 | 96 | 2.15e-06 | 1.66 |
| `TA.O20A..LHN` | 3.01 | 96 | 2.44e-06 | 1.84 |
| `TA.O20A..LHZ` | 3.01 | 96 | 1.82e-06 | 9.01 |
| `XT.D01..LHE` | 3.32 | 340 | 2.85e-06 | 2.12 |
| `XT.D01..LHN` | 3.32 | 340 | 3.21e-06 | 1.59 |
| `XT.D01..LHZ` | 3.32 | 340 | 1.42e-06 | 8.65 |
| `XT.D05..LHE` | 3.97 | 324 | 4.81e-06 | 1.19 |
| `XT.D05..LHN` | 3.97 | 324 | 7.27e-06 | 1.80 |
| `XT.D05..LHZ` | 3.97 | 324 | 1.19e-06 | 3.20 |
| `US.MVCO.00.LH1` | 4.38 | 138 | 4.20e-06 | 1.31 |
| `US.MVCO.00.LH2` | 4.38 | 138 | 1.81e-05 | 3.01 |
| `US.MVCO.00.LHZ` | 4.38 | 138 | 1.93e-06 | 3.58 |
| `US.WUAZ.00.LH1` | 5.04 | 173 | 2.03e-06 | 1.03 |
| `US.WUAZ.00.LH2` | 5.04 | 173 | 2.50e-06 | 1.21 |
| `US.WUAZ.00.LHZ` | 5.04 | 173 | 2.15e-06 | 11.41 |

## Result

| quantity | p05 / **p50** / p95 |
|---|---|
| Peak force | 1.37e+11 / **1.65e+11** / 2.44e+11 N |
| Impulse | 1.45e+12 / **2.45e+12** / 1.08e+13 N s |
| Duration | 173 / **279** / 295 s |
| Force azimuth | 99.1 / **196** / 346 deg from north |
| **Mass** | 1.57e+10 / **6.57e+10** / 2.72e+11 kg |

- Location: 40.5319, -112.2198 (depth 1.0 km, method `gsf_grid_search`, grid 2 km, resolution radius 14.4 km)
- Variance reduction: 0.425
- Azimuthal gap: 64 deg
- Regularisation: second-difference Tikhonov, order 2, zero endpoints, lambda 55.41 from the L-curve corner

### The two mass estimators

| estimator | method | p05 (kg) | p50 (kg) | p95 (kg) | a_eff basis |
|---|---|---:|---:|---:|---|
| dem_trajectory | `fmax_over_aeff` | 7.54e+10 | 1.23e+11 | 2.72e+11 | `assumed_range` |
| seismic_impulse | `impulse_over_velocity` | 1.57e+10 | 3.51e+10 | 8.74e+10 | `assumed_range` |

Consistency ratio (A/B on the medians): **3.50** — outside [1/3, 3].

The published interval is the **union** of the two, not their average.

### Modelled Green's functions

- Earth model: `prem_a_20s` via IRIS Syngine
- Band: 20-150 s at dt = 1 s
- 64 cached sets, recorded as `provenance: derived` (ADR-0016); they are modelled physics, never observations, and are never published on the bus.

### Uncertainty

200 bootstrap draws (seed 20260903) resampling stations, band_limits, lambda, source_depth, friction.

### Assumptions behind the mass

1. Published interval is the UNION of two estimators, not their average: dem_trajectory (fmax_over_aeff) gave [7.54e+10, 2.72e+11] kg and seismic_impulse (impulse_over_velocity) gave [1.57e+10, 8.74e+10] kg.
2. Consistency ratio M(dem_trajectory) / M(seismic_impulse) = 3.50 on the medians.
3. The two estimators are NOT independent: both divide by an effective acceleration built from the same Coulomb friction range. They differ in the force functional (peak versus integral) and in the source of the path geometry (terrain versus waveform), so agreement is evidence but not proof.
4. The median is the geometric mean of the two estimators' medians, which is a summary of two methods rather than a measurement.
5. The estimators disagree by more than a factor of three (ratio 3.50). This is reported, not reconciled: the union interval is correspondingly wide and the mass should be treated as order-of-magnitude only.
6. M = F_max / a_eff with a_eff = g sin(theta) (1 - phi): a rigid block on a planar slope under Coulomb friction, which ignores internal deformation, entrainment and any change of basal resistance along the path.
7. Basal friction is expressed as phi = mu / tan(theta), sampled uniformly over [0.2, 0.8]. Friction below the apparent (Heim-ratio) friction is what makes the mass accelerate at all; an absolute coefficient is not used because one larger than tan(theta) describes a mass that cannot move. The range is not calibrated to any event in this repository.
8. g = 9.81 m/s^2.
9. No DEM crop covers this runout, so the path angle is atan(H/L) from a published fall height 850 m and runout 2950 m (esec-bingham-1). This is a weaker input than a terrain profile and AEff.basis records it as assumed_range rather than dem_trajectory.
10. M = max|integral F dt| / (a_eff * t_acc): the peak of the running impulse is the slide's peak momentum M*v_max, and v_max is taken as a_eff times the time from onset to that peak.
11. The path angle comes from the force history alone -- theta = atan(|F_vertical| / |F_horizontal|) at the instant of peak horizontal force -- so this estimator uses no DEM, no catalogue and no published geometry.
12. Basal friction as a fraction of tan(theta) sampled uniformly over [0.2, 0.8].
13. Constant a_eff through the acceleration phase, which a real slide on changing terrain does not have.
14. Onset threshold 5% of peak force; acceleration phase measured as 101 s.
15. Intervals are bootstrap percentiles over 200 draws resampling stations, band corners, lambda, source depth and friction; they are a spread over analyst choices, not a posterior.
16. Nothing here resamples the 1-D Earth model: a different model would move the answer by an amount this bootstrap cannot see.

### Notes from the run

- Force azimuth 188 deg; the mass moved towards 107 deg, which is the bearing the DEM profile follows.
- No DEM crop is committed for bingham-canyon-2013-1, so the DEM-trajectory mass estimator falls back to a published fall height and runout.

## Timings

| stage | seconds |
|---|---:|
| prepare | 5.61 |
| gridearch | 39.09 |
| final_inversion | 4.65 |
| terrain | 0.00 |
| bootstrap | 367.88 |
| **total wall clock** | **417.31** |

## Disagreement

- esec-bingham-1: "ESEC event 20 'Bingham Canyon Mine 1 - rock avalanche': Mass 70000000000.00000, MassLow 56000000000.00000, MassHigh 84000000000.00000 kg; Volume 30000000.00000 m3; DOI 10.17611/DP/14768841." (5.6e+10-8.4e+10 kg)

serac's own interval is 1.57e+10 to 2.72e+11 kg (median 6.57e+10 kg).

Against published mass, esec-bingham-1 (5.6e+10-8.4e+10 kg): the intervals overlap, and serac's median is 0.96 times the geometric centre of the published interval.

This section states the numeric relationship and stops there. serac's estimate rests on assumptions listed above that the published figures do not share, and no parameter was adjusted after seeing these numbers.

## Sources

- `esec-bingham-1` — EarthScope Data Products (2025) Exotic Seismic Events Catalog entry: Bingham Canyon Mine 1 - rock avalanche IRIS/EarthScope Exotic Seismic Events Catalog (ESEC) doi:10.17611/DP/14768841; fetched 2026-09-03, sha256 `f4e107f36118481f...`, DOI resolved via datacite

