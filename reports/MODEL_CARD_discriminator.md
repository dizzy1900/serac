# Model card — M1 seismic mass-movement discriminator

Separates long-period single-force mass-movement signals from double-couple tectonic earthquakes and from noise, on regional broadband records. It is the component built to catch the failure that produced the "M4.4 earthquake" misreport of 26 August 2026.

**It does not yet catch that failure.** On the Langtang window as the open archives actually hold it, this model puts `tectonic` marginally above `mass_movement`. The case study below gives the numbers and the reason. Chamoli 2021, which had twelve receivers rather than two, is classified correctly in the held-out fold. Read the metrics below as what a v0 baseline achieves on the events that had coverage, not as a claim about the event this project exists for.

## Intended use

- Near-real-time triage of a regional broadband array: *what kind of source made this?*
- A gate in front of M2's force-history inversion, so an inversion is not attempted on an earthquake.

## Out-of-scope use

- **It does not locate.** `source_location` is always null. Locating is M2's job.
- **It does not estimate volume, mass or runout.**
- **It is not a time-of-failure predictor** and says nothing about a slope before it fails.
- **It must not be run on raw counts.** `require_response=True` raises rather than score counts, because the model would return a confident, meaningless probability.
- It has not been evaluated outside the regions in the table below, and High Mountain Asia is represented by a very small number of events.

## Data

- **1,925 windows** over **308 event groups**: mass_movement 297, tectonic 1,332, noise 296
- 600 s windows from origin-60 s, instrument response removed to velocity, 0.005-5.0 Hz, 20 Hz, up to 12 receivers at 100-1500 km, azimuth-binned.
- Requested: 323 positives, 1,544 tectonic negatives, 323 noise windows across 1,062 unique receivers.
- Written: 1,925 of 2,190 windows; positives 297 of 323. **265 windows recorded `status: not_fetched`** with their reason and excluded — never substituted, backfilled or replaced.
- Bytes fetched: 2.62 GB.
- Store pinned by chunk index sha256 `9564bc6e36b4354bb0ade99512fe957ab7623cf2e41e46776a491c437ed6d4ff` over 1,929 files.

Sources: ESEC (IRIS/EarthScope SPUD, 319 events 1977-2024, committed verbatim as a fixture), USGS ComCat `eventtype=landslide`, and the serac event library. ComCat's landslide set is **57 events since 2000, only 6 with M>=4, mostly Alaska ml 1-2, and Chamoli 2021 is absent from it** — it could not have carried this component alone.

### Counts by class x region x decade

| region | decade | mass_movement | tectonic | noise |
|---|---|---|---|---|
| Alaska and Yukon | 2000s | 20 | 92 | 20 |
| Alaska and Yukon | 2010s | 42 | 189 | 42 |
| Alaska and Yukon | 2020s | 12 | 69 | 12 |
| Andes | 2010s | 1 | 5 | 1 |
| Caucasus | 2020s | 1 | 5 | 1 |
| Eastern North America | 2020s | 1 | 1 | 1 |
| European Alps | 2000s | 18 | 81 | 18 |
| European Alps | 2010s | 47 | 223 | 47 |
| European Alps | 2020s | 12 | 60 | 12 |
| High Mountain Asia | 2000s | 1 | 6 | 1 |
| High Mountain Asia | 2010s | 7 | 23 | 6 |
| High Mountain Asia | 2020s | 1 | 10 | 1 |
| Mediterranean and Anatolia | 2020s | 1 | 5 | 1 |
| New Zealand | 2010s | 1 | 5 | 1 |
| North American Cordillera | 1990s | 1 | 12 | 0 |
| North American Cordillera | 2000s | 22 | 83 | 22 |
| North American Cordillera | 2010s | 58 | 271 | 58 |
| North American Cordillera | 2020s | 43 | 162 | 44 |
| Other / unassigned | 2000s | 3 | 14 | 3 |
| Other / unassigned | 2010s | 2 | 10 | 2 |
| Other / unassigned | 2020s | 1 | 0 | 1 |
| Scandinavia and Iceland | 2010s | 2 | 6 | 2 |

## Features

79 features, computed **only** from the Zarr `waveform` and `valid` arrays: long-period/short-period energy ratios, envelope duration and emergence, spectral centroid drift, horizontal/vertical energy ratio, long-period rectilinearity and cross-receiver envelope coherence, each aggregated across a window's receivers by median, median absolute deviation and 90th percentile.

**No feature encodes geometry, epoch or identity.** A test fails the build if any feature name contains `lat`, `lon`, `distance`, `azimuth`, `year`, `magnitude`, `depth`, `station`, `network`, `sncl`. No geometry-derived feature is kept, so there is no ablation to report: incidence angle and back-azimuth-corrected polarisation were considered and rejected, because with ~320 positives epicentral distance is close to a primary key and a model given it can identify events rather than physics.

## Metrics

Intervals are 95% percentile bootstrap over **test event groups** (2000 resamples), not over windows: a group contributes one positive plus its matched negatives and noise, all cut at the same receivers, so resampling windows would treat several views of one event as independent observations.

### Leave-one-region-out, High Mountain Asia held out (**headline**)

56 test windows over 9 groups, **9 positives**.

| metric | value [95% CI] |
|---|---|
| mass_movement F1 | 0.516 [0.333, 0.692] |
| mass_movement precision | 0.364 [0.222, 0.529] |
| mass_movement recall | 0.889 [0.625, 1.000] |
| macro F1 | 0.637 [0.473, 0.792] |
| ROC-AUC (mass_movement vs rest) | 0.868 [0.631, 0.991] |
| Brier | 0.1294 |
| ECE | 0.1932 |

Confusion matrix:

| actual \\ predicted | mass_movement | tectonic | noise |
|---|---|---|---|
| mass_movement | 8 | 1 | 0 |
| tectonic | 11 | 25 | 3 |
| noise | 3 | 0 | 5 |

Per-region confusion matrices (denominators are small; they are printed because hiding them would be worse, not because a region with one positive means anything):

**High Mountain Asia**

| actual \\ predicted | mass_movement | tectonic | noise |
|---|---|---|---|
| mass_movement | 8 | 1 | 0 |
| tectonic | 11 | 25 | 3 |
| noise | 3 | 0 | 5 |

Forced test groups:

- `chamoli-2021`: {"present_in_test": true, "n_windows": 7, "positive_detected": true, "positive_probability": [0.5295011642057127], "positive_predicted_class": ["mass_movement"]}
- `langtang-lhende-2026`: {"present_in_test": true, "n_windows": 5, "positive_detected": false, "positive_probability": [], "positive_predicted_class": []}
- `us7000tbwb`: {"present_in_test": false}
- `us7000tc90`: {"present_in_test": false}

Reliability bins (mean predicted probability vs observed frequency):

| bin | n | mean p | observed |
|---|---|---|---|
| 0.0-0.1 | 11 | 0.046 | 0.000 |
| 0.1-0.2 | 9 | 0.165 | 0.000 |
| 0.2-0.3 | 9 | 0.250 | 0.111 |
| 0.3-0.4 | 6 | 0.349 | 0.000 |
| 0.4-0.5 | 9 | 0.447 | 0.333 |
| 0.5-0.6 | 7 | 0.543 | 0.143 |
| 0.6-0.7 | 3 | 0.610 | 1.000 |
| 0.7-0.8 | 2 | 0.741 | 0.500 |

### Time-forward (train <2020, val 2020-2023, test 2024-2026)

50 test windows over 8 groups, **7 positives**.

| metric | value [95% CI] |
|---|---|
| mass_movement F1 | 0.375 [0.075, 0.714] |
| mass_movement precision | 0.333 [0.051, 0.833] |
| mass_movement recall | 0.429 [0.122, 0.800] |
| macro F1 | 0.647 [0.463, 0.831] |
| ROC-AUC (mass_movement vs rest) | 0.814 [0.691, 0.912] |
| Brier | 0.1069 |
| ECE | 0.0897 |

Confusion matrix:

| actual \\ predicted | mass_movement | tectonic | noise |
|---|---|---|---|
| mass_movement | 3 | 0 | 4 |
| tectonic | 4 | 32 | 0 |
| noise | 2 | 0 | 5 |

Per-region confusion matrices (denominators are small; they are printed because hiding them would be worse, not because a region with one positive means anything):

**Alaska and Yukon**

| actual \\ predicted | mass_movement | tectonic | noise |
|---|---|---|---|
| mass_movement | 0 | 0 | 1 |
| tectonic | 0 | 5 | 0 |
| noise | 0 | 0 | 1 |

**European Alps**

| actual \\ predicted | mass_movement | tectonic | noise |
|---|---|---|---|
| mass_movement | 0 | 0 | 1 |
| tectonic | 2 | 3 | 0 |
| noise | 1 | 0 | 0 |

**High Mountain Asia**

| actual \\ predicted | mass_movement | tectonic | noise |
|---|---|---|---|
| mass_movement | 1 | 0 | 0 |
| tectonic | 2 | 8 | 0 |
| noise | 1 | 0 | 0 |

**North American Cordillera**

| actual \\ predicted | mass_movement | tectonic | noise |
|---|---|---|---|
| mass_movement | 2 | 0 | 2 |
| tectonic | 0 | 16 | 0 |
| noise | 0 | 0 | 4 |

Forced test groups:

- `chamoli-2021`: {"present_in_test": true, "n_windows": 7, "positive_detected": true, "positive_probability": [0.5969070753479709], "positive_predicted_class": ["mass_movement"]}
- `langtang-lhende-2026`: {"present_in_test": true, "n_windows": 5, "positive_detected": false, "positive_probability": [], "positive_predicted_class": []}
- `us7000tbwb`: {"present_in_test": false}
- `us7000tc90`: {"present_in_test": false}

Reliability bins (mean predicted probability vs observed frequency):

| bin | n | mean p | observed |
|---|---|---|---|
| 0.0-0.1 | 25 | 0.028 | 0.000 |
| 0.1-0.2 | 6 | 0.179 | 0.333 |
| 0.2-0.3 | 7 | 0.245 | 0.286 |
| 0.3-0.4 | 4 | 0.350 | 0.000 |
| 0.4-0.5 | 4 | 0.460 | 0.250 |
| 0.5-0.6 | 4 | 0.582 | 0.500 |

## Deep model versus baseline

Promotion rule, fixed before either model was trained: The challenger becomes default only if the paired-bootstrap 95% lower bound on delta F1 exceeds 0. Fixed before either model was trained.

- cnn-station-transformer F1 0.500, lgbm-3class F1 0.516
- **delta F1 = -0.016 [-0.205, +0.163]** over 2000 group resamples
- **Default: the lightgbm baseline (retained).**
  The lower bound does not exceed zero, so the comparison is inconclusive rather than negative. At this number of held-out positives it could not have been anything else; that is the finding, not a failure.

Deep model: 120,091 parameters, best epoch 5 of 13, device `mps`, validation macro F1 0.682. No positional encoding on the receiver axis, so it is permutation-invariant and cannot key on slot order.

Deep on the held-out region: mass_movement F1 0.500 [0.300, 0.640], ROC-AUC 0.863 [0.739, 0.954].

## Detection latency

### chamoli-2021

| mode | fired | stream latency | theoretical floor | compute per scored window | p |
|---|---|---|---|---|---|
| `batch_600s` | True | 540 s | 573 s | 13.6 s | 0.585 |
| `sliding_180s` | True | 210 s | 153 s | 9.1 s | 0.527 |

**Verdict.** The brief's 60 s budget is NOT met and is not reachable for this architecture. The fastest mode fired 210 s after origin against a theoretical floor of 153 s. The floor is set by travel time to a >=100 km receiver plus the record length a 20-100 s band requires; no amount of compute moves it. Reaching 60 s would need receivers inside 100 km and a shorter-period discriminant, which is a different component with different physics, not a faster version of this one.

### langtang-lhende-2026

| mode | fired | stream latency | theoretical floor | compute per scored window | p |
|---|---|---|---|---|---|
| `batch_600s` | False | - | 573 s | - | - |
| `sliding_180s` | False | - | 153 s | - | - |

**Verdict.** No mode fired on this event, so no latency was measured. This is reported as the result; it is not a budget pass.

## Case study: Langtang / Lhende Khola, 26 August 2026

This is the event M1 exists for. Eight days after it happened, only **2 of 12 selected receivers** (`IN.SHL..H`, `IO.EVN..B`) had data in the open archives at 100-1500 km, against the dataset's minimum of 3. The window was therefore **excluded from the dataset** and recorded as `not_fetched` with that reason, so it contributes to no metric above. The nearest open broadband, NK.KKN at ~55 km, is inside the 100 km floor and is excluded by design.

The receiver threshold was **not** lowered to admit it. Moving a data-quality threshold after discovering it excludes the headline event is post-hoc tuning, and it is the kind this component was built to refuse. Instead the already-trained, already-sealed model was applied to the window as it is:

| class | probability |
|---|---|
| mass_movement | 0.447 |
| tectonic | 0.464 |
| noise | 0.089 |

**Predicted class: `tectonic`. Calibrated P(mass movement) = 0.369.**

**On this evidence M1 would not have prevented the 'M4.4 earthquake' misreport.** It puts `tectonic` marginally above `mass_movement` (0.464 against 0.447) — the two are nearly tied and the ordering is the wrong way round. That is the result, and it is reported as the result. Two caveats belong with it and neither rescues it: the window has two receivers where the model was trained on three or more, and a two-receiver window cannot support the cross-receiver coherence and azimuthal spread the feature set leans on. The honest reading is that M1 v0 needs regional coverage this event did not have in the open archives within eight days, not that it succeeded.

By contrast Chamoli 2021, which had twelve receivers, is classified correctly in the held-out fold under both split schemes.

## Failure modes

1. **High Mountain Asia is thinly represented.** ESEC holds five HMA events; with the serac event library the held-out fold has **nine positives**. Every HMA number in this card has an interval wide enough to contain a great deal, and no point estimate should be quoted without its interval.
2. **The time-forward test fold is tiny.** ESEC's last event is 2024, so a 2024-2026 test window has a handful of events. Leave-one-region-out is the headline for that reason.
3. **Negatives are not magnitude-matched.** ESEC publishes no magnitude, so negatives are matched on receiver set, epicentral proximity and epoch inside a fixed M4.0-6.5 band. If mass movements systematically differ in size from that band, some of what the model separates may be amplitude rather than mechanism.
4. **The noise class means 'no catalogued source', not 'quiet'.** Uncatalogued sources, small teleseisms and cultural noise are all in it.
5. **A truncated window is out of distribution.** `sliding_180s` asks the model about 180 s of record zero-padded to 600 s, which it never saw in training. Its scores are reported next to the batch scores, not instead of them.
6. **Regional coverage is what the open archives hold.** Alaska, the European Alps and the North American Cordillera dominate the positives because that is where open broadband networks and the ESEC compilers' attention are, not because mass movements are commonest there.
7. **Events with no open coverage are absent, and their absence is recorded.** They are counted above and appear in `data/manifest.jsonl` as `not_fetched` rows with reasons.
8. **Thin coverage on a recent event is the binding constraint, not model skill.** Langtang 2026 had two usable open receivers eight days after the event. Whatever the classifier can do, it cannot do it without records.
9. **A response gap silently narrows a window.** Receivers whose response could not be read are dropped; a window below three usable receivers is excluded entirely.

## Provenance and anti-tuning

- Baseline `lgbm-3class` 0.1.0, scheme `loro_hma`, 1,504 training windows over 239 groups, best iteration 60.
- Model sha256 `49e15139d3af4efa706b894cebbb8f5578953c38873dbdf781d0903aeccf009d`.
- Training groups sha256 `222b8b6be979121eb88867355217c09e58643eb54a7b8db9ab2e3696830c572f`; `validate-discriminator` recomputes it from the split, so the shipped model proves what it was trained on.
- Calibration: sigmoid, fitted on `val` only (n=365).
- Anti-tuning seal `b3aa4d1925c401ae65ab777b584151729c0e1f54f77a1abfcec81c6ab9482729` sealed at 2026-09-03T20:37:22.847996Z; schemes evaluated under it: ['loro_hma', 'time_forward']. A test evaluation under a changed configuration is refused.

Chamoli 2021 and the Langtang 2026 pair are forced into the test fold under both schemes and appear in neither training nor validation, including for early stopping and for the calibrator.

