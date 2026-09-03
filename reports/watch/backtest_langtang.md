# Langtang / Lhende Khola 2026 — slope-watch result

Generated 2026-09-03T23:07:16.040763+00:00. Same code, same pre-registered thresholds and the same
protocol as the Chamoli backtest; only the AOI and the window differ.

> The watch tier is an **ordinal** state. It is not a calibrated failure probability and it is never a prediction of when a slope will fail. With one positive event in the archive no ROC curve and no calibration curve is estimable, and none is reported here.

**Window truncation.** The interferogram archive processed here spans
2022-01-05 to 2026-08-19, not back to the start of the Sentinel-1
record in 2014. This is a deliberate budget choice, disclosed rather than hidden: the same
`n_conn = 2`, `Bt <= 36 d` network over a decade would not have fitted the disk or the
session. The walk-forward itself steps from 2016-07-01 to
2026-08-01, so its early steps precede the archive and are
`insufficient_data` by construction. A slow precursor that began before 2022-01-05 is
outside what this run could have detected, independently of everything below.

## What was processed

| quantity | value |
|---|---|
| Sentinel-1 relative orbit | 121 (DESCENDING) |
| track selection rule sha256 | `d6c159600c5b71f0` |
| interferograms planned / succeeded | 257 / 257 |
| AOI bbox imaged by the burst footprint | 25.0 % |
| slope units | 26935 |
| units with any InSAR measurement | 6407 |
| time-series epochs | 130 |
| tropospheric correction | Tropospheric correction is MintPy height_correlation, not GACOS and not ERA5: GACOS needs an email workflow and ERA5 needs a CDS key, neither of which was available. It removes only the elevation-correlated part of the delay, so turbulent monsoon-season wet delay survives it. This is the largest known error source in these velocities. |

## We could not have seen it — observability

This section is about the *sensor and the archive*, not about the slope.

| quantity | value |
|---|---|
| slope units in the AOI | 26935 |
| units never observable at any step | 23056 |
| units observable at the final step | 3879 |

Reasons a unit was not observable at the final step:

| reason | units |
|---|---|
| outside the processed burst footprint | 19385 |
| LOS sensitivity below the floor | 2983 |
| too few acquisitions | 688 |
| coherence below the floor | 0 |
| too little walk-forward history | 0 |

A unit in any of those rows was **not being watched**. Nothing about its stability follows
from its absence from the Watch list.

## There was no precursor — units that were observed and stayed quiet

This section is about the *slope*, and only for units the previous section shows were
measurable.

| quantity | value |
|---|---|
| observed and `quiet` at the final step | 3469 |
| observed and `elevated` | 212 |
| observed and `watch` | 198 |
| labelled source-zone unit | `su-04418` |
| its tier at the final step | `insufficient_data` |
| its reason, if not measurable | `too_few_samples` |
| did it ever reach Watch | False |
| did it ever reach Elevated | False |

## Why: C-band temporal coherence against elevation

| elevation band | pixels | median temporal coherence | fraction >= 0.40 |
|---|---|---|---|
| 0 - 3,000 m | 212,285 | 0.000 | 0.372 |
| 3,000 - 4,000 m | 71,264 | 0.000 | 0.386 |
| 4,000 - 4,500 m | 29,864 | 0.717 | 0.613 |
| 4,500 - 5,000 m | 23,064 | 0.757 | 0.711 |
| 5,000 - 5,500 m | 11,930 | 0.432 | 0.526 |
| 5,500 - 9,000 m | 5,252 | 0.159 | 0.130 |
| **whole AOI** | 353,659 | 0.000 | 0.419 |

## The source zone, unit by unit

48 slope units intersect the source zone. **0** of them were measurable at any step; the rest failed a data-adequacy test (4 low los sensitivity, 39 too few samples, 5 too little history). A unit that fails one of those tests is not being watched at all, and nothing about its stability follows from its absence from the Watch list.

| unit | overlap (m2) | aspect | LOS sens | measurable | best tier | reason |
|---|---|---|---|---|---|---|
| `su-04418` | 411,792 | 313 deg | -0.912 | 0/122 | insufficient_data | too_few_samples |
| `su-04438` | 284,400 | 319 deg | -0.901 | 0/122 | insufficient_data | too_few_samples |
| `su-03644` | 274,981 | 271 deg | -0.986 | 38/122 | elevated | too_little_history |
| `su-04928` | 258,175 | 357 deg | -0.638 | 0/122 | insufficient_data | too_few_samples |
| `su-04200` | 221,400 | 273 deg | -0.987 | 0/122 | insufficient_data | too_few_samples |
| `su-04018` | 219,786 | 274 deg | -0.988 | 38/122 | quiet | too_little_history |
| `su-04328` | 187,406 | 1 deg | -0.445 | 0/122 | insufficient_data | too_few_samples |
| `su-04417` | 160,200 | 319 deg | -0.904 | 0/122 | insufficient_data | too_few_samples |
| `su-04737` | 156,600 | 331 deg | -0.844 | 0/122 | insufficient_data | too_few_samples |
| `su-04891` | 127,800 | 357 deg | -0.672 | 0/122 | insufficient_data | too_few_samples |
| `su-04619` | 123,931 | 350 deg | -0.610 | 0/122 | insufficient_data | too_few_samples |
| `su-04748` | 108,418 | 356 deg | -0.591 | 0/122 | insufficient_data | too_few_samples |
| _... 36 more_ | | | | | | |

## Reading this honestly

The source-zone unit was **not measurable** at the final step (reason: `too_few_samples`). This is an observability result, not a statement about precursors: this configuration could not have seen a precursor there whether or not one existed. Reporting it as 'no precursor found' would be wrong.

A null result is a valid and important output of this component, and it is the honest one to
publish. What it costs to do better is stated in the model card: a second track for the
opposite-facing slopes, a real tropospheric correction (GACOS or ERA5), finer looks, and
ground truth on at least one instrumented slope to calibrate anything at all.

## Step-by-step

| step | tier | score | LOS velocity (mm/yr) | units at watch | other units at watch |
|---|---|---|---|---|---|
| 2016-07-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2016-08-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2016-09-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2016-10-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2016-11-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2016-12-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2017-01-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2017-02-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2017-03-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2017-04-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2017-05-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2017-06-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2017-07-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2017-08-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2017-09-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2017-10-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2017-11-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2017-12-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2018-01-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2018-02-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2018-03-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2018-04-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2018-05-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2018-06-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2018-07-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2018-08-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2018-09-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2018-10-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2018-11-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2018-12-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-01-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-02-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-03-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-04-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-05-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-06-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-07-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-08-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-09-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-10-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-11-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-12-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2020-01-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2020-02-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2020-03-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2020-04-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2020-05-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2020-06-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2020-07-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2020-08-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2020-09-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2020-10-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2020-11-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2020-12-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2021-01-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2021-02-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2021-03-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2021-04-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2021-05-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2021-06-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2021-07-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2021-08-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2021-09-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2021-10-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2021-11-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2021-12-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2022-01-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2022-02-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2022-03-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2022-04-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2022-05-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2022-06-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2022-07-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2022-08-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2022-09-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2022-10-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2022-11-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2022-12-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2023-01-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2023-02-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2023-03-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2023-04-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2023-05-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2023-06-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2023-07-01 | insufficient_data | n/a | n/a | 76 | 76 |
| 2023-08-01 | insufficient_data | n/a | n/a | 76 | 76 |
| 2023-09-01 | insufficient_data | n/a | n/a | 109 | 109 |
| 2023-10-01 | insufficient_data | n/a | n/a | 112 | 112 |
| 2023-11-01 | insufficient_data | n/a | n/a | 179 | 179 |
| 2023-12-01 | insufficient_data | n/a | n/a | 271 | 271 |
| 2024-01-01 | insufficient_data | n/a | n/a | 163 | 163 |
| 2024-02-01 | insufficient_data | n/a | n/a | 118 | 118 |
| 2024-03-01 | insufficient_data | n/a | n/a | 112 | 112 |
| 2024-04-01 | insufficient_data | n/a | n/a | 36 | 36 |
| 2024-05-01 | insufficient_data | n/a | n/a | 61 | 61 |
| 2024-06-01 | insufficient_data | n/a | n/a | 100 | 100 |
| 2024-07-01 | insufficient_data | n/a | n/a | 65 | 65 |
| 2024-08-01 | insufficient_data | n/a | n/a | 43 | 43 |
| 2024-09-01 | insufficient_data | n/a | n/a | 26 | 26 |
| 2024-10-01 | insufficient_data | n/a | n/a | 24 | 24 |
| 2024-11-01 | insufficient_data | n/a | n/a | 26 | 26 |
| 2024-12-01 | insufficient_data | n/a | n/a | 32 | 32 |
| 2025-01-01 | insufficient_data | n/a | n/a | 22 | 22 |
| 2025-02-01 | insufficient_data | n/a | n/a | 24 | 24 |
| 2025-03-01 | insufficient_data | n/a | n/a | 16 | 16 |
| 2025-04-01 | insufficient_data | n/a | n/a | 21 | 21 |
| 2025-05-01 | insufficient_data | n/a | n/a | 99 | 99 |
| 2025-06-01 | insufficient_data | n/a | n/a | 99 | 99 |
| 2025-07-01 | insufficient_data | n/a | n/a | 36 | 36 |
| 2025-08-01 | insufficient_data | n/a | n/a | 72 | 72 |
| 2025-09-01 | insufficient_data | n/a | n/a | 114 | 114 |
| 2025-10-01 | insufficient_data | n/a | n/a | 106 | 106 |
| 2025-11-01 | insufficient_data | n/a | n/a | 103 | 103 |
| 2025-12-01 | insufficient_data | n/a | n/a | 86 | 86 |
| 2026-01-01 | insufficient_data | n/a | n/a | 56 | 56 |
| 2026-02-01 | insufficient_data | n/a | n/a | 21 | 21 |
| 2026-03-01 | insufficient_data | n/a | n/a | 45 | 45 |
| 2026-04-01 | insufficient_data | n/a | n/a | 97 | 97 |
| 2026-05-01 | insufficient_data | n/a | n/a | 34 | 34 |
| 2026-06-01 | insufficient_data | n/a | n/a | 89 | 89 |
| 2026-07-01 | insufficient_data | n/a | n/a | 127 | 127 |
| 2026-08-01 | insufficient_data | n/a | n/a | 198 | 198 |
