# Chamoli 2021 — pseudo-prospective backtest of the slope-watch tier

Generated 2026-09-03T23:05:48.085368+00:00 from `reports/watch/backtest_chamoli.json`.
Thresholds and protocol were fixed in `reports/watch/PREREGISTRATION.md`, committed before any
interferogram had been delivered. `make validate-watch` checks that ancestry against git.

> The watch tier is an **ordinal** state. It is not a calibrated failure probability and it is never a prediction of when a slope will fail. With one positive event in the archive no ROC curve and no calibration curve is estimable, and none is reported here.

## Result

The labelled unit (`su-05207`) was **`insufficient_data` at every one of the 56 steps** (final reason: `low_los_sensitivity`). It never entered the tier at all.

**This is an observability result, not a statement about precursors.** This configuration — one ascending track, 80 m pixels, C-band, a height-correlation tropospheric correction — could not measure the slope that failed, whether or not it was moving. Reporting it as 'no precursor detected' would be wrong, and reporting it as a failure of the tier would be wrong too: the tier was never in a position to be asked. Section 10 of the pre-registration named exactly this outcome in advance as the third of the three ways the design could come out.

## What was processed

| quantity | value |
|---|---|
| Sentinel-1 relative orbit | 56 (ASCENDING) |
| track selection rule sha256 | `d6c159600c5b71f0` |
| interferograms planned / succeeded | 260 / 260 |
| AOI bbox imaged by the burst footprint | 100.0 % |
| slope units | 6541 |
| units with any InSAR measurement | 433 |
| time-series epochs | 134 |
| tropospheric correction | Tropospheric correction is MintPy height_correlation, not GACOS and not ERA5: GACOS needs an email workflow and ERA5 needs a CDS key, neither of which was available. It removes only the elevation-correlated part of the delay, so turbulent monsoon-season wet delay survives it. This is the largest known error source in these velocities. |

## Protocol

Monthly steps on the first of each month from 2016-07-01 to
2021-02-01, the last step preceding the failure at
2021-02-07T04:51:18+00:00. At each step every slope unit was scored using **only**
acquisitions at or before that step; `tests/unit/watch/test_anomaly.py` proves this by
appending future samples, truncating them again and asserting the scores are unchanged.

The failed unit was identified **after** all scoring, by the rule pre-registered in section 7:
greatest area of overlap with data/aoi/<aoi>/source_zone.geojson.
The source zone is a hand-digitised design rectangle in the AOI definition (geometry_quality hand_digitised_approximate, positional accuracy 1000 m), not a mapped detachment outline. The labelled unit is therefore approximate.

## Numbers

| quantity | value |
|---|---|
| steps | 56 |
| slope units | 6541 |
| reached Watch | False |
| lead time to first Watch | n/a |
| other units at Watch on that step | n/a |
| reached Elevated | False |
| lead time to first Elevated | n/a |
| median units at Watch per step | 1.0 |
| max units at Watch in any step | 35 |
| median units at insufficient_data per step | 6,166.0 |

Tier of the labelled unit across the walk-forward:
{"elevated": 0, "insufficient_data": 56, "quiet": 0, "watch": 0}

## The source zone, unit by unit

780 slope units intersect the source zone. **0** of them were measurable at any step; the rest failed a data-adequacy test (229 low los sensitivity, 551 too few samples). A unit that fails one of those tests is not being watched at all, and nothing about its stability follows from its absence from the Watch list.

| unit | overlap (m2) | aspect | LOS sens | measurable | best tier | reason |
|---|---|---|---|---|---|---|
| `su-05207` | 893,700 | 271 deg | -0.074 | 0/56 | insufficient_data | low_los_sensitivity |
| `su-05871` | 600,300 | 356 deg | -0.460 | 0/56 | insufficient_data | too_few_samples |
| `su-04892` | 571,500 | 36 deg | -0.796 | 0/56 | insufficient_data | too_few_samples |
| `su-05258` | 553,500 | 308 deg | -0.257 | 0/56 | insufficient_data | low_los_sensitivity |
| `su-04591` | 532,872 | 224 deg | -0.059 | 0/56 | insufficient_data | low_los_sensitivity |
| `su-05176` | 528,300 | 166 deg | -0.514 | 0/56 | insufficient_data | too_few_samples |
| `su-05272` | 528,279 | 352 deg | -0.445 | 0/56 | insufficient_data | too_few_samples |
| `su-05141` | 520,200 | 166 deg | -0.580 | 0/56 | insufficient_data | too_few_samples |
| `su-04293` | 513,000 | 87 deg | -0.914 | 0/56 | insufficient_data | too_few_samples |
| `su-05682` | 478,130 | 316 deg | -0.374 | 0/56 | insufficient_data | too_few_samples |
| `su-04560` | 470,700 | 44 deg | -0.829 | 0/56 | insufficient_data | too_few_samples |
| `su-05119` | 459,000 | 87 deg | -0.888 | 0/56 | insufficient_data | too_few_samples |
| _... 768 more_ | | | | | | |

## Why: C-band temporal coherence against elevation

The physical limitation this component is most constrained by, measured on this stack rather
than asserted. MintPy temporal coherence over the 260-pair network, against the HyP3 DEM:

| elevation band | pixels | median temporal coherence | fraction >= 0.40 |
|---|---|---|---|
| 0 - 3,000 m | 45,878 | 0.134 | 0.061 |
| 3,000 - 4,000 m | 56,710 | 0.147 | 0.063 |
| 4,000 - 4,500 m | 20,990 | 0.153 | 0.112 |
| 4,500 - 5,000 m | 15,152 | 0.138 | 0.038 |
| 5,000 - 5,500 m | 9,600 | 0.133 | 0.000 |
| 5,500 - 9,000 m | 7,019 | 0.095 | 0.000 |
| **whole AOI** | 155,349 | 0.139 | 0.060 |

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
| 2018-03-01 | insufficient_data | n/a | n/a | 2 | 2 |
| 2018-04-01 | insufficient_data | n/a | n/a | 1 | 1 |
| 2018-05-01 | insufficient_data | n/a | n/a | 26 | 26 |
| 2018-06-01 | insufficient_data | n/a | n/a | 35 | 35 |
| 2018-07-01 | insufficient_data | n/a | n/a | 13 | 13 |
| 2018-08-01 | insufficient_data | n/a | n/a | 4 | 4 |
| 2018-09-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2018-10-01 | insufficient_data | n/a | n/a | 6 | 6 |
| 2018-11-01 | insufficient_data | n/a | n/a | 3 | 3 |
| 2018-12-01 | insufficient_data | n/a | n/a | 6 | 6 |
| 2019-01-01 | insufficient_data | n/a | n/a | 2 | 2 |
| 2019-02-01 | insufficient_data | n/a | n/a | 1 | 1 |
| 2019-03-01 | insufficient_data | n/a | n/a | 7 | 7 |
| 2019-04-01 | insufficient_data | n/a | n/a | 15 | 15 |
| 2019-05-01 | insufficient_data | n/a | n/a | 8 | 8 |
| 2019-06-01 | insufficient_data | n/a | n/a | 5 | 5 |
| 2019-07-01 | insufficient_data | n/a | n/a | 5 | 5 |
| 2019-08-01 | insufficient_data | n/a | n/a | 1 | 1 |
| 2019-09-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-10-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-11-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2019-12-01 | insufficient_data | n/a | n/a | 2 | 2 |
| 2020-01-01 | insufficient_data | n/a | n/a | 9 | 9 |
| 2020-02-01 | insufficient_data | n/a | n/a | 7 | 7 |
| 2020-03-01 | insufficient_data | n/a | n/a | 9 | 9 |
| 2020-04-01 | insufficient_data | n/a | n/a | 5 | 5 |
| 2020-05-01 | insufficient_data | n/a | n/a | 2 | 2 |
| 2020-06-01 | insufficient_data | n/a | n/a | 0 | 0 |
| 2020-07-01 | insufficient_data | n/a | n/a | 1 | 1 |
| 2020-08-01 | insufficient_data | n/a | n/a | 1 | 1 |
| 2020-09-01 | insufficient_data | n/a | n/a | 1 | 1 |
| 2020-10-01 | insufficient_data | n/a | n/a | 1 | 1 |
| 2020-11-01 | insufficient_data | n/a | n/a | 1 | 1 |
| 2020-12-01 | insufficient_data | n/a | n/a | 3 | 3 |
| 2021-01-01 | insufficient_data | n/a | n/a | 9 | 9 |
| 2021-02-01 | insufficient_data | n/a | n/a | 8 | 8 |

## Limitations that bear on this result

See `reports/MODEL_CARD_watch.md` for the full list. The ones that matter most here:

- Tropospheric correction is `height_correlation`, not GACOS or ERA5, so turbulent wet delay
  survives into the velocities. This is the most likely source of a spurious Watch.
- One ascending track. Downslope motion on west-facing slopes projects onto the line of sight
  with a factor near zero, so those units are reported `insufficient_data`, not `quiet`.
- C-band decorrelates over snow and ice within days, which is the surface of a rock-ice
  avalanche source zone.
- A brittle crystalline failure need not have measurable tertiary creep at all. A `quiet` tier
  on competent bedrock is weak evidence of stability.
