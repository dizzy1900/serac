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
- Per-pair optical noise floors, measured.
- Coverage: how much of each AOI the processed burst footprint actually images.

See `reports/watch/backtest_chamoli.md` and `reports/watch/backtest_langtang.md` for the
numbers. A result there is a description of one event, not a performance estimate.

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
