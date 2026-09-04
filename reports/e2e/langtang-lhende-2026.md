# End-to-end replay: Langtang Lirung / Lhende Khola / Trishuli, 26 August 2026

`serac cascade e2e --event langtang-lhende-2026` on serac 0.1.0, run 2026-09-04T01:34:52.802208+00:00.

## Verdict

**The chain stops at the `detection` stage.** no candidate in either mode: the committed fixture carries 2 receiver(s) against the detector's minimum of 3 contributing stations, and no window was ever scored

No stage downstream of that point ran, and nothing was substituted for the missing input. serac produced **no cascade forecast and no CAP alert** for this event.

## Chain

| # | Stage | Component | Source | Outcome |
|---|---|---|---|---|
| 1 | `waveform` | committed seismic fixture | executed | **produced** |
| 2 | `detection` | M1 discriminator (executed here) | executed | **did_not_fire** |
| 3 | `lfh` | M2 single-force inversion (executed here, offline) | executed | **refused** |
| 4 | `runout` | M4 runout surrogate | unavailable | **not_reached** |
| 5 | `cap` | M5 CAP 1.2 generator | unavailable | **not_reached** |
| 6 | `avoided_loss` | M5 avoided-loss computation | executed | **insufficient_input** |

## Stage detail

### `waveform` — committed seismic fixture

- outcome: **produced** (executed)
- artifact: `data/fixtures/seismic/langtang-2026/manifest.json` (sha256 `aaec97e422c4fb61…`, generated unknown)
- summary: 2 receiver(s) over 2026-08-26T02:50:00+00:00 to 2026-08-26T02:58:00+00:00

```json
{
  "stations": [
    "NK.KKN..BHZ",
    "IO.EVN..BHZ"
  ],
  "window_end_utc": "2026-08-26T02:58:00+00:00",
  "window_seconds": 480.0,
  "window_start_utc": "2026-08-26T02:50:00+00:00"
}
```
> Waveforms are the committed fixture langtang-2026 (status fetched); licence None, see https://www.earthscope.org/terms-of-service/.

### `detection` — M1 discriminator (executed here)

- outcome: **did_not_fire** (executed)
- summary: no candidate in either mode: the committed fixture carries 2 receiver(s) against the detector's minimum of 3 contributing stations, and no window was ever scored

```json
{
  "modes": {
    "batch_600s": {
      "chunks_ingested": 194,
      "class_label": null,
      "compute_seconds_total": 0.0723,
      "fired": false,
      "min_contributing_stations": 3,
      "probability": null,
      "windows_scored": 0
    },
    "sliding_180s": {
      "chunks_ingested": 194,
      "class_label": null,
      "compute_seconds_total": 0.0731,
      "fired": false,
      "min_contributing_stations": 3,
      "probability": null,
      "windows_scored": 0
    }
  },
  "receivers_in_fixture": [
    "IO.EVN",
    "NK.KKN"
  ],
  "response": "StationXML loaded from stations.xml; the detector removes the response only when it scores a window, and none was scored here"
}
```
> The committed replay fixtures are two vertical-component receivers each -- they were assembled in Prompt 1 to exercise the streaming plumbing, not to feed a multi-station discriminator. The M1 build's own waveform set lives under data/raw/ (DVC-tracked, gitignored) and is not present in a fresh clone.

### `lfh` — M2 single-force inversion (executed here, offline)

- outcome: **refused** (executed)
- summary: REFUSED: only 3 station(s) contributed, below the minimum of 5; 3 stations / 12 channels, azimuthal gap 317 deg, distance 11.80-14.66 deg, median pre-event SNR 3.52. serac does not publish a source location it cannot support. No location, no mass and no force history are reported for this event.

```json
{
  "azimuthal_gap_deg": 316.8678865576259,
  "config_hash": "4762e79e5879b7b2559a0f7d476dddd45fc2dd869858f7a5e023535ff1a0d988",
  "mass": null,
  "median_pre_event_snr": 3.517,
  "n_channels": 12,
  "n_stations": 3,
  "stations": [
    "G.WUS",
    "II.NIL",
    "KC.MRZ1"
  ],
  "status": "failed",
  "variance_reduction": null,
  "wall_clock_s": 0.146
}
```
> M2 produces no mass, so the runout surrogate has no release volume to be given: the cascade forecast for this event cannot be built from serac's own chain.
> Re-run in this session from the committed Green's fixtures.

### `runout` — M4 runout surrogate

- outcome: **not_reached** (unavailable)
- summary: not reached: the lfh stage (M2 single-force inversion (executed here, offline)) refused. Measured reason: REFUSED: only 3 station(s) contributed, below the minimum of 5; 3 stations / 12 channels, azimuthal gap 317 deg, distance 11.80-14.66 deg, median pre-event SNR 3.52. serac does not publish a source location it cannot support. No location, no mass and no force history are reported for this event.
> No substitute input was used. A default mass, location or footprint here would turn an upstream refusal into a forecast.

### `cap` — M5 CAP 1.2 generator

- outcome: **not_reached** (unavailable)
- summary: not reached: the detection stage (M1 discriminator (executed here)) did_not_fire. Measured reason: no candidate in either mode: the committed fixture carries 2 receiver(s) against the detector's minimum of 3 contributing stations, and no window was ever scored
> No substitute input was used. A default mass, location or footprint here would turn an upstream refusal into a forecast.

### `avoided_loss` — M5 avoided-loss computation

- outcome: **insufficient_input** (executed)
- summary: status=not_implemented; costed 0 of 14 exposed asset(s)

```json
{
  "determined": [],
  "lives_in_warned_zone": null,
  "status": "not_implemented",
  "undetermined": {
    "betrawati": "no_arrival",
    "chilime-hep": "no_arrival",
    "devighat-hep": "no_arrival",
    "miteri-bridge": "no_flow_depth",
    "rasuwagadhi-hep": "no_flow_depth",
    "rasuwagadhi-kerung-border-post": "no_flow_depth",
    "sanjen-hep": "no_transect",
    "sanjen-upper-hep": "no_transect",
    "syabrubesi": "no_arrival",
    "timure": "no_flow_depth",
    "trishuli-hep": "no_arrival",
    "upper-trishuli-1": "no_arrival",
    "upper-trishuli-3a": "no_arrival",
    "upper-trishuli-3b": "no_arrival"
  }
}
```
> Run on the best available input rather than on a forecast, because the chain produced no forecast. Every asset it could not cost is reported as undetermined, never as zero loss.

## Context (not part of the chain)

### `detection` — M1 discriminator

- outcome: **did_not_fire** (artifact)
- artifact: `reports/m1/latency_langtang-lhende-2026.json`
- summary: no mode fired: batch_600s, sliding_180s all returned no candidate on 12 receiver(s)

```json
{
  "budget_met": false,
  "modes": {
    "batch_600s": {
      "class_label": null,
      "compute_seconds_per_scored_window": null,
      "fired": false,
      "probability": null,
      "stream_latency_s": null,
      "theoretical_floor_s": 573.3333333333334
    },
    "sliding_180s": {
      "class_label": null,
      "compute_seconds_per_scored_window": null,
      "fired": false,
      "probability": null,
      "stream_latency_s": null,
      "theoretical_floor_s": 153.33333333333334
    }
  },
  "n_receivers": 12,
  "origin_utc": "2026-08-26T02:52:10Z"
}
```
> No mode fired on this event, so no latency was measured. This is reported as the result; it is not a budget pass.
> replayed from data/raw/discriminator/waveforms/pos_serac-langtang-lhende-2026.mseed (raw counts, ledgered by the M1 build); the detector's own response removal is inside the timed section
> model lgbm-3class trained under loro_hma; langtang-lhende-2026 is a forced test group and was not in training

### `runout` — M4 runout ensemble (frozen design prior)

- outcome: **produced** (artifact)
- artifact: `reports/runout/langtang_sanity.json`
- summary: 230 frozen members; 1 of 4 transect(s) reached by at least one member

```json
{
  "design_hash": "ce679a8f93002433a4ca8d8e4608e53208fba023ea1ac4943777f28484dae183",
  "solver_version": "0.2.0",
  "transects": [
    {
      "members_reaching": 0,
      "members_total": 230,
      "p50_min": null,
      "p5_min": null,
      "p95_min": null,
      "reach_fraction": 0.0,
      "transect_id": "betrawati"
    },
    {
      "members_reaching": 0,
      "members_total": 230,
      "p50_min": null,
      "p5_min": null,
      "p95_min": null,
      "reach_fraction": 0.0,
      "transect_id": "galchhi"
    },
    {
      "members_reaching": 45,
      "members_total": 230,
      "p50_min": 21.93,
      "p5_min": 15.654,
      "p95_min": 43.60799999999999,
      "reach_fraction": 0.1957,
      "transect_id": "rasuwagadhi-gyirong"
    },
    {
      "members_reaching": 0,
      "members_total": 230,
      "p50_min": null,
      "p5_min": null,
      "p95_min": null,
      "reach_fraction": 0.0,
      "transect_id": "syabrubesi"
    }
  ]
}
```
> This is the frozen ensemble's own arrival distribution over its Latin-hypercube design prior. It is a sampling design, NOT an estimate of the 26 August 2026 release, because M2 refused and no release volume for that event exists.
> Read from a whitelist of member keys; the press-comparison fields in this artifact are never read (serac.cascade.evidence.FORBIDDEN_SANITY_KEYS).

### `detection-case-study` — M1 discriminator single-window case study

- outcome: **produced** (artifact)
- artifact: `reports/m1/case_study_langtang-lhende-2026.json`
- summary: predicted class tectonic, calibrated p(mass movement)=0.36900604944410326

```json
{
  "below_the_datasets_quality_bar": true,
  "class_probabilities": {
    "mass_movement": 0.4469827152782939,
    "noise": 0.08927620803276472,
    "tectonic": 0.46374107668894127
  },
  "min_stations_required_by_the_dataset": 3,
  "receivers_selected": 12,
  "receivers_with_response_removed_data": 2
}
```
> This is a single-window case study, not a test-set metric, and it is never averaged into one. The window has 2 receiver(s) with response-removed data against the dataset's minimum of 3, so it was excluded from the built dataset and recorded as `not_fetched` with that reason. The threshold was deliberately NOT lowered to admit it: moving a data-quality threshold after discovering it excludes the headline event is post-hoc tuning. The model here is the already-trained, already-sealed one, applied unchanged.

## Avoided loss on the best available input

```
==============================================================================
INPUT PROVENANCE — read before reading a single figure below
==============================================================================
AOI                : lhende-khola-trishuli (Langtang Lirung - Lhende Khola - Bhote Koshi - Trishuli corridor)
Hazard input       : serac-swe-voellmy-ensemble-prior v0.2.0, provenance=simulator
                     This is the FROZEN ENSEMBLE'S DESIGN PRIOR, not a forecast of this event.
Exposure           : REAL — 14 asset(s) from data/aoi/lhende-khola-trishuli/exposed_assets.geojson, each feature sourced
Transects          : REAL — 4 (rasuwagadhi-gyirong, syabrubesi, betrawati, galchhi)
Damage functions   : ASSUMPTION — parametric, no cited source (serac.cascade.damage)
Replacement values : ASSUMPTION where derived; ABSENT for every non-hydropower asset
Warning benefit    : ASSUMPTION — stated ramps, no effectiveness study fetched
Lives in warned zone: NULL — no sourced population for any settlement in this AOI
------------------------------------------------------------------------------
NO VALIDATED FORECAST EXISTS FOR THIS EVENT. serac has no model validated against events (RELEASE_STATUS.md), and for this event the chain produced no forecast at all.
RESULT: INSUFFICIENT INPUT: the computation ran and costed 0 of 14 exposed assets. betrawati: the forecast carries no arrival at this asset's transect: the model does not reach it. That is a model output, NOT a statement that the asset is safe; chilime-hep: the forecast carries no arrival at this asset's transect: the model does not reach it. That is a model output, NOT a statement that the asset is safe; devighat-hep: the forecast carries no arrival at this asset's transect: the model does not reach it. That is a model output, NOT a statement that the asset is safe; miteri-bridge: the arrival at this asset's transect carries no peak stage, so there is no depth to put into a damage function; rasuwagadhi-hep: the arrival at this asset's transect carries no peak stage, so there is no depth to put into a damage function; rasuwagadhi-kerung-border-post: the arrival at this asset's transect carries no peak stage, so there is no depth to put into a damage function; sanjen-hep: the exposure record names no transect, so no arrival in the forecast can be attached to this asset; sanjen-upper-hep: the exposure record names no transect, so no arrival in the forecast can be attached to this asset; syabrubesi: the forecast carries no arrival at this asset's transect: the model does not reach it. That is a model output, NOT a statement that the asset is safe; timure: the arrival at this asset's transect carries no peak stage, so there is no depth to put into a damage function; trishuli-hep: the forecast carries no arrival at this asset's transect: the model does not reach it. That is a model output, NOT a statement that the asset is safe; upper-trishuli-1: the forecast carries no arrival at this asset's transect: the model does not reach it. That is a model output, NOT a statement that the asset is safe; upper-trishuli-3a: the forecast carries no arrival at this asset's transect: the model does not reach it. That is a model output, NOT a statement that the asset is safe; upper-trishuli-3b: the forecast carries no arrival at this asset's transect: the model does not reach it. That is a model output, NOT a statement that the asset is safe. Contract 0.0.0 has no 'insufficient_input' status, so this response uses 'not_implemented'; the computation is implemented and produced no numbers because it was given no usable input.
Missing from the exposure layer:
  - 14 of 14 exposed asset(s) carry no replacement value: the AOI exposure layer has no monetary field, so a value can only come from the caller or, for hydropower, be derived from installed capacity under a stated assumption.
  - 3 of 3 settlement(s) carry population=null (timure, syabrubesi, betrawati); no qualifying population source was fetched, so lives in a warned zone cannot be counted.
  - 2 asset(s) name no transect (sanjen-hep, sanjen-upper-hep), so no forecast arrival can be attached to them.
==============================================================================
```

### Scenario `no-warning`

| Asset | Type | Transect | Arrival | Lead time | Depth | Damage | Replacement | Expected loss | Avoided | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| `betrawati` | settlement | `betrawati` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `devighat-hep` | hydropower_plant | `betrawati` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `trishuli-hep` | hydropower_plant | `betrawati` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `upper-trishuli-1` | hydropower_plant | `betrawati` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `upper-trishuli-3a` | hydropower_plant | `betrawati` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `upper-trishuli-3b` | hydropower_plant | `betrawati` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `miteri-bridge` | bridge | `rasuwagadhi-gyirong` | 15.7 to 43.6 min | — | — | — | — | — | — | **undetermined** - no flow depth |
| `rasuwagadhi-hep` | hydropower_plant | `rasuwagadhi-gyirong` | 15.7 to 43.6 min | — | — | — | — | — | — | **undetermined** - no flow depth |
| `rasuwagadhi-kerung-border-post` | border_post | `rasuwagadhi-gyirong` | 15.7 to 43.6 min | — | — | — | — | — | — | **undetermined** - no flow depth |
| `timure` | settlement | `rasuwagadhi-gyirong` | 15.7 to 43.6 min | — | — | — | — | — | — | **undetermined** - no flow depth |
| `chilime-hep` | hydropower_plant | `syabrubesi` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `syabrubesi` | settlement | `syabrubesi` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `sanjen-hep` | hydropower_plant | — | — | — | — | — | — | — | — | **undetermined** - no transect |
| `sanjen-upper-hep` | hydropower_plant | — | — | — | — | — | — | — | — | **undetermined** - no transect |

### Scenario `serac-counterfactual-warning`

| Asset | Type | Transect | Arrival | Lead time | Depth | Damage | Replacement | Expected loss | Avoided | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| `betrawati` | settlement | `betrawati` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `devighat-hep` | hydropower_plant | `betrawati` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `trishuli-hep` | hydropower_plant | `betrawati` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `upper-trishuli-1` | hydropower_plant | `betrawati` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `upper-trishuli-3a` | hydropower_plant | `betrawati` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `upper-trishuli-3b` | hydropower_plant | `betrawati` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `miteri-bridge` | bridge | `rasuwagadhi-gyirong` | 15.7 to 43.6 min | 12.5 to 40.5 min | — | — | — | — | — | **undetermined** - no flow depth |
| `rasuwagadhi-hep` | hydropower_plant | `rasuwagadhi-gyirong` | 15.7 to 43.6 min | 12.5 to 40.5 min | — | — | — | — | — | **undetermined** - no flow depth |
| `rasuwagadhi-kerung-border-post` | border_post | `rasuwagadhi-gyirong` | 15.7 to 43.6 min | 12.5 to 40.5 min | — | — | — | — | — | **undetermined** - no flow depth |
| `timure` | settlement | `rasuwagadhi-gyirong` | 15.7 to 43.6 min | 12.5 to 40.5 min | — | — | — | — | — | **undetermined** - no flow depth |
| `chilime-hep` | hydropower_plant | `syabrubesi` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `syabrubesi` | settlement | `syabrubesi` | — | — | — | — | — | — | — | **undetermined** - model does not reach it |
| `sanjen-hep` | hydropower_plant | — | — | — | — | — | — | — | — | **undetermined** - no transect |
| `sanjen-upper-hep` | hydropower_plant | — | — | — | — | — | — | — | — | **undetermined** - no transect |

### Assumptions

1. ASSUMPTION: serac fetched no depth-damage curve, asset valuation or evacuation-effectiveness study for the Nepal/Tibet Trishuli corridor. Every damage function, replacement value and warning-benefit parameter in serac.cascade.damage is a stated parametric assumption with no cited source, and the monetary outputs inherit that status. They are not a loss estimate; they are what the stated parameters imply.
2. ASSUMPTION: where a caller supplies no replacement value, a hydropower plant is valued at 1.5-4 million USD per installed MW (2026 prices), with no central value because no qualifying source supports one. Bridges, settlements, border posts and roads get NO derived value at all: serac holds no asset-specific input for them (no span, no building count, no population), and a class-average figure applied to an asset serac knows nothing about would be a fabricated number. Those assets come back as 'undetermined', never as zero.
3. Lives in the warned zone are reported as null, not zero. Every settlement in data/aoi/lhende-khola-trishuli/exposed_assets.geojson carries population=null, because no qualifying source for a resident or transient population was fetched; the same is true of the border post and the bridge. serac therefore cannot count who is in a warned zone, and reporting a fatality figure of any kind -- including zero -- would be an invented number.
4. ASSUMPTION: damage function 'hydropower-headworks-v0' for hydropower_plant is 1-exp(-(d/d0)^1.2) with d0 in [1, 6] m (central 2.5 m). No source: run-of-river headworks (weir, intake, desilting basin) sit in the channel, so they are loaded by the first metre of a debris-laden flow; the small d0 reflects that, and the interval is wide because no fragility study for Himalayan run-of-river intakes was fetched
5. ASSUMPTION: damage function 'hydropower-powerhouse-v0' for hydropower_plant is 1-exp(-(d/d0)^1.5) with d0 in [2, 12] m (central 5 m). No source: powerhouses sit on a terrace above the normal channel and are built as reinforced concrete, so they tolerate more depth than the headworks before total loss; the steeper shape reflects the step from 'wet' to 'flooded to the machine hall'
6. ASSUMPTION: damage function 'bridge-v0' for bridge is 1-exp(-(d/d0)^2) with d0 in [1.5, 9] m (central 4 m). No source: a bridge is largely unaffected until the flow loads the deck or scours a pier, then fails over a narrow depth band; the shape exponent of 2 encodes that threshold behaviour. Deck clearance is the physically right variable and is not in the AOI record for any bridge here, so depth above the channel bed is used instead
7. ASSUMPTION: damage function 'settlement-v0' for settlement is 1-exp(-(d/d0)^1) with d0 in [2, 10] m (central 5 m). No source: aggregate building stock, mixed construction; a gentler exponent because a settlement is a mixture of structures that fail at different depths rather than a single structure with a single threshold
8. ASSUMPTION: damage function 'built-other-v0' for border_post/road/other is 1-exp(-(d/d0)^1) with d0 in [2, 10] m (central 5 m). No source: a catch-all for built assets with no class-specific reasoning; identical in form to the settlement function, which is itself an assumption, so this is an assumption about an assumption and should be replaced before any figure derived from it is used
9. ASSUMPTION: a run-of-river hydropower plant's replacement value is split 35% to hydropower-headworks-v0, 65% to hydropower-powerhouse-v0. No cost breakdown was fetched.
10. ASSUMPTION: for border_post, a warning avoids between 0% and 5% of the physical loss, ramping linearly from 10 min of lead time to 60 min. Treated as a settlement; serac holds no asset-specific basis for anything else. No study of protective action effectiveness on this corridor was fetched.
11. ASSUMPTION: for bridge, a warning avoids between 0% and 2% of the physical loss, ramping linearly from 5 min of lead time to 30 min. A bridge cannot be protected by a warning at all; the only avoidable physical loss is whatever is standing on it. The life-safety benefit of closing it is real and is not a monetary saving, so it does not appear in this share. No study of protective action effectiveness on this corridor was fetched.
12. ASSUMPTION: for hydropower_plant, a warning avoids between 2% and 15% of the physical loss, ramping linearly from 10 min of lead time to 60 min. A warning cannot move a weir, a waterway or a powerhouse. What it can do is trip the units, close gates, de-energise the switchyard, evacuate the machine hall and move vehicles and mobile plant, which protects a small share of the capital value and much of the restart cost. The share is small on purpose. No study of protective action effectiveness on this corridor was fetched.
13. ASSUMPTION: for other, a warning avoids between 0% and 5% of the physical loss, ramping linearly from 10 min of lead time to 60 min. Catch-all; replace before using any figure derived from it. No study of protective action effectiveness on this corridor was fetched.
14. ASSUMPTION: for road, a warning avoids between 0% and 2% of the physical loss, ramping linearly from 5 min of lead time to 30 min. Treated as a bridge: the asset itself cannot be protected by a warning. No study of protective action effectiveness on this corridor was fetched.
15. ASSUMPTION: for settlement, a warning avoids between 0% and 5% of the physical loss, ramping linearly from 10 min of lead time to 60 min. Buildings cannot be moved. Movable contents, livestock and vehicles can, and that is the whole of the monetary benefit. The benefit that matters for a settlement is lives, which serac cannot count here because no sourced population figure exists for any settlement in this AOI. No study of protective action effectiveness on this corridor was fetched.
16. This is the frozen runout ensemble's design prior, not a forecast of this event. The release volume, ice fraction, release band and friction parameters are Latin-hypercube samples from a design frozen before any comparison was made; nothing here is conditioned on the 26 August 2026 event, because M2 refused and no release volume for it exists.
17. Every Range carries best=null: a design prior has no central estimate.
18. No footprint polygon: the committed ensemble artifacts record chainage profiles and transect arrivals, not an inundation polygon.
19. No peak stage at any transect: see above. The avoided-loss engine therefore cannot evaluate a damage function and reports every asset as undetermined.
20. COUNTERFACTUAL alert-issue delay after origin: detection 153.3 s (M1's own theoretical floor for the causal sliding_180s mode (travel time to a >=100 km receiver plus 180 s of record minus the 60 s pre-origin lead-in). The measured latency is not used because the detector did not fire on this event) + LFH 4.2 s (M2's measured wall clock on this event (failed); a refusal is reached before the inversion runs, so a run that produced a mass would take longer) + surrogate 0.002 s + dissemination 30 s (ASSUMPTION) = 187.6 s. serac issued no alert for this event; this is what the measured stage numbers imply, not a delivered lead time.
21. The hazard input is serac-swe-voellmy-ensemble-prior, the frozen ensemble's own arrival distribution over its Latin-hypercube design prior. It is NOT a forecast of this event: M2 refused, so no release volume for this event exists.
22. The chain stopped at the detection stage. no candidate in either mode: the committed fixture carries 2 receiver(s) against the detector's minimum of 3 contributing stations, and no window was ever scored
23. Aggregation is comonotonic: interval endpoints are summed with endpoints, because every asset is costed with the same stated parameters and their errors move together.
24. 14 of 14 exposed asset(s) could not be costed and are reported as undetermined, not as zero loss: betrawati (no_arrival); chilime-hep (no_arrival); devighat-hep (no_arrival); miteri-bridge (no_flow_depth); rasuwagadhi-hep (no_flow_depth); rasuwagadhi-kerung-border-post (no_flow_depth); sanjen-hep (no_transect); sanjen-upper-hep (no_transect); syabrubesi (no_arrival); timure (no_flow_depth); trishuli-hep (no_arrival); upper-trishuli-1 (no_arrival); upper-trishuli-3a (no_arrival); upper-trishuli-3b (no_arrival)


## Caveats

- The avoided-loss stage was run on the frozen ensemble design prior, out of band with the chain, so that the exposure and the computation are exercised. It is not a chain output and must not be read as one.
- M4's measured surrogate inference latency is 1.72 ms (p95, CPU, batch 1), from reports/runout/surrogate_metrics.json. It is quoted for the latency budget only; the surrogate was never invoked in this run.

## What would have to change

The reason this chain stops is not a defect in the integration. Each stage refused, or failed to fire, for a measured physical reason that its own report states. Fixing the integration cannot move any of them. In the order the chain meets them:

1. **Detection here needs more than two receivers.** The committed replay fixtures carry two vertical-component stations each; they were assembled in Prompt 1 to exercise the streaming plumbing. The discriminator needs three contributing stations before it scores a window at all. Its own multi-station waveform set lives under `data/raw/` (DVC-tracked, absent from a fresh clone), and `reports/m1/latency_*.json` records what it did there.
2. **The inversion needs station geometry it does not have.** M2's refusals are about how many broadband receivers recorded the event, how they are distributed in azimuth, and whether the long-period signal is above the noise. More compute does not help; more instruments, closer, would.
3. **The runout model needs a mass.** Without one there is nothing to run the surrogate on, so no footprint, no arrival and no stage exist to alert on or to cost.
4. **The loss layer needs values and populations.** Even given a forecast, the committed exposure layer carries no replacement value for any asset and no population for any settlement. That gap is independent of the three above and is the cheapest to close.

