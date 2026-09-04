# Model card — M3 slope watch (kinematic anomaly layer)

**Component**: `src/serac/models/watch/` — serac's L0/L1 layer.
**Version**: 0.1.0. **Status**: v0 baseline, not promoted.
**Gate**: `make validate-watch`. **Pre-registration**: `reports/watch/PREREGISTRATION.md`.

## What it does

Ranks slope units in an AOI by evidence of kinematic destabilisation, from a Sentinel-1
small-baseline InSAR time series and Sentinel-2 feature tracking, and assigns each unit an
ordinal tier: **Quiet**, **Elevated**, **Watch**, or **insufficient_data**.

## Intended use

- Prioritising where to put money and instruments: which slopes in a valley deserve a ground
  sensor, a repeat survey, or a higher-cadence commercial tasking.
- Producing a defensible, reproducible, timestamped record of what the satellite archive did
  and did not show about a slope before an event.
- Feeding an exposure-prioritisation process on a horizon of months to years.

## Out-of-scope use

- **This is not a time-of-failure predictor.** It does not output a failure date, a failure
  window, or a time-to-failure, and no such field exists in any schema or report it writes.
  `validate-watch` fails the build if one appears.
- **The score is not a calibrated probability.** It is a robust z-score. There is exactly one
  positive event in the archive processed here, so no ROC curve, no calibration curve and no
  precision/recall figure is estimable, and none is reported.
- Not an evacuation trigger. A Watch tier is a reason to look harder, not a reason to move
  people. The layer that saves lives in serac is L2/L3 (seismic detection plus runout), not
  this one.
- Not a substitute for ground instrumentation on a slope already known to be moving.
- Not comparable with ITS_LIVE: the optical tracker is not autoRIFT (see below).

## Data

| Input | Source | Provenance |
|---|---|---|
| Sentinel-1 interferograms | ASF HyP3 `INSAR_ISCE_MULTI_BURST`, 20x4 looks (80 m) | Every product zip hashed on arrival, cropped to the AOI, then deleted (`retention: transient`); crops retained and re-hashable |
| DEM | Copernicus GLO-30 public COGs | Ledgered, windowed reads |
| Glacier outlines | RGI 7.0 official regional archives | **Mirror**, not the NSIDC original: NSIDC's endpoint answers 401 to an Earthdata bearer token and needs an interactive OAuth redirect. Fetched from the University of Bremen `rgi70_official` path, hashed and ledgered with that URL. CC BY 4.0 |
| Sentinel-2 L2A | Earth Search public COGs (B03 10 m, SCL) | Through the existing `EarthSearchSentinel2Adapter` |
| Tropospheric correction | MintPy `height_correlation` | **Not GACOS, not ERA5** — see limitations |

Track selection, the interferogram network, the slope-unit delineation and the MintPy config
are each hashed, and those hashes are carried in the reports and in the zarr attributes.

## Method

1. **Track selection** by a rule frozen in `track_select.SELECTION_RULE` and committed before
   it was ever run: LOS sensitivity to downslope motion over all AOI terrain steeper than 25
   degrees, DEM-simulated layover and shadow fraction over that same mask, scene count and
   largest temporal gap. Eligibility is a hard filter; the score only orders what survives it.
2. **Network**: `n_conn = 2`, temporal baseline <= 36 days, plus annual anchors. A disclosed
   budget choice, not a science one.
3. **Slope units**: connected components of (aspect octant, 250 m elevation band) over terrain
   steeper than 15 degrees, with a circular-mean aspect smoother and small components dissolved
   into their longest-shared-boundary neighbour. **Not `r.slopeunits`** and not a hydrological
   half-basin delineation (GRASS containers are amd64-only here). Deterministic and hashed.
4. **Time series**: MintPy `smallbaselineApp`, two passes, with a deterministic reference-point
   rule fixed in the pre-registration.
5. **Aggregation**: median LOS displacement per unit per epoch over pixels clearing a temporal
   coherence floor, with the contributing pixel count stored alongside every value.
6. **Anomaly model**: harmonic deseasonalisation (annual + semi-annual, fitted causally),
   trailing 180-day velocity and its change, then two robust z-scores — against the unit's own
   history and against its peers at the same step — combined by taking the **minimum**, so a
   common-mode atmospheric ramp cancels one and a seasonal cycle cancels the other. Tier
   thresholds 2.0 (Elevated) and 3.0 (Watch).
7. **Optical**: orientation-correlation NCC on Sentinel-2 B03 pairs, with a stable-ground noise
   floor measured on every pair. Reported alongside the InSAR tier; it does **not** enter the
   score, because the two have not been cross-validated against each other.

## Metrics

There is no accuracy metric here, and that is the honest position, not an omission. What is
reported instead:

- The Chamoli walk-forward: the labelled unit's tier at every monthly step, the lead time to
  its first Watch, and **the number of other units simultaneously at Watch** — the false-alarm
  burden, which is what decides whether the tier could be acted on.
- The Langtang result, with observability separated from the absence of a precursor.
- Per-pair optical noise floors, measured — including the finding that the pre-registered
  median floor is **not a usable discriminator** as implemented (see below).
- Coverage: how much of each AOI the processed burst footprint actually images.

See `reports/watch/backtest_chamoli.md` and `reports/watch/backtest_langtang.md` for the
numbers. A result there is a description of one event, not a performance estimate.

### Results as run

| | chamoli-rishiganga | lhende-khola-trishuli |
|---|---|---|
| Sentinel-1 track (frozen rule) | 56 ASC IW1 | 121 DESC IW3 |
| interferograms succeeded | 260 / 260 | 257 / 257 |
| archive span | 2016-01-08 - 2021-01-29 | 2022-01-05 - 2026-08-19 |
| epochs | 134 | 130 |
| slope units | 6,541 | 26,935 |
| units measurable at >= 1 step | 375 (5.7%) | 3,879 (14.4%) |
| source-zone units | 780 | 48 |
| source-zone units measurable at >= 1 step | **0** | **5** (1 Elevated, 4 Quiet) |
| labelled unit's tier | `insufficient_data` at all 56 steps | `insufficient_data` at all 122 steps |
| lead time to first Watch | n/a — never entered the tier | n/a — no unit reached Watch |
| other units at Watch, median / max per step | 1 / 35 | 0 / 271 |
| AOI median temporal coherence (imaged pixels) | 0.139 | 0.622 |

**Chamoli is purely an observability result. Langtang is mixed, and the mixture is the more
interesting finding.**

- **Chamoli**: no part of the source zone was measurable at any step, so the tier was never in
  a position to be asked. This is not "no precursor was detected" and it is not a failure of
  the thresholds.
- **Langtang**: 43 of 48 source-zone units were never measurable — same observability limit.
  But **5 were measurable at 38 of 122 steps**, and of those **four stayed Quiet** (a genuine
  null, for those units) and **one, `su-03644`, reached Elevated**. A single unit at a single
  tier, from an uncalibrated ordinal score with no validated positive, is evidence for looking
  harder at that slope and for nothing else. It is not a detection and it carries no date.

Why so little was measurable, measured rather than asserted:

- **Chamoli** is decorrelated **across the whole AOI**, not only at altitude. Median temporal
  coherence over imaged pixels is 0.139 AOI-wide, and only 6.0% of pixels clear 0.40; the
  median barely moves between the 0-3000 m band (0.134) and the 5000-5500 m band (0.133).
  Altitude sharpens an already severe problem rather than creating it. The source zone spans
  3,305-6,493 m (median 4,984 m), computed from the DEM under the source-zone polygon and
  committed in `backtest_chamoli.json`; by area 31.5% lies in 4500-5000 m where 3.8% of pixels
  clear the threshold, 30.7% in 5000-5500 m and 18.2% above 5500 m where 0.0% do, and 19.6%
  below 4500 m where 6-11% do. Separately, the labelled
  unit is west-facing (aspect 271 degrees) with a signed LOS sensitivity of **-0.074** on the
  chosen ascending track — the horizontal approach to the satellite almost exactly cancels the
  vertical recession — and 229 of the 780 source-zone units fall below the sensitivity floor
  for the same reason.
- **Langtang** is *not* coherence-limited in the area it images: median temporal coherence over
  imaged pixels is 0.622 and 83.9% clear 0.40. Its limit is **footprint**: 19,385 units (72%)
  lie outside the processed burst footprint, because a Sentinel-1 subswath is ~85 km wide and
  this AOI is a 100 km corridor. The plan records 25% AOI coverage independently. Within the
  source zone the binding constraint is sample count at altitude (39 of 43) — the 5500 m+ band
  is the one place Langtang does decorrelate (median 0.170, 14.1% above 0.40).

**The measurability thresholds are not pre-registered.** `MIN_PIXEL_TEMPORAL_COHERENCE = 0.40`
and `MIN_PIXELS_PER_UNIT = 5` decide whether a unit is measurable at all and are more decisive
than any pre-registered parameter. They were introduced with the aggregation code, after
`PREREGISTRATION.md` was committed and before any backtest ran, and have never been edited —
so this is not post-hoc tuning — but 0.40 sits above the pre-registered unit-level
`MIN_COHERENCE = 0.30`, in the direction that makes fewer units measurable. The result is
materially sensitive to it, and the sweep is committed in both backtest reports:

| coherence threshold | Chamoli units measurable | Chamoli source-zone | Langtang units measurable | Langtang source-zone |
|---|---|---|---|---|
| 0.20 | 1,902 (29.1%) | 111 / 780 | 7,199 (26.7%) | 31 / 48 |
| 0.30 | 642 (9.8%) | 1 / 780 | 6,968 (25.9%) | 16 / 48 |
| **0.40 (in use)** | **433 (6.6%)** | **0 / 780** | **6,407 (23.8%)** | **7 / 48** |
| 0.50 | 325 (5.0%) | 0 / 780 | 5,561 (20.6%) | 1 / 48 |
| 0.60 | 232 (3.5%) | 0 / 780 | 4,475 (16.6%) | 0 / 48 |

At 0.20 the Chamoli source zone would have had 111 nominally measurable units instead of none.
The headline "nothing in the Chamoli source zone was measurable" is therefore a statement about
this configuration **including its un-pre-registered coherence cut**, not a threshold-free fact
about C-band InSAR. Lowering the cut would not have made those units *trustworthy* — the point
of a coherence floor is that low-coherence phase is unreliable — but the reader is owed the
number rather than a bare claim.

The single most useful thing this component produced is therefore a quantified statement of
what one Sentinel-1 track cannot see, which is a prerequisite for arguing for the second track
and the tropospheric correction that would be needed to see it.

### What the optical layer actually showed

Four season-matched post-monsoon scenes per AOI, three annual pairs each.

| AOI | pairs | stable chips | median floor | p95 floor | verdict |
|---|---|---|---|---|---|
| chamoli-rishiganga | 3 | 512 | 2.6 - 10.3 m | 54 - 59 m | heavy-tailed; median unusable |
| lhende-khola-trishuli | 3 | 6,133 | 0.0 m | 1.5 - 2.7 m | degenerate; median unusable |

Two honest negatives here, both left uncorrected because fixing them after seeing them would
be tuning:

1. **The median floor is degenerate on well-correlated ground.** A stable chip's correlation
   peak lands on the zero-shift sample and the sub-pixel fit returns exactly 0, so over half
   the Langtang stable chips are exactly zero and the median floor collapses to 0.0 m. The
   pre-registered test "displacement exceeds the median floor" is then true of every non-zero
   measurement and means nothing. `noise_floor_degenerate` flags it.
2. **The floor is heavy-tailed everywhere.** Chamoli's stable ground has a p95 of 54-59 m
   against a median of 3-10 m, on only 512 stable chips — that AOI has very little terrain
   below 10 degrees to measure a floor on.

**The optical significance flag should not be read as a detection at v0.** The p95 in each
pair's record is the statistic with meaning. The optical layer does not enter the watch score,
so none of this affects any tier. A v1 should pre-register a percentile floor rather than a
median, and should measure it on a stable-ground sample large enough to estimate a tail.

## Failure modes

- **False alarms from atmosphere.** The minimum-of-two-z-scores rule suppresses common-mode
  delay but not a *localised* wet-delay cell sitting over one unit. With `height_correlation`
  as the only tropospheric correction, this is the most likely source of a spurious Watch.
- **Unwrapping errors.** A 2-pi cycle slip in a unit's pixels appears as a step in
  displacement and can look like an acceleration. The per-unit median absorbs isolated slips;
  it does not absorb a slip that affects most of a unit.
- **Reference-point contamination.** Every displacement is relative to one pixel. If that pixel
  itself moves, every unit acquires a spurious common trend. The rule picks a coherent,
  low-slope, non-layover pixel to make this unlikely, not impossible.
- **Slope units that do not correspond to real failure blocks.** The segmentation follows
  aspect and elevation, so a failure spanning two aspect octants is split across units and each
  half is scored separately, diluting both.
- **Missing units.** A unit outside the burst footprint or below the LOS-sensitivity floor is
  reported `insufficient_data` and simply is not being watched. It is never reported Quiet, but
  a reader skimming a tier table can still mistake absence for safety.

## Physical limitations

These are properties of the physics and the sensor, not of the implementation, and no amount
of modelling removes them:

- **C-band decorrelation over snow and ice.** Sentinel-1's 5.5 cm wavelength decorrelates
  within days over fresh snow and over an active glacier surface. The exact surfaces most
  relevant to a rock-ice avalanche source zone are the ones this sensor sees worst, and the
  decorrelation is worst in winter and through the monsoon.
- **Layover and shadow.** A slope facing the sensor more steeply than the incidence angle lays
  over; a back slope steeper than 90 minus the incidence angle is in shadow. On a single track
  a substantial fraction of steep terrain is unmeasurable, and which fraction depends on
  aspect. North faces are frequently among them.
- **LOS blindness by geometry.** For an ascending pass the horizontal and vertical components
  of downslope motion partially cancel on west-facing slopes: at 40 degrees slope and 35
  degrees incidence, an ascending track sees 96 per cent of the motion on an east-facing slope
  and 9 per cent on a west-facing one. A single track cannot watch a whole valley.
- **Brittle failure with little tertiary creep.** The whole premise of a kinematic watch layer
  is that a slope accelerates measurably before it fails. Crystalline bedrock failures —
  including the class of event serac exists for — can occur with little or no resolvable
  precursory displacement. **A Quiet tier on a competent rock slope is weak evidence of
  stability.**
- **Monsoon cloud gaps.** Optical tracking over High Mountain Asia is unusable from roughly
  June to September, exactly the season of greatest hydrological loading.
- **Resolution.** 80 m interferogram pixels cannot resolve a detachment smaller than a few
  hundred metres across, and a 30 m DEM cannot resolve the structure of a headwall.

## Compute

Track selection, slope units, aggregation, the anomaly model and the backtest all run on CPU
in minutes on a laptop. The InSAR processing is external (ASF HyP3) and the binding local cost
is disk and download bandwidth for the transient product zips, which is why the
stream-crop-delete retention exists.

## Reproducibility

`serac watch select-track` -> `plan-network` -> `submit-insar` -> `poll-insar` ->
`slope-units` -> `mintpy` -> `aggregate` -> `optical` -> `backtest`. Every step writes a
report under `reports/watch/` carrying the hash of its inputs. The scoring code reads no file,
takes no path argument, and transitively imports nothing that could tell it where or when a
failure happened; `tests/unit/watch/test_no_hindsight.py` enforces that mechanically, and
`tests/unit/watch/test_anomaly.py` proves causality by appending future samples and truncating
them again.
