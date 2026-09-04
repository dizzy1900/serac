# End-to-end replay: Chamoli / Rishiganga, 7 February 2021

`serac cascade e2e --event chamoli-2021` on serac 0.1.0, run 2026-09-04T01:31:03.848747+00:00.

## Verdict

**The chain stops at the `detection` stage.** no candidate in either mode: the committed fixture carries 2 receiver(s) against the detector's minimum of 3 contributing stations, and no window was ever scored

No stage downstream of that point ran, and nothing was substituted for the missing input. serac produced **no cascade forecast and no CAP alert** for this event.

## Chain

| # | Stage | Component | Source | Outcome |
|---|---|---|---|---|
| 1 | `waveform` | committed seismic fixture | executed | **produced** |
| 2 | `detection` | M1 discriminator (executed here) | executed | **did_not_fire** |
| 3 | `lfh` | M2 single-force inversion | artifact | **refused** |
| 4 | `runout` | M4 runout surrogate | unavailable | **not_reached** |
| 5 | `cap` | M5 CAP 1.2 generator | unavailable | **not_reached** |
| 6 | `avoided_loss` | M5 avoided-loss computation | unavailable | **not_reached** |

## Stage detail

### `waveform` — committed seismic fixture

- outcome: **produced** (executed)
- artifact: `data/fixtures/seismic/chamoli-2021/manifest.json` (sha256 `9ed1ab9a6bf61b99…`, generated unknown)
- summary: 2 receiver(s) over 2021-02-07T04:49:00+00:00 to 2021-02-07T04:57:00+00:00

```json
{
  "stations": [
    "NK.KKN..BHZ",
    "IC.LSA.00.BHZ"
  ],
  "window_end_utc": "2021-02-07T04:57:00+00:00",
  "window_seconds": 480.0,
  "window_start_utc": "2021-02-07T04:49:00+00:00"
}
```
> Waveforms are the committed fixture chamoli-2021 (status fetched); licence None, see https://www.earthscope.org/terms-of-service/.

### `detection` — M1 discriminator (executed here)

- outcome: **did_not_fire** (executed)
- summary: no candidate in either mode: the committed fixture carries 2 receiver(s) against the detector's minimum of 3 contributing stations, and no window was ever scored

```json
{
  "modes": {
    "batch_600s": {
      "chunks_ingested": 193,
      "class_label": null,
      "compute_seconds_total": 0.0942,
      "fired": false,
      "min_contributing_stations": 3,
      "probability": null,
      "windows_scored": 0
    },
    "sliding_180s": {
      "chunks_ingested": 193,
      "class_label": null,
      "compute_seconds_total": 0.1059,
      "fired": false,
      "min_contributing_stations": 3,
      "probability": null,
      "windows_scored": 0
    }
  },
  "receivers_in_fixture": [
    "IC.LSA",
    "NK.KKN"
  ]
}
```
> The committed replay fixtures are two vertical-component receivers each -- they were assembled in Prompt 1 to exercise the streaming plumbing, not to feed a multi-station discriminator. The M1 build's own waveform set lives under data/raw/ (DVC-tracked, gitignored) and is not present in a fresh clone.

### `lfh` — M2 single-force inversion

- outcome: **refused** (artifact)
- artifact: `reports/m2/chamoli-2021.json` (sha256 `2e08350c8270c8f7…`, generated 2026-09-03T21:51:29.990768+00:00)
- summary: REFUSED: the best-fitting trial location explains only 0.089 of the data variance, below the floor of 0.20; 7 stations / 27 channels, azimuthal gap 180 deg, distance 6.43-11.43 deg, median pre-event SNR 0.70. A least-squares inversion of records that do not contain the signal still returns a smooth force history with a clean envelope, and an amplitude set by noise rather than by the event, so serac reports nothing. serac does not publish a source location it cannot support. No location, no mass and no force history are reported for this event. XR.BA20..LH1: peak amplitude 2.062e-04 m is 24x the median; dropped as a glitch XR.BA20..LH2: peak amplitude 4.838e-04 m is 55x the median; dropped as a glitch

```json
{
  "azimuthal_gap_deg": 179.51528076714573,
  "config_hash": "4762e79e5879b7b2559a0f7d476dddd45fc2dd869858f7a5e023535ff1a0d988",
  "mass": null,
  "median_pre_event_snr": 0.696,
  "n_channels": 27,
  "n_stations": 7,
  "stations": [
    "G.WUS",
    "IC.LSA",
    "II.NIL",
    "KC.ASAI",
    "XR.BA19",
    "XR.BA20",
    "XR.BN08"
  ],
  "status": "failed",
  "variance_reduction": null,
  "wall_clock_s": 33.798
}
```
> M2 produces no mass, so the runout surrogate has no release volume to be given: the cascade forecast for this event cannot be built from serac's own chain.

### `runout` — M4 runout surrogate

- outcome: **not_reached** (unavailable)
- summary: not reached: the lfh stage (M2 single-force inversion) refused. Measured reason: REFUSED: the best-fitting trial location explains only 0.089 of the data variance, below the floor of 0.20; 7 stations / 27 channels, azimuthal gap 180 deg, distance 6.43-11.43 deg, median pre-event SNR 0.70. A least-squares inversion of records that do not contain the signal still returns a smooth force history with a clean envelope, and an amplitude set by noise rather than by the event, so serac reports nothing. serac does not publish a source location it cannot support. No location, no mass and no force history are reported for this event. XR.BA20..LH1: peak amplitude 2.062e-04 m is 24x the median; dropped as a glitch XR.BA20..LH2: peak amplitude 4.838e-04 m is 55x the median; dropped as a glitch
> No substitute input was used. A default mass, location or footprint here would turn an upstream refusal into a forecast.

### `cap` — M5 CAP 1.2 generator

- outcome: **not_reached** (unavailable)
- summary: not reached: the detection stage (M1 discriminator (executed here)) did_not_fire. Measured reason: no candidate in either mode: the committed fixture carries 2 receiver(s) against the detector's minimum of 3 contributing stations, and no window was ever scored
> No substitute input was used. A default mass, location or footprint here would turn an upstream refusal into a forecast.

### `avoided_loss` — M5 avoided-loss computation

- outcome: **not_reached** (unavailable)
- summary: not reached: the detection stage (M1 discriminator (executed here)) did_not_fire. Measured reason: no candidate in either mode: the committed fixture carries 2 receiver(s) against the detector's minimum of 3 contributing stations, and no window was ever scored
> No substitute input was used. A default mass, location or footprint here would turn an upstream refusal into a forecast.

## Context (not part of the chain)

### `detection` — M1 discriminator

- outcome: **produced** (artifact)
- artifact: `reports/m1/latency_chamoli-2021.json`
- summary: fired in 2 of 2 mode(s); fastest sliding_180s at 209.99 s after origin, calibrated p=0.5267548717594978, class mass_movement

```json
{
  "budget_met": false,
  "modes": {
    "batch_600s": {
      "class_label": "mass_movement",
      "compute_seconds_per_scored_window": 13.637385790934786,
      "fired": true,
      "probability": 0.585423759711845,
      "stream_latency_s": 539.99,
      "theoretical_floor_s": 573.3333333333334
    },
    "sliding_180s": {
      "class_label": "mass_movement",
      "compute_seconds_per_scored_window": 9.113665937504265,
      "fired": true,
      "probability": 0.5267548717594978,
      "stream_latency_s": 209.99,
      "theoretical_floor_s": 153.33333333333334
    }
  },
  "n_receivers": 12,
  "origin_utc": "2021-02-07T04:51:18Z"
}
```
> The brief's 60 s budget is NOT met and is not reachable for this architecture. The fastest mode fired 210 s after origin against a theoretical floor of 153 s. The floor is set by travel time to a >=100 km receiver plus the record length a 20-100 s band requires; no amount of compute moves it. Reaching 60 s would need receivers inside 100 km and a shorter-period discriminant, which is a different component with different physics, not a faster version of this one.
> replayed from data/raw/discriminator/waveforms/pos_serac-chamoli-2021.mseed (raw counts, ledgered by the M1 build); the detector's own response removal is inside the timed section
> model lgbm-3class trained under loro_hma; chamoli-2021 is a forced test group and was not in training

## Caveats

- No frozen runout ensemble exists for the chamoli-rishiganga corridor: M4's ensemble was built for the Lhende Khola / Trishuli corridor only. There is therefore no best-available hazard input for this event at all, and the avoided-loss stage has nothing to run on.
- M4's measured surrogate inference latency is 1.72 ms (p95, CPU, batch 1), from reports/runout/surrogate_metrics.json. It is quoted for the latency budget only; the surrogate was never invoked in this run.

## What would have to change

The reason this chain stops is not a defect in the integration. Each stage refused, or failed to fire, for a measured physical reason that its own report states. Fixing the integration cannot move any of them.

