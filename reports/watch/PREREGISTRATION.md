# M3 slope watch — pre-registration

**This file is committed before the Chamoli backtest is run.** `serac validate watch` checks by
git ancestry that the commit introducing this file precedes the commit that introduces
`reports/watch/backtest_chamoli.json`, and that this file was not modified afterwards. If a
threshold below turns out to be unworkable, the honest response is to say so in the backtest
report and leave the number alone — not to edit this file.

Written on 2026-09-03, before any interferogram had been delivered by HyP3 and before any
time series existed. The InSAR network for `chamoli-rishiganga` (path 56, 260 pairs) and
`lhende-khola-trishuli` (path 121, 257 pairs) had been submitted; nothing had been processed.

## 0. What the output is, and what it is not

The output is an **ordinal tier** in {`quiet`, `elevated`, `watch`, `insufficient_data`} and a
scalar **score**. The score is a robust z-score. It is **not** a probability, it is not
calibrated, and no threshold below is claimed to correspond to any likelihood of failure. With
one positive event in the archive, no ROC and no calibration curve is estimable, and none will
be reported. **No output of this component is, or may be presented as, a failure date.**

## 1. Inputs

Per slope unit `u` and time `t`, from the MintPy small-baseline time series on the selected
track:

- `d_los(u, t)` — cumulative line-of-sight displacement, millimetres, referenced to the
  deterministic reference point defined in section 6.
- `coh(u, t)` — spatial-coherence statistic of the unit at that acquisition.
- `sens(u) = |downslope . LOS|` — the unit's LOS sensitivity from the DEM and the track
  geometry (`serac.models.watch.geometry.los_sensitivity`).

Displacement is converted to a downslope-equivalent before anything else:

```
d_slope(u, t) = d_los(u, t) / clip(sens_signed(u), |.| >= SENS_FLOOR)
```

so that a unit seen edge-on is not scored as quiet merely because its motion is invisible.
`sens_signed` keeps the sign, so positive `d_slope` is downslope movement.

## 2. Fixed parameters

| Name | Value | Why this value |
|---|---|---|
| `SENS_FLOOR` | 0.30 | Below this the track sees under a third of any downslope motion; dividing by it would amplify noise more than signal. Units below it are `insufficient_data`, never `quiet`. |
| `TRAILING_WINDOW_DAYS` | 180 | Half a year: long enough to average out one monsoon's tropospheric noise, short enough that a change within a season is still visible. |
| `MIN_SAMPLES` | 24 | Two years of nominal 12-day repeats inside the trailing two years; fewer and the harmonic fit is not identifiable. |
| `MIN_HISTORY_STEPS` | 8 | A robust z-score against a unit's own history needs at least eight prior monthly steps before it means anything. |
| `MIN_COHERENCE` | 0.30 | Below this, unwrapped phase over snow and ice is not trustworthy at C-band. |
| `HARMONIC_ORDERS` | 2 | Annual and semi-annual terms only. More orders would start absorbing the signal. |
| `ELEVATED_THRESHOLD` | 2.0 | |
| `WATCH_THRESHOLD` | 3.0 | Chosen as round robust-z values, not fitted. They are the conventional "unusual" and "very unusual" marks on a MAD-scaled z; no data informed them. |

## 3. Seasonal decomposition

At each evaluation time `T`, using **only** samples with `t <= T`, fit by ordinary least
squares:

```
d_slope(t) = a + b*t + sum_{k=1..2} [ c_k cos(2*pi*k*t/365.25) + s_k sin(2*pi*k*t/365.25) ]
```

Harmonic regression rather than STL, because the sampling is irregular (12-day nominal with
gaps up to 48 days) and STL requires a regular grid. The deseasonalised series is
`d_star(t) = d_slope(t) - (harmonic terms)`; the trend term is deliberately left in.

## 4. Statistics

- Velocity `v(u, T)`: OLS slope of `d_star` over `[T - 180 d, T]`, in mm/yr.
- Acceleration `acc(u, T) = (v(u, T) - v(u, T - 180 d)) / 0.4931 yr`, in mm/yr^2.

Each is turned into two robust z-scores. `MAD` below means `1.4826 * median(|x - median(x)|)`;
where the MAD is zero the fallback scale is `IQR / 1.349`; where that is zero too, the z-score
is 0, because a population with no spread cannot rank anything.

- **Temporal** `z_t(u, T)` — against that unit's own values at all earlier monthly steps.
  Requires `MIN_HISTORY_STEPS` prior steps.
- **Spatial** `z_s(u, T)` — against the values of all other eligible units at the same step.

The combined score takes the **minimum** of the two:

```
z_v = min(z_t(v), z_s(v))
z_a = min(z_t(acc), z_s(acc))
score(u, T) = max(z_v, z_a)
```

The minimum is the point of the design. A tropospheric artefact or an orbital ramp moves every
unit together, which inflates `z_t` but leaves `z_s` near zero; a unit's own seasonal cycle
inflates `z_s` at that time of year but leaves `z_t` near zero. Only a unit that is anomalous
**both** against its own past **and** against its neighbours survives. This is a false-alarm
suppression choice made before seeing any false-alarm count.

## 5. Tier rules

Evaluated in this order:

1. `insufficient_data` if any of: the unit is outside the processed burst footprint;
   `sens(u) < SENS_FLOOR`; fewer than `MIN_SAMPLES` samples in `[T - 730 d, T]`; median
   coherence over the trailing window `< MIN_COHERENCE`; fewer than `MIN_HISTORY_STEPS` prior
   evaluation steps.
2. `watch` if `score >= WATCH_THRESHOLD`.
3. `elevated` if `score >= ELEVATED_THRESHOLD`.
4. `quiet` otherwise.

A unit that cannot be measured is **never** reported as quiet.

## 6. Deterministic reference point (MintPy)

The reference pixel is chosen mechanically, with no operator input:

> Among pixels whose temporal-coherence is at least 0.85 and whose slope is below 15 degrees
> and which are not flagged layover or shadow on the selected track, take the one with the
> highest temporal coherence; break ties by lowest row index, then lowest column index.

Chosen for stability (flat, coherent ground), and deterministic so a re-run reproduces the
same series bit for bit. The chosen pixel and its coherence are recorded in
`reports/watch/mintpy_<aoi>.json`.

## 7. Backtest protocol (Chamoli)

- Monthly steps on the first of each month, from **2016-07-01** to **2021-02-01**, the last
  step preceding the 2021-02-07 failure.
- At each step, every slope unit is scored using only acquisitions with `t <= step`.
- Reported: the failed unit's tier at every step; the **lead time** from the first step at
  which it reaches `watch` to 2021-02-07; and at that same step the **count of other units at
  `watch`**, which is the false-alarm burden.
- If the failed unit never reaches `watch`, that is the result and it is reported as such.

**Identifying the failed unit is post-hoc labelling and is done only in the reporting layer.**
The failed unit is the slope unit with the greatest area of overlap with
`data/aoi/chamoli-rishiganga/source_zone.geojson`. Nothing in the anomaly, scoring or
threshold code may read that file, any event record, or any failure date; a test asserts this
by import and source inspection.

## 8. Langtang

The same code, thresholds and protocol are applied to `lhende-khola-trishuli` over whatever
archive exists. The write-up must separate two distinct negatives:

- **Observability** — "we could not have seen it": no coverage, insufficient samples,
  coherence below threshold, or LOS sensitivity below the floor.
- **No precursor** — the unit was measurable to the stated sensitivity and stayed `quiet`.

A null result is a valid outcome and is written up as one.

## 9. Optical feature tracking

Displacements from orientation-correlation NCC on Sentinel-2 pairs (**not** autoRIFT, not
comparable to ITS_LIVE). For every pair a **stable-ground noise floor** is measured as the
median absolute displacement over pixels with slope below 10 degrees that are not glacier and
not water; any per-unit displacement below its pair's noise floor is reported as
not-significant rather than as a measurement. The optical layer is reported alongside the
InSAR tier in v0 and does **not** enter the score, because the two have not been
cross-validated against each other.

## 10. What would falsify the design

Stated in advance so it cannot be reframed later:

- If the failed unit never reaches `watch` at any step, the tier did not work on the one
  positive case available.
- If the failed unit reaches `watch` only at a step where dozens of other units are also at
  `watch`, the tier is not usable operationally even though it "detected" the event.
- If the failed unit is `insufficient_data` throughout, the honest finding is that this
  geometry and this archive could not have seen this failure, which is a statement about
  observability and not about precursors.
