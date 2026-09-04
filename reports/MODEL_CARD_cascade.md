# Model card — M5 cascade: avoided-loss computation and CAP alerting

> **No damage function, replacement value or warning-benefit parameter in this component has a
> cited source.** Every one is a stated parametric assumption, declared
> `provenance=assumption` in `src/serac/cascade/damage.py` and repeated verbatim in
> `AvoidedLossResponse.assumptions[]`. serac fetched no vulnerability curve, asset valuation or
> evacuation-effectiveness study for this corridor. The monetary outputs are what the stated
> parameters imply; they are not a loss estimate.

> **On both end-to-end replays the chain stops before a forecast exists.** serac produced no
> cascade forecast and no CAP alert for Chamoli 2021 or Langtang 2026. See
> `reports/e2e/chamoli-2021.md` and `reports/e2e/langtang-lhende-2026.md`.

| | |
|---|---|
| Component | M5 — avoided-loss accounting and alerting |
| Code | `src/serac/cascade/`, `src/serac/alerting/`, `src/serac/adapters/alerting/`, `src/serac/pipelines/e2e.py` |
| Contracts | `AvoidedLossRequest` / `AvoidedLossResponse` v0.0.0, `CAPMessage` v0.1.0 |
| Gate | `make validate-e2e` (`src/serac/validation/e2e.py`) |
| Alerting | CAP 1.2, enveloped Ed25519 XML-Signature (RFC 9231), file and HTTP POST sinks |
| Reports | `reports/e2e/{chamoli-2021,langtang-lhende-2026}.{md,json}`, `reports/e2e/latency.json` |

## Intended use

Given a `CascadeForecast` and an exposure schedule, produce **expected loss with no warning
against expected loss with the lead time serac would deliver**, per asset and per transect,
every figure an interval; and turn the same forecast into a signed, schema-valid CAP 1.2
message with per-transect ETAs.

The realistic user is a downstream decision or financial layer that supplies its own exposure
and its own replacement values through `AvoidedLossRequest` and reads
`AvoidedLossResponse` — never by importing serac internals. `serac cascade avoided-loss
--request <file>` is that entry point.

## Out-of-scope use

* **Not a loss estimate for any real event.** No damage function is sourced and no asset in
  either committed AOI carries a replacement value. A figure this component produces from
  serac's own data is the output of stated assumptions applied to a hazard input that is
  itself unvalidated.
* **Not an underwriting model, a rating basis, or a reserve calculation.** The intervals do
  not represent a loss distribution: they are the span implied by the stated parameter ranges,
  summed comonotonically across assets.
* **Not a life-safety count.** Expected fatalities and lives-in-warned-zone are always `null`.
  Every settlement in `data/aoi/lhende-khola-trishuli/exposed_assets.geojson` carries
  `population: null`, so there is no population at risk to multiply by anything.
* **Not an operational warning system.** A CAP message serac produces is `status: Test` or
  `Exercise` under the current confidence tiers. serac is not a warning authority in any
  jurisdiction and has no dissemination path.
* **Not evidence about the 180 s design budget.** No end-to-end latency was measured, because
  no CAP message was produced.

## The damage functions, and their sources

One saturating family, five parameter sets:

    fraction(d) = 1 - exp(-(d / d0) ** p),  clamped to [0, 1], fraction(0) = 0

`d` is flow depth at the asset in metres. The loss interval uses the large `d0` for its low end
and the small `d0` for its high end, so it brackets the parameter uncertainty.

| Function | Applies to | `d0` (high-damage / central / low-damage), m | `p` | Source |
|---|---|---|---|---|
| `hydropower-headworks-v0` | hydropower_plant | 1.0 / 2.5 / 6.0 | 1.2 | **none — assumption** |
| `hydropower-powerhouse-v0` | hydropower_plant | 2.0 / 5.0 / 12.0 | 1.5 | **none — assumption** |
| `bridge-v0` | bridge | 1.5 / 4.0 / 9.0 | 2.0 | **none — assumption** |
| `settlement-v0` | settlement | 2.0 / 5.0 / 10.0 | 1.0 | **none — assumption** |
| `built-other-v0` | border_post, road, other | 2.0 / 5.0 / 10.0 | 1.0 | **none — assumption** |

The reasoning behind each shape is in the `rationale` field of the corresponding
`DamageFunction` and is qualitative: headworks sit in the channel and are loaded by the first
metre; a powerhouse sits on a terrace; a bridge is unaffected until the deck is loaded and then
fails over a narrow band (hence `p = 2`); a settlement is a mixture of structures failing at
different depths (hence `p = 1`). A run-of-river plant is split 35% headworks / 65% everything
downstream — also an assumption, and also unsourced.

**Why nothing is cited.** `CLAUDE.md`'s citation rule requires a document retrieved and hashed
in-session before a number may claim a source. No depth-damage curve for Himalayan run-of-river
hydropower, Nepali highway bridges or Nepali settlement building stock was fetched, and the
generic flood curves that do exist in open form describe a different asset class in a different
country. Applying one and calling it a source would be worse than the honest marker.

## Replacement values

* A value supplied by the caller in `ExposureItem.replacement_value` always wins.
* Otherwise, a **hydropower plant** with a sourced installed capacity is valued at
  **1.5–4.0 million USD per MW (2026 prices)**, with `best: null`. That is an assumption.
* Otherwise the asset's value is **undetermined**. Bridges, settlements, border posts and roads
  get no derived value at all: serac holds no span, no building count and no population for any
  of them, and a class average applied to an asset serac knows nothing about is a fabricated
  number, not an estimate.

## Warning benefit

How much of an asset's physical loss a warning of lead time *L* avoids, ramping linearly
between a threshold and a saturation lead time. All assumptions.

| Asset type | Avoidable share | Ramp (min) | Reasoning |
|---|---|---|---|
| hydropower_plant | 2–15% | 10 → 60 | trip the units, close gates, de-energise, evacuate the hall, move mobile plant. The civil works cannot be moved. |
| bridge | 0–2% | 5 → 30 | a warning cannot protect a bridge; only what is standing on it. The life-safety benefit of closing it is real and is not a monetary saving. |
| settlement | 0–5% | 10 → 60 | movable contents, livestock, vehicles. Buildings cannot be moved; the benefit that matters is lives, which serac cannot count. |
| border_post / road / other | 0–5% | 5–10 → 30–60 | catch-alls; replace before using any figure derived from them. |

## What the chain actually did

`reports/e2e/*.md` carry the full timeline. In summary, on the committed repository:

| Stage | Chamoli 2021 | Langtang 2026 |
|---|---|---|
| waveform (committed fixture) | produced — 2 receivers, 8 min window | produced — 2 receivers, 8 min window |
| detection, M1 executed here | **did not fire** — 2 receivers vs a 3-station minimum; no window was ever scored | **did not fire** — same |
| detection, M1's own recorded run | fired: `sliding_180s` at 210 s after origin, p = 0.585, class `mass_movement` | **no mode fired at all** on 12 receivers |
| LFH, M2 | **refused** — variance reduction 0.089 vs a 0.20 floor, 7 stations, median pre-event SNR 0.70 | **refused** — 3 contributing stations vs a minimum of 5, azimuthal gap 317° |
| runout, M4 | not reached | not reached |
| CAP, M5 | not reached | not reached |
| avoided loss, M5 | not reached (no frozen ensemble for this corridor) | ran on the frozen ensemble **design prior**; costed **0 of 14** assets |

**Both chains stop at detection on the committed fixtures**, and both would stop at M2's
refusal even with M1's full receiver set. Langtang is the stricter case: M1's own recorded run
on 12 receivers did not fire in either mode, so the chain has two independent stopping points
before a forecast.

### The Langtang table, and why it is empty

`make underwriting-check` runs the computation for the Lhende AOI on the best input serac has:
the frozen runout ensemble's design prior (230 members, design hash `ce679a8f…`, hashed before
any comparison was made). It is a sampling design conditioned on nothing — **not** a forecast
of the 26 August 2026 event, for which no release volume exists.

0 of 14 assets could be costed:

* **10 assets** sit at `syabrubesi`, `betrawati` or no transect at all. No ensemble member
  reaches `syabrubesi`, `betrawati` or `galchhi` (0 of 230 each). They are reported
  `undetermined`, **not** zero: 87.4% of this corridor's thalweg is below 4.57°, which biases
  the solver against reaching anywhere, and the real cascade reached ~100 km.
* **4 assets** sit at `rasuwagadhi-gyirong`, which 45 of 230 members reach, with modelled
  arrivals of 15.7–43.6 min after origin. The committed ensemble artifacts record arrival
  times and not stages, so there is no flow depth to put into a damage function. Filling it
  with a deposit depth measured at a nearby constriction would be a substitution, not an
  estimate, and is refused.

The counterfactual alert-issue delay, assembled from measured stage numbers (M1's own
theoretical floor of 153.3 s for the causal `sliding_180s` mode + M2's measured wall clock +
M4's measured 1.7 ms surrogate inference + a stated 30 s dissemination allowance) is **187.6 s
for Langtang and 217.1 s for Chamoli**. Against the ensemble prior's arrivals at
`rasuwagadhi-gyirong` that would have been **12.5–40.5 min of lead time** — had a forecast
existed, which it did not. That number is a counterfactual and is never called a delivered
lead time.

## The CAP generator

Every field is derived by a stated rule, and the rule that fired is written into the message as
a `parameter` so a recipient can see why they were told what they were told.

| Field | Rule |
|---|---|
| `status` | `unqualified → Test`, `low → Exercise`, `medium → Exercise`, `high → Actual`. A `stub`-provenance forecast is forced to `Test` regardless. `Actual` is reserved for `high`, which no serac model reaches. |
| `scope` | follows `status`: only `Actual` is `Public`; `Test` and `Exercise` are `Private`, addressed to the operators list. |
| `urgency` | earliest modelled arrival: ≤ 30 min `Immediate`, ≤ 180 min `Expected`, else `Future`; `Unknown` when no transect is reached. |
| `severity` | largest modelled peak stage through a threshold table (5 / 2 / 0.5 m). **An assumption**, stated as such in `serac:severity_rule`. `Unknown` when no stage was modelled. |
| `certainty` | from the tier. `Observed` is never emitted: serac forecasts, it does not observe. |
| `area` | emitted only when the forecast has a footprint or reaches a transect. A footprint becomes a CAP `polygon` (`lat,lon`; interior rings are dropped because CAP cannot express a hole); reached transects become `geocode` entries. **No geometry is invented.** |
| `parameter` | per-transect ETA, absolute earliest/latest UTC, peak stage, peak discharge, delivered lead time (negative when the flow arrives first), damming, secondary surges, and every forecast assumption. |

Signing: an enveloped `ds:Signature` (exclusive C14N, SHA-256 digest, Ed25519 per RFC 9231) is
appended as the last child of `alert`, which the vendored CAP 1.2 XSD admits through its
`xmldsig` wildcard. A signed message still validates against the XSD — asserted in
`validate-e2e` and in the unit tests. Key handling is documented in `docs/CREDENTIALS.md`.

Sinks: `AlertSink` (`src/serac/ports/alert_sink.py`) with a file/log adapter and an HTTP POST
adapter. **Nothing is sent anywhere by default.** The HTTP sink cannot be constructed without
an absolute endpoint, is disabled unless explicitly enabled, and imports `requests` inside
`deliver` so the offline suite fails mechanically if a test ever enables it.

## Failure modes

1. **Silent substitution.** The failure this component is designed against. If someone later
   gives the loss engine a default depth, a class-average value or a nominal footprint when the
   real one is absent, the response becomes a forecast-shaped object with no forecast behind
   it. The guard is that a missing input produces `undetermined` with `blocked_by`, and the
   test `test_an_asset_with_no_usable_input_is_never_zero` asserts it.
2. **Undetermined read as safe.** A reader who skims the table and sees no number at
   `betrawati` may conclude there is no exposure there. Every rendering therefore lists every
   asset with the reason, and the reason for `no_arrival` says in words that it is a model
   output and not a statement that the asset is safe.
3. **A green gate read as a working system.** `validate-e2e` passes while the chain produces
   nothing, because an early stop is the outcome to record rather than a harness failure. The
   suite's own `how_to_read_this_suite` check and its `chain_produced_a_forecast` warnings say
   so; the module docstring says so; this card says so.
4. **Comonotonic aggregation.** Totals sum interval endpoints with endpoints. That is the
   widest honest bound for parameters that move together, and it is **not** a convolution of
   independent uncertainties. Read as one it would look like a distribution and is not.
5. **The assumption set drifting into being treated as calibrated.** The parameters are
   round numbers with reasoning, not estimates. `damage.py` marks each with
   `ASSUMPTION`, and the marker is asserted per function in the unit tests.
6. **Signature theatre.** The signature proves that the holder of one key produced these bytes.
   With no certificate chain, no revocation and no trust store, it does not prove who that
   holder is. A recipient who treats a valid signature as authorisation is mistaken.
7. **The prior mistaken for a forecast.** `serac-swe-voellmy-ensemble-prior` is a sampling
   design. Its arrival distribution says what the solver does over the design, not what
   happened. Every `Range` it produces carries `best: null` for exactly this reason, and the
   printed header names it as a design prior on the line above the first number.

## Limitations inherited from upstream

The loss layer is only as good as its hazard input, and its hazard input currently does not
exist. In descending order of how much they matter:

* **No mass.** M2 refuses Langtang, Chamoli and Blatten on station geometry, SNR and variance
  reduction. Without a mass there is no release volume, so the surrogate cannot be run for a
  real event at all.
* **No reach.** M4's ensemble reaches one of four Lhende transects, and its own write-up
  measures why: 87.4% of the thalweg lies below the slope a Voellmy Coulomb coefficient above
  0.08 can sustain motion on.
* **No stage.** The committed M4 artifacts record arrival times, not stages at transects, so
  even the reached transect has no depth.
* **No exposure values, no populations.** The AOI layer has neither. This is the cheapest of
  the gaps to close and the one that most directly limits what the component can say.
* **No detection.** M1 classifies Langtang as `tectonic` (0.464 vs 0.447) on the two receivers
  with response-removed data, and did not fire in either streaming mode.

## Reproducing

```
serac cascade e2e --event chamoli-2021
serac cascade e2e --event langtang-lhende-2026
serac validate e2e            # writes reports/validation/e2e.json and reports/e2e/latency.json
serac underwriting-check      # computes and prints the Lhende table
serac alerting keygen --out secrets/cap-signing.pem
serac alerting cap --sign --signing-key secrets/cap-signing.pem
```

All of these run offline against committed fixtures and artifacts. Nothing is transmitted.
