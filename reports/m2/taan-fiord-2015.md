# M2 force-history inversion — Taan Fiord (Tyndall Glacier) rock avalanche, 17 October 2015

- Target id: `taan-fiord-2015` (reproduction)
- Origin: 2015-10-18T05:18:36+00:00
- Nominal source: 60.1737, -141.1822
- Config hash: `4762e79e5879b7b2559a0f7d476dddd45fc2dd869858f7a5e023535ff1a0d988`
- Wall clock: 384.5 s
- Status: **computed**

## Station geometry

9 stations / 27 channels, azimuthal gap 129 deg, distance 0.55-7.73 deg, median pre-event SNR 1.82.

| channel | distance (deg) | azimuth (deg) | peak displacement (m) | SNR |
|---|---:|---:|---:|---:|
| `AK.BAGL..LHE` | 0.55 | 305 | 1.10e-05 | 1.77 |
| `AK.BAGL..LHN` | 0.55 | 305 | 7.24e-06 | 0.88 |
| `AK.BAGL..LHZ` | 0.55 | 305 | 2.41e-06 | 1.99 |
| `AK.GRNC..LHE` | 0.63 | 333 | 6.86e-06 | 1.01 |
| `AK.GRNC..LHN` | 0.63 | 333 | 8.31e-06 | 1.32 |
| `AK.GRNC..LHZ` | 0.63 | 333 | 1.46e-06 | 3.25 |
| `AK.LOGN..LHE` | 0.66 | 8 | 7.62e-06 | 3.98 |
| `AK.LOGN..LHN` | 0.66 | 8 | 3.63e-06 | 2.35 |
| `AK.LOGN..LHZ` | 0.66 | 8 | 2.01e-06 | 4.03 |
| `AK.CYK..LHE` | 0.66 | 263 | 9.37e-06 | 0.83 |
| `AK.CYK..LHN` | 0.66 | 263 | 7.86e-06 | 1.39 |
| `AK.CYK..LHZ` | 0.66 | 263 | 2.47e-06 | 3.45 |
| `AK.BCP..LHE` | 0.81 | 105 | 4.29e-06 | 1.40 |
| `AK.BCP..LHN` | 0.81 | 105 | 1.47e-06 | 0.82 |
| `AK.BCP..LHZ` | 0.81 | 105 | 5.41e-06 | 1.77 |
| `AK.PNL..LHE` | 1.03 | 119 | 2.79e-06 | 1.11 |
| `AK.PNL..LHN` | 1.03 | 119 | 3.00e-06 | 4.51 |
| `AK.PNL..LHZ` | 1.03 | 119 | 4.24e-06 | 2.78 |
| `TA.N31M..LHE` | 2.95 | 61 | 1.10e-06 | 1.13 |
| `TA.N31M..LHN` | 2.95 | 61 | 2.38e-06 | 1.82 |
| `TA.N31M..LHZ` | 2.95 | 61 | 1.80e-06 | 7.40 |
| `US.EGAK.00.LH1` | 4.62 | 0 | 8.69e-07 | 2.61 |
| `US.EGAK.00.LH2` | 4.62 | 0 | 2.22e-06 | 4.01 |
| `US.EGAK.00.LHZ` | 4.62 | 0 | 6.39e-07 | 2.89 |
| `AK.SII..LHE` | 7.73 | 248 | 1.01e-05 | 1.82 |
| `AK.SII..LHN` | 7.73 | 248 | 6.73e-06 | 0.77 |
| `AK.SII..LHZ` | 7.73 | 248 | 6.65e-07 | 6.28 |

## Result

| quantity | p05 / **p50** / p95 |
|---|---|
| Peak force | 1.38e+11 / **1.73e+11** / 2.22e+11 N |
| Impulse | 1.9e+12 / **2.5e+12** / 3.87e+12 N s |
| Duration | 293 / **296** / 297 s |
| Force azimuth | 261 / **279** / 294 deg from north |
| **Mass** | 1.14e+10 / **4.46e+10** / 2.01e+11 kg |

- Location: 60.1198, -141.2183 (depth 1.0 km, method `gsf_grid_search`, grid 2 km, resolution radius 12.8 km)
- Variance reduction: 0.402
- Azimuthal gap: 129 deg
- Regularisation: second-difference Tikhonov, order 2, zero endpoints, lambda 55.41 from the L-curve corner

### The two mass estimators

| estimator | method | p05 (kg) | p50 (kg) | p95 (kg) | a_eff basis |
|---|---|---:|---:|---:|---|
| dem_trajectory | `fmax_over_aeff` | 5.22e+10 | 8.77e+10 | 2.01e+11 | `assumed_range` |
| seismic_impulse | `impulse_over_velocity` | 1.14e+10 | 2.27e+10 | 5.58e+10 | `assumed_range` |

Consistency ratio (A/B on the medians): **3.87** — outside [1/3, 3].

The published interval is the **union** of the two, not their average.

### Modelled Green's functions

- Earth model: `prem_a_20s` via IRIS Syngine
- Band: 20-150 s at dt = 1 s
- 46 cached sets, recorded as `provenance: derived` (ADR-0016); they are modelled physics, never observations, and are never published on the bus.

### Uncertainty

200 bootstrap draws (seed 20260903) resampling stations, band_limits, lambda, source_depth, friction.

### Assumptions behind the mass

1. Published interval is the UNION of two estimators, not their average: dem_trajectory (fmax_over_aeff) gave [5.22e+10, 2.01e+11] kg and seismic_impulse (impulse_over_velocity) gave [1.14e+10, 5.58e+10] kg.
2. Consistency ratio M(dem_trajectory) / M(seismic_impulse) = 3.87 on the medians.
3. The two estimators are NOT independent: both divide by an effective acceleration built from the same Coulomb friction range. They differ in the force functional (peak versus integral) and in the source of the path geometry (terrain versus waveform), so agreement is evidence but not proof.
4. The median is the geometric mean of the two estimators' medians, which is a summary of two methods rather than a measurement.
5. The estimators disagree by more than a factor of three (ratio 3.87). This is reported, not reconciled: the union interval is correspondingly wide and the mass should be treated as order-of-magnitude only.
6. M = F_max / a_eff with a_eff = g sin(theta) (1 - phi): a rigid block on a planar slope under Coulomb friction, which ignores internal deformation, entrainment and any change of basal resistance along the path.
7. Basal friction is expressed as phi = mu / tan(theta), sampled uniformly over [0.2, 0.8]. Friction below the apparent (Heim-ratio) friction is what makes the mass accelerate at all; an absolute coefficient is not used because one larger than tan(theta) describes a mass that cannot move. The range is not calibrated to any event in this repository.
8. g = 9.81 m/s^2.
9. No DEM crop covers this runout, so the path angle is atan(H/L) from a published fall height 720 m and runout 1600 m (esec-taan). This is a weaker input than a terrain profile and AEff.basis records it as assumed_range rather than dem_trajectory.
10. M = max|integral F dt| / (a_eff * t_acc): the peak of the running impulse is the slide's peak momentum M*v_max, and v_max is taken as a_eff times the time from onset to that peak.
11. The path angle comes from the force history alone -- theta = atan(|F_vertical| / |F_horizontal|) at the instant of peak horizontal force -- so this estimator uses no DEM, no catalogue and no published geometry.
12. Basal friction as a fraction of tan(theta) sampled uniformly over [0.2, 0.8].
13. Constant a_eff through the acceleration phase, which a real slide on changing terrain does not have.
14. Onset threshold 5% of peak force; acceleration phase measured as 52 s.
15. Intervals are bootstrap percentiles over 200 draws resampling stations, band corners, lambda, source depth and friction; they are a spread over analyst choices, not a posterior.
16. Nothing here resamples the 1-D Earth model: a different model would move the answer by an amount this bootstrap cannot see.

### Notes from the run

- Force azimuth 280 deg; the mass moved towards 234 deg, which is the bearing the DEM profile follows.
- No DEM crop is committed for taan-fiord-2015, so the DEM-trajectory mass estimator falls back to a published fall height and runout.

## Timings

| stage | seconds |
|---|---:|
| prepare | 0.80 |
| gridearch | 29.90 |
| final_inversion | 3.54 |
| terrain | 0.00 |
| bootstrap | 350.19 |
| **total wall clock** | **384.47** |

## Disagreement

- higman-2018: "These findings, combined with the seismologically determined force history, further suggested a slide mass of 1–1.5 × 10 11 kg." (1e+11-1.5e+11 kg)

serac's own interval is 1.14e+10 to 2.01e+11 kg (median 4.46e+10 kg).

Against published mass, higman-2018 (1e+11-1.5e+11 kg): the intervals overlap, and serac's median is 0.36 times the geometric centre of the published interval.

This section states the numeric relationship and stops there. serac's estimate rests on assumptions listed above that the published figures do not share, and no parameter was adjusted after seeing these numbers.

## Sources

- `esec-taan` — EarthScope Data Products (2025) Exotic Seismic Events Catalog entry: Taan Fjord IRIS/EarthScope Exotic Seismic Events Catalog (ESEC); fetched 2026-09-03, sha256 `f4e107f36118481f...`, DOI resolved via not resolved
- `higman-2018` — Higman, B. et al. (2018) The 2015 landslide and tsunami in Taan Fiord, Alaska Scientific Reports doi:10.1038/s41598-018-30475-w; fetched 2026-09-03, sha256 `27ab9f6154fd7187...`, DOI resolved via crossref

