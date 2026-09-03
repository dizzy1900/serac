# M2 force-history inversion — Lamplugh Glacier rock avalanche, 28 June 2016

- Target id: `lamplugh-glacier-2016` (reproduction)
- Origin: 2016-06-28T16:20:48+00:00
- Nominal source: 58.7792, -136.8883
- Config hash: `4762e79e5879b7b2559a0f7d476dddd45fc2dd869858f7a5e023535ff1a0d988`
- Wall clock: 375.8 s
- Status: **computed**

## Station geometry

9 stations / 27 channels, azimuthal gap 172 deg, distance 1.08-4.92 deg, median pre-event SNR 3.79.

| channel | distance (deg) | azimuth (deg) | peak displacement (m) | SNR |
|---|---:|---:|---:|---:|
| `AK.BESE..LHE` | 1.08 | 100 | 8.69e-06 | 2.32 |
| `AK.BESE..LHN` | 1.08 | 100 | 2.58e-05 | 3.46 |
| `AK.BESE..LHZ` | 1.08 | 100 | 5.69e-06 | 5.94 |
| `TA.P30M..LHE` | 1.35 | 358 | 1.34e-06 | 3.57 |
| `TA.P30M..LHN` | 1.35 | 358 | 7.46e-06 | 5.34 |
| `TA.P30M..LHZ` | 1.35 | 358 | 1.02e-05 | 7.11 |
| `AK.JIS..LHE` | 1.41 | 110 | 7.07e-06 | 7.82 |
| `AK.JIS..LHN` | 1.41 | 110 | 5.07e-06 | 2.47 |
| `AK.JIS..LHZ` | 1.41 | 110 | 4.10e-06 | 10.57 |
| `AK.PNL..LHE` | 1.57 | 306 | 1.05e-05 | 0.77 |
| `AK.PNL..LHN` | 1.57 | 306 | 9.59e-06 | 1.18 |
| `AK.PNL..LHZ` | 1.57 | 306 | 6.27e-06 | 9.64 |
| `TA.P32M..LHE` | 1.82 | 62 | 3.20e-06 | 3.05 |
| `TA.P32M..LHN` | 1.82 | 62 | 7.12e-06 | 2.97 |
| `TA.P32M..LHZ` | 1.82 | 62 | 6.27e-06 | 8.63 |
| `7C.MM04..LH1` | 1.89 | 38 | 5.94e-06 | 1.26 |
| `7C.MM04..LH2` | 1.89 | 38 | 5.72e-06 | 0.69 |
| `7C.MM04..LHZ` | 1.89 | 38 | 7.42e-06 | 9.75 |
| `TA.O30N..LHE` | 2.04 | 11 | 2.09e-06 | 1.53 |
| `TA.O30N..LHN` | 2.04 | 11 | 6.11e-06 | 5.07 |
| `TA.O30N..LHZ` | 2.04 | 11 | 8.17e-06 | 13.94 |
| `TA.N31M..LHE` | 2.77 | 11 | 1.79e-06 | 2.72 |
| `TA.N31M..LHN` | 2.77 | 11 | 5.17e-06 | 3.91 |
| `TA.N31M..LHZ` | 2.77 | 11 | 7.28e-06 | 13.48 |
| `TA.Q23K..LHE` | 4.92 | 282 | 4.65e-06 | 3.79 |
| `TA.Q23K..LHN` | 4.92 | 282 | 3.02e-06 | 1.41 |
| `TA.Q23K..LHZ` | 4.92 | 282 | 2.38e-06 | 11.43 |

## Result

| quantity | p05 / **p50** / p95 |
|---|---|
| Peak force | 3.32e+11 / **3.96e+11** / 4.76e+11 N |
| Impulse | 4.07e+12 / **6.41e+12** / 1.56e+13 N s |
| Duration | 219 / **282** / 297 s |
| Force azimuth | 143 / **175** / 281 deg from north |
| **Mass** | 2.21e+10 / **1.92e+11** / 1.21e+12 kg |

- Location: 58.6892, -136.8883 (depth 1.0 km, method `gsf_grid_search`, grid 2 km, resolution radius 5.7 km)
- Variance reduction: 0.614
- Azimuthal gap: 172 deg
- Regularisation: second-difference Tikhonov, order 2, zero endpoints, lambda 13.43 from the L-curve corner

### The two mass estimators

| estimator | method | p05 (kg) | p50 (kg) | p95 (kg) | a_eff basis |
|---|---|---:|---:|---:|---|
| dem_trajectory | `fmax_over_aeff` | 3.45e+11 | 5.38e+11 | 1.21e+12 | `assumed_range` |
| seismic_impulse | `impulse_over_velocity` | 2.21e+10 | 6.87e+10 | 2.13e+11 | `assumed_range` |

Consistency ratio (A/B on the medians): **7.84** — outside [1/3, 3].

The published interval is the **union** of the two, not their average.

### Modelled Green's functions

- Earth model: `prem_a_20s` via IRIS Syngine
- Band: 20-150 s at dt = 1 s
- 52 cached sets, recorded as `provenance: derived` (ADR-0016); they are modelled physics, never observations, and are never published on the bus.

### Uncertainty

200 bootstrap draws (seed 20260903) resampling stations, band_limits, lambda, source_depth, friction.

### Assumptions behind the mass

1. Published interval is the UNION of two estimators, not their average: dem_trajectory (fmax_over_aeff) gave [3.45e+11, 1.21e+12] kg and seismic_impulse (impulse_over_velocity) gave [2.21e+10, 2.13e+11] kg.
2. Consistency ratio M(dem_trajectory) / M(seismic_impulse) = 7.84 on the medians.
3. The two estimators are NOT independent: both divide by an effective acceleration built from the same Coulomb friction range. They differ in the force functional (peak versus integral) and in the source of the path geometry (terrain versus waveform), so agreement is evidence but not proof.
4. The median is the geometric mean of the two estimators' medians, which is a summary of two methods rather than a measurement.
5. The estimators disagree by more than a factor of three (ratio 7.84). This is reported, not reconciled: the union interval is correspondingly wide and the mass should be treated as order-of-magnitude only.
6. M = F_max / a_eff with a_eff = g sin(theta) (1 - phi): a rigid block on a planar slope under Coulomb friction, which ignores internal deformation, entrainment and any change of basal resistance along the path.
7. Basal friction is expressed as phi = mu / tan(theta), sampled uniformly over [0.2, 0.8]. Friction below the apparent (Heim-ratio) friction is what makes the mass accelerate at all; an absolute coefficient is not used because one larger than tan(theta) describes a mass that cannot move. The range is not calibrated to any event in this repository.
8. g = 9.81 m/s^2.
9. No DEM crop covers this runout, so the path angle is atan(H/L) from a published fall height 1620 m and runout 10500 m (esec-lamplugh). This is a weaker input than a terrain profile and AEff.basis records it as assumed_range rather than dem_trajectory.
10. M = max|integral F dt| / (a_eff * t_acc): the peak of the running impulse is the slide's peak momentum M*v_max, and v_max is taken as a_eff times the time from onset to that peak.
11. The path angle comes from the force history alone -- theta = atan(|F_vertical| / |F_horizontal|) at the instant of peak horizontal force -- so this estimator uses no DEM, no catalogue and no published geometry.
12. Basal friction as a fraction of tan(theta) sampled uniformly over [0.2, 0.8].
13. Constant a_eff through the acceleration phase, which a real slide on changing terrain does not have.
14. Onset threshold 5% of peak force; acceleration phase measured as 77 s.
15. Intervals are bootstrap percentiles over 200 draws resampling stations, band corners, lambda, source depth and friction; they are a spread over analyst choices, not a posterior.
16. Nothing here resamples the 1-D Earth model: a different model would move the answer by an amount this bootstrap cannot see.

### Notes from the run

- Force azimuth 172 deg; the mass moved towards 175 deg, which is the bearing the DEM profile follows.
- No DEM crop is committed for lamplugh-glacier-2016, so the DEM-trajectory mass estimator falls back to a published fall height and runout.

## Timings

| stage | seconds |
|---|---:|
| prepare | 0.85 |
| gridearch | 29.01 |
| final_inversion | 3.81 |
| terrain | 0.00 |
| bootstrap | 342.08 |
| **total wall clock** | **375.80** |

## Disagreement

- esec-lamplugh: "ESEC event 81 'Lamplugh Glacier main - rock avalanche': Mass 141000000000.00000, MassLow 134000000000.00000 kg; Volume 64900000.00000 m3." (1.34e+11-1.41e+11 kg)

serac's own interval is 2.21e+10 to 1.21e+12 kg (median 1.92e+11 kg).

Against published mass, esec-lamplugh (1.34e+11-1.41e+11 kg): the intervals overlap, and serac's median is 1.40 times the geometric centre of the published interval.

This section states the numeric relationship and stops there. serac's estimate rests on assumptions listed above that the published figures do not share, and no parameter was adjusted after seeing these numbers.

## Sources

- `esec-lamplugh` — EarthScope Data Products (2025) Exotic Seismic Events Catalog entry: Lamplugh Glacier main IRIS/EarthScope Exotic Seismic Events Catalog (ESEC); fetched 2026-09-03, sha256 `f4e107f36118481f...`, DOI resolved via not resolved

