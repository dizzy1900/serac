# Model card — M2, landslide force-history inversion (`serac.models.lfh`)

**Version** 0.1.0 · **Contract** `force-history` 0.2.0 · **Status** implemented, three published
reproductions pass, no result yet for the motivating event.

Long-period single-force inversion after Ekström & Stark (2013) and Allstadt (2013): modelled
Green's functions from a 1-D Earth model in the 20–150 s band, a grid search over trial source
locations by variance reduction (gSF), a regularised least-squares inversion for a
three-component force history at the chosen node, and two mass estimators whose union is what
serac publishes.

## Intended use

Estimate, within a few minutes of a confirmed detection, where a large mass movement
detached, what net force it exerted on the Earth, how long it lasted, which way it went, and
roughly how much mass moved — **each as an interval**, and each only when the recording
geometry can support it.

## Out-of-scope use

- **Not a detector.** It runs on a window supplied by something else and assumes a mass
  movement occurred. It will fit a force history to a tectonic earthquake if handed one.
- **Not a volume estimate.** It estimates *mass*, through an effective acceleration that
  depends on assumed friction. Converting to volume needs a density nobody measured.
- **Not a substitute for a field or satellite survey.** The reproductions agree with published
  masses to within a factor of about 1.5 on the median and span an order of magnitude in the
  interval. That is useful for a warning decision and useless for an engineering one.
- **Not a small-event tool.** Below roughly 10⁷ m³, or beyond a few hundred kilometres of the
  nearest open long-period station, the signal is not in the records — see Chamoli below.
- The `status="failed"` outcome is a **result**, not a bug. It is the honest output whenever
  the geometry or the fit cannot carry a number.

## Data

| Input | What it is | Provenance |
|---|---|---|
| Waveforms | Real 1 sps LH? records, 9–10 open stations per event, 0.5–15°, −180 s to +840 s about origin | `provenance: real`, `source: fdsn_waveforms`; EarthScope, GEOFON, RESIF, INGV |
| Green's functions | **Modelled**, PREM (`prem_a_20s`) via EarthScope Syngine | `provenance: derived`, `source: iris_syngine`, `params.modelled: true` (ADR-0016) |
| DEM | Copernicus GLO-30 crops, three AOIs only | `provenance: real`, `source: dem_glo30` |
| Published figures | `data/references/lfh_published.json` | Fetched, hashed, DOI-resolved in session; each carries the verbatim sentence |

**Green's functions are never published on the bus.** As a `SeismicTrace` a modelled trace
would be indistinguishable from a recording downstream. `validate-lfh` enforces the isolation
structurally, by checking that no streaming, bus or domain module imports the machinery.

The 20–150 s band is why 1 sps channels suffice, and that is why the whole offline fixture set
is under a megabyte.

## Method

1. **Prepare.** Response removal to displacement, band-pass 20–150 s (4-pole, zero-phase),
   resample to 1 s, window −120 s to +780 s about origin. Displacement never touches the bus:
   `SeismicTrace.units` is `Literal["counts"]`.
2. **Select and check geometry.** Distance window, glitch rejection, then an azimuth-binned cap
   at 12 stations so a dense local cluster cannot outvote the station that closes the gap.
   **Refusal is decided here, before any inversion**, so a good-looking fit cannot argue it away.
3. **gSF.** 121 trial locations at 2 km spacing, coarse (5 s) force basis, λ fixed across the
   grid so nodes are compared like for like. Best variance reduction wins.
4. **Invert.** Full 1 s basis at the chosen node. Second-difference Tikhonov, **zero endpoints**
   (a mass movement starts and ends at rest, so its net force does too), λ from the L-curve
   corner and recorded.
5. **Mass, twice.** See below.
6. **Bootstrap.** 200 draws resampling stations (with replacement), band corners, λ, source
   depth and friction. Percentiles taken per sample, so the envelope brackets the median by
   construction.

### Two mass estimators, published as a union

| | A — `fmax_over_aeff` | B — `impulse_over_velocity` |
|---|---|---|
| Formula | `M = F_max / a_eff` | `M = max‖∫F dt‖ / (a_eff · t_acc)` |
| Geometry from | A trajectory-consistent solve on the DEM, or a published fall height and runout | The force history itself: `θ = atan(‖F_vert‖/‖F_horiz‖)` at peak horizontal force |
| External data | DEM or catalogue | **None** |

The DEM solve is a real use of the terrain: the force history integrates to a unit-mass path
whose every displacement scales as 1/M, so laying that path on the raster from the inverted
source location and requiring the modelled drop to match the ground's own drop closes the
system with one equation in one unknown.

They are **not independent**: both divide by an effective acceleration built from the same
friction range. They differ in the force functional (peak versus integral) and in where the
geometry comes from. The published interval is the **union**, never an average, and the
consistency ratio of the medians is reported whether or not it is flattering.

`MassEstimate` makes a point mass impossible to construct. That is a validator, not a
convention.

### Friction is a fraction of the slope, not an absolute coefficient

`a_eff = g sin θ (1 − φ)` with `φ = μ / tan θ ∈ [0.2, 0.8]`.

An absolute Coulomb coefficient can exceed `tan θ`, which describes a mass that cannot move.
Sampling one drives `a_eff` to a numerical floor and inflates the mass. Bingham Canyon, whose
Heim ratio is 0.29, sits below the middle of an unremarkable absolute range — the mass came out
**ten times too large** before this was fixed, with nothing in the output to say so.

## When serac refuses

Three conditions, all checked and all reported with the geometry stated:

| Condition | Threshold | Why |
|---|---|---|
| Too few stations | < 5 | A location from four stations is a number with no evidence behind it |
| Azimuthal gap | > 200° | The force azimuth is unconstrained across the gap |
| Fit quality | variance reduction < 0.20 | Records that do not contain the signal still invert to a smooth force history |

The variance-reduction floor refuses **more** than the brief asks. It was added after the
synthetic round trip showed a deliberately broken inversion returning a clean-looking answer at
VR = 0.11, and it was fixed at 0.20 when the failing runs sat at 0.065–0.089 and the passing
ones at 0.40–0.61. It can only ever make serac say less; it cannot make serac agree with a
figure it would otherwise disagree with.

## Results

### Published reproductions (three pass; the gate requires three)

| Event | Published mass (kg) | serac p05 / **p50** / p95 (kg) | Overlap | Median ÷ published centre | VR | Gap |
|---|---|---|---|---|---|---|
| Bingham Canyon 1, 2013 | 5.6–8.4 × 10¹⁰ (ESEC) | 1.6 × 10¹⁰ / **6.6 × 10¹⁰** / 2.7 × 10¹¹ | yes | **0.96** | 0.43 | 64° |
| Taan Fiord, 2015 | 1.0–1.5 × 10¹¹ (Higman 2018, seismic) | 1.1 × 10¹⁰ / **4.5 × 10¹⁰** / 2.0 × 10¹¹ | yes | **0.36** | 0.40 | 129° |
| Lamplugh Glacier, 2016 | 1.34–1.41 × 10¹¹ (ESEC) | 2.2 × 10¹⁰ / **1.9 × 10¹¹** / 1.2 × 10¹² | yes | **1.40** | 0.61 | 172° |
| Chamoli, 2021 | 26.5–27.3 × 10⁶ m³ → 5.9–6.5 × 10¹⁰ | **REFUSED** | — | — | 0.089 | 180° |

Two other published quantities are checked where they exist, both for Taan Fiord:

| Quantity | Published (Higman et al. 2018) | serac | Verdict |
|---|---|---|---|
| Peak force | "about 2 × 10¹¹ N" | 1.4 / **1.7** / 2.2 × 10¹¹ N | ratio 0.87 |
| **Runout bearing** | "an eastward-moving (**bearing 96°**) landslide" | **81 / 99 / 114°** | inside the interval, **3° from the median** |
| Duration | "lasting 90 seconds" | 293 / **296** / 297 s | **wrong — see below** |

The bearing is the strongest independent check M2 has. Nothing in the inversion is fitted to
a direction; it falls out of the three-component force history, compared as the force azimuth
rotated by 180° because a slide pushes the ground opposite the way it travels. Both ways the
Syngine force convention could have been wrong — a mirrored `F_north = -Ft`, or a flipped
transverse sign — would move it conspicuously, and neither would show up in a misfit.

**Duration is not a usable output.** serac returns 296 s against a published 90 s. The source
time series is 300 s long and the second-difference penalty spreads energy across it, so the
5%-of-peak threshold that defines duration simply reads back the window. It is reported in the
contract because the contract has a field for it; it should not be believed, and `duration_s`
is the one published quantity where serac and the literature disagree outright. Shortening the
source window would probably fix it, and doing so after seeing this number is exactly the
adjustment the seal exists to prevent — so it stands as a known defect for the next cycle.

Every median lands inside the magnitude sanity band [1/3, 3], so none of the three overlaps is
an artefact of interval width. Taan Fiord at 0.36 is close to the edge and should be read as a
weaker agreement than the other two.

### Chamoli 2021 — refused, and why it matters

Chamoli is in the brief as a reproduction target. serac cannot do it from open data. Its
event window is **quieter than its own pre-event noise**: median SNR 0.70 across 27 channels at
6.4–11.4°, and the best trial location explains 8.9% of the data variance. Cook et al. (2021)
worked from a dense regional network at ≤ 100 km; the open long-period stations within 15° of
Ronti are 51 in total, with a 180° gap, and a 27 × 10⁶ m³ event does not reach them.

Before the fit-quality floor existed, this same run returned a location, a duration and a mass
of 1.8 × 10¹² kg — thirty times the published-volume-derived figure — with a clean envelope and
a plausible waveform. That is the failure mode this component is built around.

### New events, under the sealed config

Both were run **after** `reports/m2/seal.json` recorded the config hash the reproductions
passed under, and `validate-lfh` checks that every run carries it.

| Event | Outcome | Geometry | Public figures |
|---|---|---|---|
| Langtang / Lhende Khola, 2026 | **REFUSED**: 3 stations, **317° gap** | Signal is present: median pre-event SNR **3.52**, and **57.3 at the best channel** (II.NIL) — the geometry, not the signal, is missing | 100–200 × 10⁶ m³ (ICIMOD via Kathmandu Post, preliminary) |
| Blatten / Birch Glacier, 2025 | **REFUSED**: VR 0.191 against a floor of 0.20 | 9 stations, 78° gap, median SNR 1.82 — good geometry, marginal fit | 9–10 × 10⁶ m³ (EGU 2026 abstracts) |

Blatten is a **marginal refusal** and is reported as one. The floor was set at 0.20 while
Blatten stood at 0.065, under a press-derived origin time. Correcting the origin to the Swiss
Seismological Service catalogue entry (13:24:26Z, event type *landslide* — five and a half
minutes before the "gegen 15.30 Uhr" in the event library, whose own stated uncertainty is
±900 s) lifted the fit to 0.191. The floor was **not** moved to let it through.

## Metrics

- **Reproduction**: interval overlap on mass, plus a magnitude ratio in [1/3, 3] (peak force,
  [1/2, 2]). 3 of 4 targets pass; the fourth refuses.
- **Fit**: variance reduction 0.40–0.61 on the three that pass.
- **Direction**: the one published bearing available is recovered to 3°.
- **Location resolution**: the radius containing every grid node within 0.02 of the best
  variance reduction, reported per event as `uncertainty_radius_km`. It is a resolution
  statement about the grid search, not a confidence interval.
- **Offline reproducibility**: `validate-lfh` re-inverts Taan Fiord from committed bytes with
  the network refused and recovers the reported peak force to **1.7%**.

## Latency

Measured on a 10-core Apple Silicon box. The machine was shared with other work during part of
this session, so per-stage `perf_counter` timings are quoted rather than contaminated
wall-clock totals.

| Stage | Seconds |
|---|---|
| Waveform preparation (response removal, filter, window) | 0.2–3.9 |
| gSF grid search, 121 nodes, coarse basis | 5.3–10.8 |
| Final full-resolution inversion incl. L-curve | 0.6–0.8 |
| DEM trajectory solve | < 0.1 |
| Bootstrap, 200 draws, **serial** | 65–70 |
| **Warm total (Green's library present)** | **~72–80** |
| Green's library, cold, 405 sets / 810 Syngine calls, 6 workers | 26 |
| Green's library, cold, full 0.5–15° at 0.05° (291 distances, 582 calls) | ~40 (measured 16.2 calls/s at 6 workers, 2.9 serial) |
| **Cold total** | **~115** |

**The ≤ 120 s target is met warm (~75 s) and, on this measurement, marginally met cold
(~115 s).** Two caveats, and they are the reason the deployment prerequisite below still
stands:

- The cold figure depends entirely on a third-party service answering in 60 ms per request. It
  was measured at 16 calls/s with six workers on uncached distances. Syngine's own endpoint
  proved intermittent during this session — the ESEC catalogue served 1 MB and then began
  404ing within the hour — and a service that is merely usually fast is not a latency budget.
- 90% of the warm cost is the bootstrap, which is 200 independent inversions run serially. It
  is embarrassingly parallel and untouched; on ten cores it should fall to under 10 s, putting
  the warm total near 20 s. That work was not done here and the number above is what was
  actually measured.

**Deployment prerequisite.** A per-AOI Green's-function library must be pre-built, or a local
`instaseis` database installed, before this component is put on a warning path. See
`infra/jobs/m2-greens-library.yaml`.

## Failure modes

1. **No signal in the records.** The dominant one. Caught by the variance-reduction floor;
   Chamoli and Blatten are both examples.
2. **Geometry too sparse.** Caught by the station-count and gap rules; Langtang is the example,
   and it is the motivating event.
3. **Regularisation bias.** The L-curve corner over-smooths. On the synthetic round trip it
   recovers the peak force at **0.36×** and the shape at r = 0.41, where a lightly regularised
   solution gives 1.09× and r = 0.81. The criterion is the one the brief specifies and was not
   changed after this was measured; the λ jitter in the bootstrap carries part of the spread
   into the published interval, but a systematic low bias in peak force — and therefore in
   mass — is possible and is not corrected for.
4. **Timing and geometry bookkeeping.** Three bugs of this kind occurred, none of which raised
   an error: a 120-sample Green's-function shift, a DEM profile drawn along the force azimuth
   instead of the direction of motion, and a naive great-circle station distance where Syngine
   uses geocentric latitude (a **36%** waveform misfit at 5°). All three now have tests.
5. **A single force is a model.** A slide that fragments, or one whose centre of mass turns
   sharply, is not one point force. Nothing here detects that.
6. **One Earth model.** PREM only. The bootstrap does not resample the Earth model, so
   whatever a different model would change is outside every interval quoted.
7. **Duration saturates the source window.** 296 s returned against 90 s published. See above.

## Limitations

- **Mass is not measured, it is inferred** through an assumed friction range on an assumed
  planar slope. Entrainment, fragmentation and changing basal resistance are all absent.
- **The two estimators share their friction assumption**, so their agreement is evidence but
  not proof, and where no DEM and no published geometry exist they share their geometry too —
  the code labels that case `DEGRADED` in `assumptions[]`.
- **Chamoli's comparison interval is derived, not published.** No paper retrieved in session
  gives a Chamoli mass. The 5.9–6.5 × 10¹⁰ kg comes from the published 26.5–27.3 × 10⁶ m³ and
  a 21:6 rock:ice split, converted at 2222–2381 kg/m³. The arithmetic is recorded; the density
  range is an assumption.
- **The bootstrap is a spread over analyst choices, not a posterior.** Its draws are not
  independent and it does not include model error.
- **Source duration is fixed at 300 s.** A prolonged multi-phase collapse — Blatten's Birch
  Glacier is plausibly one — may not be representable in that window. This was noticed after
  the config was sealed and was deliberately not changed.
- **Open long-period coverage of High Mountain Asia is the binding constraint**, not the
  method. Eleven open LH? stations within 15° of Langtang, of which three held data.

## Reproducing

```bash
uv run python scripts/build_lfh_references.py       # fetch, hash and DOI-resolve the sources
uv run python scripts/fetch_lfh_fixtures.py         # LH? waveforms for all six targets
uv run serac lfh greens build --workers 6           # ~40 s against Syngine
uv run serac lfh run-all                            # all six events
uv run serac lfh summary                            # the reproduction table
uv run serac validate lfh                           # 21 checks, offline
```

`make validate-lfh` runs the gate alone. Everything except the first three commands works with
no network.

## References

Every published figure in `data/references/lfh_published.json` was fetched, hashed and
DOI-resolved in session; eight of ten sources clear that bar, and the gate fails below three.

- Ekström & Stark (2013), *Science*, doi:10.1126/science.1232887 — the method.
- Higman et al. (2018), *Scientific Reports*, doi:10.1038/s41598-018-30475-w — Taan Fiord mass,
  peak force and duration.
- Hibert et al. (2014), *GRL*, doi:10.1002/2014gl060592 — Bingham Canyon single-force analysis.
- Pankow et al. (2014), *GSA Today*, doi:10.1130/gsatg191a.1 — Bingham Canyon context.
- Cook et al. (2021), *Science*, doi:10.1126/science.abj1227 — Chamoli regional detection.
- van Wyk de Vries et al. (2022), *NHESS*, doi:10.5194/nhess-22-3309-2022 — Chamoli volume.
- EarthScope ESEC, e.g. doi:10.17611/DP/14768841 — catalogue masses.
- Swiss Seismological Service, doi:10.12686/sed/networks/ch — the Blatten origin.
