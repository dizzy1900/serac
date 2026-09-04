# Prompt 2 — what the modelling core actually shows

Written for a reader who assumes this is oversold and is looking for the catch. Every figure
below is read from a committed artefact under `reports/`; where a number could not be sourced,
it is not here. Nothing in this document is a performance claim.

---

## 1. The result

**Every one of serac's five model components returned a negative or a refusal on the motivating
event — Langtang Lirung / Lhende Khola, 26 August 2026 — for four unrelated physical reasons.**

| Component | Outcome on Langtang | Cause |
|---|---|---|
| **M1** discriminator | Classified `tectonic` (0.464) over `mass_movement` (0.447). Fired in neither streaming mode. | 2 of 12 selected receivers had response-removed data in the open archives eight days on |
| **M2** force-history inversion | **Refused.** No location, no mass, no force history. | 3 contributing stations against a minimum of 5; **317° azimuthal gap** |
| **M3** slope watch | **Mixed, not a null.** 5 of 48 source-zone units measurable at 38 of 122 steps; 4 stayed Quiet, 1 reached Elevated. | 72 % of the AOI outside a single Sentinel-1 burst footprint |
| **M4** runout | 45 of 230 ensemble members reach the first transect; **0 of 230** reach the other three. | 87.4 % of the corridor thalweg lies below **4.57°** |
| **M5** avoided loss / CAP | Chain stops at detection. **No forecast, no CAP message.** Table costs **0 of 14** assets. | No input from M1, M2 or M4 |

These are four different walls. They do not share a fix, they do not share a cause, and none of
them is "the model needs more training".

`make validate-serac` is red because `validate-discriminator` reports two unmet criteria of the
brief. `make promote` is blocked. That is the intended behaviour and it is documented in
`RELEASE_STATUS.md`. CI is green because it runs `ruff`, `mypy --strict` and 1,225 offline
tests — code health, not model skill.

---

## 2. "The method failed" versus "the data could not support the method"

This distinction is the single most valuable output of this prompt, and it cuts three ways to
one.

### The data could not support the method (three of the four)

**M2 — geometry.** The inversion is not broken. It reproduces three published force histories
under a sealed configuration. It refuses Langtang because eleven open long-period stations exist
within 15° of the event and **three held data**, leaving a 317° azimuthal gap across which the
force azimuth is unconstrained. A denser open network in Nepal, Tibet and northern India would
change this outcome and nothing in the code would need to move.

**M3 — footprint and line-of-sight geometry.** Langtang is *not* coherence-limited in the area
it images: median temporal coherence over imaged pixels is **0.622**, and 83.9 % of them clear
0.40. The limit is that a Sentinel-1 subswath is ~85 km wide and the AOI is a 100 km corridor,
so **19,385 of 26,935 units (72 %)** are outside the processed footprint. Chamoli's limit is
different again and equally geometric: the labelled unit faces west (aspect 271°) with a signed
LOS sensitivity of **−0.074** on the chosen ascending track — the horizontal approach almost
exactly cancels the vertical recession. A second track fixes both. No model change does.

**M1 — archive coverage.** Eight days after the event, the open archives held usable
response-removed data for 2 of the 12 receivers the dataset build selected. The window fell
below the dataset's 3-receiver minimum and was excluded and recorded `not_fetched` — the
threshold was deliberately **not** lowered to admit it, because moving a data-quality threshold
after discovering it excludes your headline event is post-hoc tuning. Whatever a classifier can
do, it cannot do it without records.

### The method failed (one of the four)

**M4 — the rheology is wrong for this cascade.** This one is not a data problem and should not
be excused as one. A single-phase depth-averaged Voellmy-Salm rheology cannot travel this
corridor: **87.4 % of the thalweg lies below 4.57°** (median 0.42° over 499 binned segments at
30 m), the slope below which a Coulomb coefficient of 0.08 stops being able to drive the flow —
and 0.08 is well inside the published range for rock-ice avalanches and debris flows. Ensemble
reach is median 13.88 km, max 28.72 km, against a corridor whose furthest transect is at
97.0 km and a real cascade reported to have travelled ~100 km. More members, a finer grid or a
better surrogate would change none of that. What is needed is multi-phase physics that can put a
fluid-rich front ahead of a solid-rich body — which is exactly what this solver states it cannot
represent.

### And one that is neither: a physics floor

**M1's ≤ 60 s latency budget is unreachable, and no amount of engineering reaches it.** Measured
on Chamoli: **210 s** (`sliding_180s`) and **540 s** (`batch_600s`), against theoretical floors
of 153 s and 573 s. The floor is travel time to a ≥ 100 km receiver plus the record length a
20–100 s band requires. Reaching 60 s needs receivers inside 100 km and a shorter-period
discriminant — a different component with different physics, not a faster version of this one.

---

## 3. The numbers, with their n

None of these is a performance claim. Read every one with its interval and its denominator.

**M1 discriminator**, leave-one-region-out with High Mountain Asia held out — 56 test windows
over 9 groups, **9 positives**:

| metric | value [95 % CI, 2000 group-level bootstrap resamples] |
|---|---|
| mass_movement F1 | **0.516 [0.333, 0.692]** |
| precision | 0.364 [0.222, 0.529] |
| recall | 0.889 [0.625, 1.000] |
| ROC-AUC | 0.868 [0.631, 0.991] |
| ECE | 0.1932 |

An F1 of 0.516 on nine positives is a number whose interval contains a great deal. The
time-forward fold is smaller still (7 positives, F1 0.375 [0.075, 0.714]). The deep model did
not beat the baseline: **ΔF1 −0.016 [−0.205, +0.163]**, which is inconclusive rather than
negative — at this number of held-out positives it could not have been anything else.

And a leak vector survives all ten of the suite's leakage assertions: positives realise on
average **+1.01** more receivers than their own matched negatives, and `n_stations` alone gives
**ROC-AUC 0.587**. Some of the skill above may be archive density rather than source physics.

**M2 inversion** — three published reproductions overlap by interval; the gate requires three:

| Event | Published mass | serac median ÷ published centre | VR | Gap |
|---|---|---|---|---|
| Bingham Canyon 2013 | 5.6–8.4 × 10¹⁰ kg | 0.96 | 0.43 | 64° |
| Lamplugh Glacier 2016 | 1.34–1.41 × 10¹¹ kg | 1.40 | 0.61 | 172° |
| Taan Fiord 2015 | 1.0–1.5 × 10¹¹ kg | **0.36** | 0.40 | 129° |

Overlap is a weak test: the serac intervals span an order of magnitude, and Taan Fiord at 0.36
sits close to the [1/3, 3] sanity edge. The one genuinely independent check is stronger than the
masses: Taan Fiord's published runout bearing of 96° against serac's **81 / 99 / 114°** — 3°
from the median, on a quantity nothing in the inversion was fitted to. Two ways the Syngine
force convention could have been wrong would have moved it conspicuously and would not have
shown up in a misfit.

One published quantity disagrees outright: `duration_s` returns **296 s** against a published
90 s, because the 300 s source window and the second-difference penalty spread energy across the
whole window. It is reported and should not be believed.

**M4 surrogate** — 4 of 5 gates pass:

| Gate | Measured | Target | Pass |
|---|---|---|---|
| Median inundation IoU at 1 m | 0.966 | ≥ 0.70 | yes |
| Arrival MAE, worst scored transect | 46.5 s | ≤ 90 s | yes |
| p95 inference latency | 0.0017 s | ≤ 2 s | yes |
| 5–95 % depth coverage (wet bins) | 0.914 | 0.85–0.95 | yes |
| **5–95 % arrival coverage** | **0.794** | 0.85–0.95 | **no** |

The arrival gate that passes rests on **3 held-out members at one transect**; three of four
transects scored nothing at all. A surrogate that reproduces a solver well is not evidence about
the world — it is evidence about the solver, and this solver has an 8.7 % low bias in terminal
velocity at the production CFL plus a release emplaced at rest ~1,300 m below the real
detachment. Both make every modelled arrival late.

**M5** — 0 of 14 Lhende assets costed. 10 sit at transects no member reaches; 4 sit at the one
transect 45 of 230 members reach, but the committed artifacts record arrival times and not
stages, so there is no depth for a damage function. All 14 are reported `undetermined`. Every
damage function, replacement value and warning-benefit share is an unsourced assumption — 27 of
them are printed with every run. There is no population figure for any settlement in the AOI, so
lives-in-warned-zone is `null` everywhere.

---

## 4. What genuinely worked

The models did not. Four other things did, and they are the reason the negative results above
can be trusted.

**Refusal machinery.** M2 checks station count, azimuthal gap and fit quality **before** any
inversion runs, so a good-looking fit cannot argue a refusal away. The variance-reduction floor
of 0.20 refuses more than the brief asks, and it earns its keep: before it existed, Chamoli
returned a location, a duration and a mass of **1.8 × 10¹² kg** — thirty times the
published-volume-derived figure — with a clean envelope and a plausible waveform. A floor that
can only ever make serac say less is the cheapest safety property in this repository. The same
idea appears in M5: an asset with no usable input comes back `undetermined` with a
`blocked_by` reason, never zero, and a test asserts it.

**Anti-tuning seals.** M1 seals its configuration and bumps `seal_version` when the test set is
re-scored. M2 recorded a config hash under which the reproductions passed, *before* Langtang and
Blatten were run, and `validate-lfh` checks every run carries it. M4 froze its ensemble design
hash before the Langtang comparison and `validate-runout` greps the write-ups for calibration
vocabulary. M3 pre-registered its thresholds and `validate-watch` proves against git that the
pre-registration commit is an ancestor of the backtest commit and was never edited afterwards.
None of this makes a model better. All of it makes a reported number harder to fake.

**The gates and the review caught four real defects, all of which pointed the same way — toward
a cleaner, stronger-looking story than the truth.**

1. **A demonstrated leak that passed all ten leakage assertions.** The suite proves group
   inheritance, split disjointness, forced-group placement, feature-name hygiene, calibration
   isolation and byte identity. None of them catches the fact that a positive window realises
   ~1 more receiver than its matched negatives, and that `n_stations` alone scores ROC-AUC
   0.587. It is disclosed as a live residual rather than asserted away.
2. **An overstated slope statistic that flattered its own conclusion.** Seven places said "92 %
   of this corridor's thalweg is below 6.8 degrees". The committed measurement is **87.4 % below
   4.57°** — at the μ = 0.08 threshold the sentence was actually arguing about. 92 % was
   measured at μ = 0.12 and was never committed anywhere. Every write-up now renders the number
   and its threshold from `terrain.json`.
3. **A quantifier inversion that turned a mixed result into a clean null.** M3's source-zone
   code set each unit's insufficiency reason from the last reason found *anywhere* in its
   history and then treated a unit as measurable only when that field was absent — silently
   meaning "measurable at **every** step" while the prose said "at **any** step". Langtang
   reported "0 of 48 source-zone units measurable" directly above a table showing one unit
   measurable at 38 of 122 steps. The real answer is messier: 43 never measurable, 5 measurable,
   4 Quiet, 1 Elevated. The defect surfaced exactly where the honest answer was more
   complicated.
4. **A generator that would emit a public evacuation alert with no geographic area.** CAP
   consumers route on `scope` and `responseType`, so an `Actual`/`Public` alert carrying
   `Evacuate` with no `<area>` is a public evacuation instruction with nothing to instruct
   about. `area_for` already declined to invent geometry; refusing to publish was the missing
   other half of that rule.

**Provenance discipline that survived contact with inconvenience.** 265 M1 windows were recorded
`not_fetched` with their reason and excluded — never substituted or backfilled. 517 HyP3 product
zips were hashed on arrival, cropped and deleted, and the ledger says so rather than pretending
they can be re-hashed. When the M1 test set had to be scored a second time after a float32
overflow fix, the superseded result was kept and **every metric moved in the unfavourable
direction** — which is the only condition under which a second scoring is defensible.

---

## 5. What surprised me

**The binding constraint was almost never the model.** Four components, four independent walls,
and only one of them (M4's rheology) is a modelling choice. Going in, the expectation was that
v0 baselines would be weak. They are — but their weakness is not what stopped anything.

**Langtang's InSAR archive is well correlated.** The expected story for a Himalayan rock-ice
source zone is C-band decorrelation over snow and ice. Chamoli fits that story (AOI median
temporal coherence 0.139, and it is decorrelated across the *whole* AOI, not only at altitude:
the median barely moves between the 0–3,000 m band at 0.134 and the 5,000–5,500 m band at
0.133). Langtang does not: median 0.622, 83.9 % above 0.40. Its problem is swath width. That
inverts the intuition about which fix matters — for this corridor, a second track buys more than
a longer wavelength would.

**The corridor's own slope, not the source volume, decides reach.** The Latin hypercube swept
release volume over 5–300 × 10⁶ m³. It did not matter: 87.4 % of the thalweg is too flat for the
rheology, so no volume in that range travels the corridor. A parameter sweep can only explore
what the physics admits.

**The anti-tuning seal did not trip on a behaviour-changing bug fix.** It hashes named constants
— feature names, window parameters, split rules, hyperparameters — and not code. The float32 fix
changed the trained model, changed the test predictions, and left `config_hash()` untouched. The
re-seal was a manual version bump, not an automatic detection. A seal over constants is
protection against hyperparameter tuning between scorings and nothing more, and I had been
reading it as more.

**The refusal was worth more than the answer.** M2's most useful single output this cycle was
not a mass. It was a demonstration that the *same* code, on the *same* Chamoli records, returned
a clean-looking 1.8 × 10¹² kg before a fit-quality floor existed. Without that floor the
component would have produced confident numbers for every event serac cares about, and every one
would have been wrong.

**Every review finding pointed toward a stronger-looking conclusion.** None of the four defects
in §4 made a result look worse than it was. That is not coincidence; it is what unexamined
narrative pressure does, and it is an argument for keeping the veto role rather than trusting
self-review.

---

## 6. What an operational Himalayan pilot would need

Nothing here is compute-bound. Every scaled job manifest in `infra/jobs/` is either trivial or
affordable, and none has ever been executed. The constraints are data access, physics and
partnerships, in that order.

### Data

| Need | Why, with the measurement |
|---|---|
| **Broadband station geometry in Nepal / Tibet / northern India** | M2 needs ≥ 5 contributing stations and ≤ 200° azimuthal gap. Langtang gave 3 and 317°. Eleven open LH? stations exist within 15° and three held data. |
| **Restricted regional network access** (Nepal NSC, China CEA, India NCS) | The highest-leverage single item. It is a partnership, not a purchase, and it would move M1 and M2 together. Note the physics floor: even dense regional coverage inside 100 km changes the discriminant, not the 20–100 s band's record-length requirement. |
| **A second Sentinel-1 track over the Lhende corridor** | 72 % of the AOI is outside the single processed burst footprint, and 2,983 units fall below the LOS-sensitivity floor. One track cannot watch a valley with slopes on both aspects. |
| **GACOS or ERA5 tropospheric correction** | Currently MintPy `height_correlation`, which removes only the elevation-correlated delay. Turbulent monsoon wet delay survives it, and it is the largest known error source in M3's velocities. Needs a CDS key or the GACOS email workflow — neither was available. |
| **Sediment thickness along the corridor** | `erodible_depth` is a 5 m parametric mantle tapered above 35°. No survey exists. Entrainment is the term most likely to change reach. |
| **Channel cross-sections at the Bhote Koshi gorge** | The gorge is under 60 m wide in places, fewer than two cells at 30 m. Superelevation, run-up and blocking are unresolved, and every damming number depends on them. |
| **A mapped detachment outline** | The source zones in `data/aoi/*/source_zone.geojson` are hand-digitised rectangles at 1,000 m positional accuracy. M3's labelled unit is only as good as that polygon. |
| **Exposure valuation and population** | 14 of 14 Lhende assets carry no replacement value; 3 of 3 settlements carry `population: null`. This is the cheapest gap in the whole project and it is the one that makes every M5 output `undetermined`. |
| **Depth-damage curves for the actual asset classes** | Himalayan run-of-river headworks and powerhouses, Nepali highway bridges, Nepali settlement stock. All five current functions are unsourced assumptions. Bridge fragility needs deck clearance, which no AOI record holds. |
| **A landslide-dam inventory for the corridor** | The damming logistic's midpoint and scale were chosen, not estimated. |
| **A real-time hydrometric feed** | Nepal DHM gauges have no stable open API; the adapter reads an ICIMOD-derived fixture. Independent confirmation of a passing surge is the cheapest possible validation of a live warning. |
| **More High Mountain Asia positives** | The entire headline F1 rests on 9. ESEC holds five HMA events. A regional force-history catalogue would matter more to M1 than any architecture change. |

### Compute

All figures from the committed manifests. All are estimates with a stated basis; none has been
executed.

| Job | Estimate | Note |
|---|---|---|
| `runout-ensemble-10k.yaml` | **917 core-hours**, ~5 h wall at 200 shards × 8 vCPU, 4 GiB outputs, arm64, no GPU | The manifest's own note: 10⁴ buys a denser Latin hypercube, not a better solver. Cross-validation against r.avaflow would be a better use of the same money. |
| `fno-train-gpu.yaml` | **3 GPU-hours** extrapolated (60 CPU-hours fallback) | A denser ensemble tightens the surrogate's fit to the solver and does nothing about the solver's structural error, which is the dominant term. |
| `m2-greens-library.yaml` | **0.02–0.4 core-hours**, ≤ 50 MB | Measured. The constraint is service latency and availability, not compute: the cold path depends on Syngine answering at 16 calls/s, and Syngine proved intermittent. A local `instaseis` database is a deployment prerequisite. |
| `hyp3-insar-batch.yaml`, `s1-stack.yaml` | 1–10 and 0.5–8 core-hours; 10–200 and 5–500 GB | Assumptions, download-bound. Needs Earthdata Login. |

There is also cheap local work with real value that was not done: M2's bootstrap is 200
independent inversions run **serially** and accounts for ~90 % of the warm cost. Parallelising it
on ten cores should take the warm total from ~75 s to near 20 s.

### Partners

- **A regional seismic network operator or consortium.** The one thing that unblocks two
  components at once.
- **A hazard agency with a mandate** (DHM Nepal, ICIMOD, or an equivalent on the Tibetan side).
  serac is not a warning authority in any jurisdiction, has no dissemination path, and should
  never acquire one on its own.
- **A multi-phase runout group** (r.avaflow's authors, or a RAMMS / DAN3D team). r.avaflow could
  not be obtained — no official GRASS addon, no canonical public repository, avaflow.org behind a
  registration wall — so there is currently **no independent simulator** against which to
  separate `serac-swe-voellmy`'s structural bias from implementation error.
- **A hydropower operator on the Trishuli**, for installed capacities, replacement values, deck
  clearances and a SCADA endpoint the `AlertSink` HTTP adapter could actually talk to.
- **ESA / ASF**, for a second track and for the tasking that a real watch cadence needs.
- **A field or remote-sensing group** to map detachment outlines and post-event deposits.

### What would have to be true that is not true now

1. **A mass estimate exists for at least one recent High Mountain Asia event.** Today: none. M2
   refused all three it was given.
2. **A rheology that can carry the flow ~100 km down this corridor exists in the tree.** Today:
   single-phase Voellmy stops at ≤ 28.72 km, and the corridor's slope distribution says it must.
3. **The detector fires on the motivating event.** Today: neither streaming mode fires, and the
   sealed model orders the classes the wrong way round on the receivers that exist.
4. **The ≤ 60 s budget is either abandoned or replaced with a different discriminant.** It is
   unreachable for a 20–100 s band at ≥ 100 km, and no roadmap should carry it forward as-is.
5. **The stub is actually retired.** `serac replay --detector discriminator` now runs the
   trained model, but `stub` is still the default and `serac stream run` — the live lane —
   remains hardcoded to `DetectorStub`. On the Chamoli fixture the trained detector emits
   **0 detections against the stub's 4**, because two receivers are available and it requires
   three: selectable is not the same as retired.
6. **Somebody with a mandate would act on the output.** Nothing in this repository can create
   that, and nothing in it should pretend to.

---

## 7. What serac must never claim

Restated, and extended by what Prompt 2 measured.

1. **That serac predicts the day or hour of a bedrock collapse from satellites.** L1 is a
   probabilistic watch state. No schema or report serac writes contains a failure date, window
   or time-to-failure, and `validate-watch` fails the build if one appears.
2. **That any component is validated against events.** None is. `validate-discriminator` fails
   on an unmet criterion; every other component's `validated-against-events` cell in
   `RELEASE_STATUS.md` is `no` or `partial`.
3. **That M1 would have prevented the "M4.4 earthquake" misreport.** On the evidence that
   existed, it puts `tectonic` above `mass_movement` and fires in neither streaming mode.
4. **That a passing gate means a working system.** `validate-e2e` passes while the chain
   produces no forecast and no CAP message at all. `validate-runout` passes on a solver with no
   independent cross-validation.
5. **That flow depths or arrival times come from r.avaflow.** They come from
   `serac-swe-voellmy` v0.2.0, implemented here, single-phase, uncross-validated.
6. **That the ≤ 180 s detachment-to-CAP latency is proven.** No end-to-end latency has ever been
   measured, because no CAP message was ever produced. The 187.6 s and 217.1 s figures in the
   reports are counterfactuals assembled from per-stage measurements and are never delivered
   lead times.
7. **That a monetary figure from M5 is a loss estimate.** Every damage function, replacement
   value and warning-benefit share is an unsourced assumption. An `undetermined` asset is not a
   zero-loss asset.
8. **That a Quiet or absent watch tier means a slope is safe.** A unit outside the burst
   footprint or below the LOS-sensitivity floor is not being watched at all, and brittle
   crystalline failure can occur with no resolvable precursory displacement.
9. **That "no precursor was detected" describes the Chamoli backtest.** Nothing in that source
   zone was measurable at any step. The tier was never in a position to be asked.
10. **That data are real where they are synthetic, that a dataset was fetched when the manifest
    says otherwise, or that a service was verified live when it was only exercised against a
    fixture.** The ledger is the arbiter, not the prose.

---

*Generated 2026-09-04, last revised at `3dbe7e7`. Sources: `reports/MODEL_CARD_{discriminator,lfh,watch,runout,cascade}.md`, `reports/validation/*.json`, `reports/m1/`, `reports/m2/`, `reports/runout/`, `reports/watch/`, `reports/e2e/`, `infra/jobs/*.yaml`.*
