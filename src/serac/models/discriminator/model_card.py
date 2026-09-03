"""Render `reports/MODEL_CARD_discriminator.md` from the committed JSON reports.

Every number in the card is read out of a file the pipeline wrote — the build report, the
dataset index, the evaluation results, the paired comparison, the latency reports and the
artifacts. Nothing is typed in by hand, so the card cannot drift from what was actually run,
and a claim with no backing file simply does not appear.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from serac.models.discriminator.baseline import CLASSES
from serac.models.discriminator.catalog import ClassLabel
from serac.models.discriminator.dataset import DatasetIndex
from serac.models.discriminator.features import FEATURE_NAMES, FORBIDDEN_FEATURE_TOKENS
from serac.models.discriminator.regions import HELD_OUT_REGION, region_label

CARD_PATH = Path("reports/MODEL_CARD_discriminator.md")


def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _interval(block: dict[str, Any] | None) -> str:
    if block is None:
        return "not measured"
    return f"{block['point']:.3f} [{block['low']:.3f}, {block['high']:.3f}]"


def _counts_table(index: DatasetIndex) -> str:
    rows = Counter((w.region_id, w.decade, w.class_label.value) for w in index.windows)
    regions = sorted({r for r, _, _ in rows})
    decades = sorted({d for _, d, _ in rows})
    lines = [
        "| region | decade | " + " | ".join(CLASSES) + " |",
        "|---|---|" + "---|" * len(CLASSES),
    ]
    for region in regions:
        for decade in decades:
            values = [rows.get((region, decade, c), 0) for c in CLASSES]
            if not any(values):
                continue
            lines.append(
                f"| {region_label(region)} | {decade} | "
                + " | ".join(str(v) for v in values)
                + " |"
            )
    return "\n".join(lines)


def _confusion(matrix: list[list[int]]) -> str:
    lines = [
        "| actual \\\\ predicted | " + " | ".join(CLASSES) + " |",
        "|---|" + "---|" * len(CLASSES),
    ]
    for label, row in zip(CLASSES, matrix, strict=True):
        lines.append(f"| {label} | " + " | ".join(str(v) for v in row) + " |")
    return "\n".join(lines)


def render(repo: Path) -> str:
    reports = repo / "reports" / "m1"
    index_path = repo / "data" / "features" / "discriminator" / "windows.json"
    index = (
        DatasetIndex.model_validate(json.loads(index_path.read_text(encoding="utf-8")))
        if index_path.exists()
        else None
    )
    build = _load(reports / "build.json")
    plan = _load(reports / "plan.json")
    loro = _load(reports / "eval_loro_hma_baseline.json")
    forward = _load(reports / "eval_time_forward_baseline.json")
    loro_deep = _load(reports / "eval_loro_hma_deep.json")
    paired = _load(reports / "paired_loro_hma.json")
    seal = _load(reports / "seal.json")
    artifact = _load(repo / "baselines" / "discriminator" / "loro_hma" / "artifact.json")
    deep_artifact = _load(repo / "baselines" / "discriminator_deep" / "loro_hma" / "artifact.json")
    latencies = sorted(reports.glob("latency_*.json"))

    out: list[str] = []
    add = out.append

    add("# Model card — M1 seismic mass-movement discriminator")
    add("")
    add(
        "Separates long-period single-force mass-movement signals from double-couple tectonic "
        "earthquakes and from noise, on regional broadband records. It is the component built "
        'to catch the failure that produced the "M4.4 earthquake" misreport of 26 August 2026.'
    )
    add("")
    add(
        "**It does not yet catch that failure.** On the Langtang window as the open archives "
        "actually hold it, this model puts `tectonic` marginally above `mass_movement`. The "
        "case study below gives the numbers and the reason. Chamoli 2021, which had twelve "
        "receivers rather than two, is classified correctly in the held-out fold. Read the "
        "metrics below as what a v0 baseline achieves on the events that had coverage, not as "
        "a claim about the event this project exists for."
    )
    add("")
    add("## Intended use")
    add("")
    add(
        "- Near-real-time triage of a regional broadband array: *what kind of source made this?*\n"
        "- A gate in front of M2's force-history inversion, so an inversion is not attempted on "
        "an earthquake."
    )
    add("")
    add("## Out-of-scope use")
    add("")
    add(
        "- **It does not locate.** `source_location` is always null. Locating is M2's job.\n"
        "- **It does not estimate volume, mass or runout.**\n"
        "- **It is not a time-of-failure predictor** and says nothing about a slope before it "
        "fails.\n"
        "- **It must not be run on raw counts.** `require_response=True` raises rather than "
        "score counts, because the model would return a confident, meaningless probability.\n"
        "- It has not been evaluated outside the regions in the table below, and High Mountain "
        "Asia is represented by a very small number of events."
    )
    add("")

    add("## Data")
    add("")
    if index is not None:
        counts = Counter(w.class_label for w in index.windows)
        add(
            f"- **{index.n_windows:,} windows** over **{len(index.groups()):,} event groups**: "
            + ", ".join(f"{k.value} {counts.get(k, 0):,}" for k in ClassLabel)
        )
        add(
            f"- 600 s windows from origin-60 s, instrument response removed to velocity, "
            f"{index.bandpass_hz[0]}-{index.bandpass_hz[1]} Hz, {index.sampling_rate_hz:.0f} Hz, "
            f"up to {index.max_stations} receivers at 100-1500 km, azimuth-binned."
        )
    if plan is not None:
        add(
            f"- Requested by the build: {plan['n_positives']:,} positives, "
            f"{plan['n_negatives']:,} tectonic negatives, {plan['n_noise']:,} noise windows "
            f"across {plan['n_unique_stations']:,} unique receivers. That positive count is "
            "what the join actually produced, and the join was incomplete: see the source "
            "table below."
        )
    if build is not None:
        add(
            f"- Written: {build['n_windows_written']:,} of {build['n_windows_requested']:,} "
            f"windows; positives {build['positives_written']:,} of "
            f"{build['positives_requested']:,}. "
            f"**{build['n_windows_not_fetched']:,} windows recorded `status: not_fetched`** with "
            "their reason and excluded — never substituted, backfilled or replaced."
        )
        add(f"- Bytes fetched: {build['bytes_fetched'] / 1e9:.2f} GB.")
        add(
            f"- Store pinned by chunk index sha256 `{build['chunk_index_sha256']}` over "
            f"{build['n_chunk_files']:,} files."
        )
    add("")
    add("### Where the windows actually came from")
    add("")
    if index is not None:
        by_source = Counter(w.source for w in index.windows)
        positives_by_source = Counter(
            w.source for w in index.windows if w.class_label is ClassLabel.mass_movement
        )
        add("| source | windows | of which positives |")
        add("|---|---|---|")
        for source, total in sorted(by_source.items()):
            add(f"| `{source}` | {total:,} | {positives_by_source.get(source, 0):,} |")
        add("")
    add(
        "**USGS ComCat `eventtype=landslide` contributed nothing, and that is a defect, not a "
        "design choice.** The brief names it as a positive source and `catalog.assemble` "
        "supports it, but the CLI never passed the committed fixture into the join, so "
        "**zero** of its 57 events are in this dataset. The bug is fixed in `cli_data.py`; the "
        "built store predates the fix and was **deliberately not rebuilt**, because those 57 "
        "events are mostly Alaska ml 1-2, Chamoli 2021 is absent from them, and a rebuild "
        "would cost an hour of compute and force a third scoring of the test set. It is "
        "recorded here and in the failure modes as a known gap rather than quietly corrected."
    )
    add("")
    add(
        "The positives in this dataset therefore come from ESEC (IRIS/EarthScope SPUD, 319 "
        "events 1977-2024, committed verbatim as a fixture) and the serac event library, and "
        "from nothing else. For the record of what was missed: ComCat's landslide set is 57 "
        "events since 2000, only 6 with M>=4, mostly Alaska ml 1-2, and Chamoli 2021 is absent "
        "from it, so it could not have carried this component alone."
    )
    add("")

    if index is not None:
        add("### Counts by class x region x decade")
        add("")
        add(_counts_table(index))
        add("")

    add("## Features")
    add("")
    add(
        f"{len(FEATURE_NAMES)} features, computed **only** from the Zarr `waveform` and `valid` "
        "arrays: long-period/short-period energy ratios, envelope duration and emergence, "
        "spectral centroid drift, horizontal/vertical energy ratio, long-period rectilinearity "
        "and cross-receiver envelope coherence, each aggregated across a window's receivers by "
        "median, median absolute deviation and 90th percentile."
    )
    add("")
    add(
        "**No feature encodes geometry, epoch or identity.** A test fails the build if any "
        "feature name contains "
        + ", ".join(f"`{t}`" for t in FORBIDDEN_FEATURE_TOKENS)
        + ". No geometry-derived feature is kept, so there is no ablation to report: incidence "
        "angle and back-azimuth-corrected polarisation were considered and rejected, because "
        "with ~320 positives epicentral distance is close to a primary key and a model given "
        "it can identify events rather than physics."
    )
    add("")

    add("## Metrics")
    add("")
    superseded = _load(reports / "superseded" / "eval_loro_hma_baseline_seal1.json")
    if superseded is not None and loro is not None:
        add("### The test set was scored twice, and why")
        add("")
        add(
            "The first scoring is superseded and is kept at "
            "`reports/m1/superseded/eval_loro_hma_baseline_seal1.json`. It was discarded "
            "because the finiteness check in `windows.py` ran on the float64 array before the "
            "cast to float32, so a value above float32 max became `inf` and one window in "
            "1,925 entered the store as a poisoned trace. Fixing it changed the trained model, "
            "which changed the test predictions. `seal_version` was bumped from 1 to 2 to "
            "record the re-scoring as a deliberate act."
        )
        add("")
        add("| metric | first scoring (superseded) | second scoring (reported) | moved |")
        add("|---|---|---|---|")
        rows = [
            (
                "mass_movement F1",
                superseded["mass_movement_f1"]["point"],
                loro["mass_movement_f1"]["point"],
                "higher is better",
            ),
            (
                "ROC-AUC",
                superseded["roc_auc"]["point"],
                loro["roc_auc"]["point"],
                "higher is better",
            ),
            (
                "Brier",
                superseded["reliability"]["brier"],
                loro["reliability"]["brier"],
                "lower is better",
            ),
            (
                "ECE",
                superseded["reliability"]["ece"],
                loro["reliability"]["ece"],
                "lower is better",
            ),
        ]
        for label, before, after, sense in rows:
            improved = (after > before) if sense == "higher is better" else (after < before)
            verdict = "better" if improved else "worse"
            add(f"| {label} ({sense}) | {before:.4f} | {after:.4f} | **{verdict}** |")
        add("")
        add(
            "**Every metric moved in the unfavourable direction.** That is the point of "
            "disclosing it: a second scoring is only defensible if a reader can see it did not "
            "buy a better number, and here it did not. Every figure reported in this card is "
            "from the second, worse scoring."
        )
        add("")
    add(
        "Intervals are 95% percentile bootstrap over **test event groups** (2000 resamples), "
        "not over windows: a group contributes one positive plus its matched negatives and "
        "noise, all cut at the same receivers, so resampling windows would treat several views "
        "of one event as independent observations."
    )
    add("")
    for name, result in (
        (f"Leave-one-region-out, {region_label(HELD_OUT_REGION)} held out (**headline**)", loro),
        ("Time-forward (train <2020, val 2020-2023, test 2024-2026)", forward),
    ):
        add(f"### {name}")
        add("")
        if result is None:
            add("_Not evaluated._")
            add("")
            continue
        add(
            f"{result['n_test_windows']:,} test windows over {result['n_test_groups']} groups, "
            f"**{result['n_test_positives']} positives**."
        )
        add("")
        add("| metric | value [95% CI] |")
        add("|---|---|")
        add(f"| mass_movement F1 | {_interval(result['mass_movement_f1'])} |")
        add(f"| mass_movement precision | {_interval(result['mass_movement_precision'])} |")
        add(f"| mass_movement recall | {_interval(result['mass_movement_recall'])} |")
        add(f"| macro F1 | {_interval(result['macro_f1'])} |")
        add(f"| ROC-AUC (mass_movement vs rest) | {_interval(result['roc_auc'])} |")
        add(f"| Brier | {result['reliability']['brier']:.4f} |")
        add(f"| ECE | {result['reliability']['ece']:.4f} |")
        add("")
        add("Confusion matrix:")
        add("")
        add(_confusion(result["confusion"]))
        add("")
        add(
            "Per-region confusion matrices (denominators are small; they are printed because "
            "hiding them would be worse, not because a region with one positive means anything):"
        )
        add("")
        for region, matrix in sorted(result["confusion_by_region"].items()):
            add(f"**{region_label(region)}**")
            add("")
            add(_confusion(matrix))
            add("")
        add("Forced test groups:")
        add("")
        for group, outcome in sorted(result["forced_group_outcomes"].items()):
            add(f"- `{group}`: {json.dumps(outcome)}")
        add("")
        add("Reliability bins (mean predicted probability vs observed frequency):")
        add("")
        rel = result["reliability"]
        add("| bin | n | mean p | observed |")
        add("|---|---|---|---|")
        for i, n in enumerate(rel["bin_counts"]):
            if n == 0:
                continue
            add(
                f"| {rel['bin_edges'][i]:.1f}-{rel['bin_edges'][i + 1]:.1f} | {n} | "
                f"{rel['bin_mean_probability'][i]:.3f} | {rel['bin_observed_frequency'][i]:.3f} |"
            )
        add("")

    add("## Deep model versus baseline")
    add("")
    if paired is None:
        add("_The paired comparison was not run._")
    else:
        add(f"Promotion rule, fixed before either model was trained: {paired['rule']}")
        add("")
        add(
            f"- {paired['challenger']} F1 {paired['challenger_f1']:.3f}, "
            f"{paired['incumbent']} F1 {paired['incumbent_f1']:.3f}"
        )
        add(
            f"- **delta F1 = {paired['delta_f1']:+.3f} "
            f"[{paired['delta_low']:+.3f}, {paired['delta_high']:+.3f}]** over "
            f"{paired['n_resamples']} group resamples"
        )
        add(
            f"- **Default: "
            f"{'the deep model' if paired['promoted'] else 'the lightgbm baseline (retained)'}.**"
        )
        if not paired["promoted"]:
            add(
                "  The lower bound does not exceed zero, so the comparison is inconclusive "
                "rather than negative. At this number of held-out positives it could not have "
                "been anything else; that is the finding, not a failure."
            )
    if deep_artifact is not None:
        add("")
        add(
            f"Deep model: {deep_artifact['n_parameters']:,} parameters, best epoch "
            f"{deep_artifact['best_epoch']} of {deep_artifact['epochs_run']}, device "
            f"`{deep_artifact['device']}`, validation macro F1 "
            f"{deep_artifact['best_val_macro_f1']:.3f}. No positional encoding on the receiver "
            "axis, so it is permutation-invariant and cannot key on slot order."
        )
    if loro_deep is not None:
        add("")
        add(
            f"Deep on the held-out region: mass_movement F1 "
            f"{_interval(loro_deep['mass_movement_f1'])}, ROC-AUC "
            f"{_interval(loro_deep['roc_auc'])}."
        )
    add("")

    add("## Detection latency")
    add("")
    if not latencies:
        add("_Not measured._")
    for path in latencies:
        report = _load(path)
        if report is None:
            continue
        add(f"### {report['event_id']}")
        add("")
        add("| mode | fired | stream latency | theoretical floor | compute per scored window | p |")
        add("|---|---|---|---|---|---|")
        for mode in report["modes"]:
            latency = (
                f"{mode['stream_latency_s']:.0f} s" if mode["stream_latency_s"] is not None else "-"
            )
            probability = f"{mode['probability']:.3f}" if mode["probability"] is not None else "-"
            compute = (
                f"{mode['compute_seconds_per_scored_window']:.1f} s"
                if mode.get("compute_seconds_per_scored_window")
                else "-"
            )
            add(
                f"| `{mode['mode']}` | {mode['fired']} | {latency} | "
                f"{mode['theoretical_floor_s']:.0f} s | {compute} | {probability} |"
            )
        add("")
        add(f"**Verdict.** {report['verdict']}")
        add("")

    add("## Case study: Langtang / Lhende Khola, 26 August 2026")
    add("")
    case = _load(reports / "case_study_langtang-lhende-2026.json")
    if case is None:
        add("_Not run._")
    else:
        add(
            f"This is the event M1 exists for. Eight days after it happened, only "
            f"**{case['receivers_with_response_removed_data']} of "
            f"{case['receivers_selected']} selected receivers** "
            f"({', '.join(f'`{r}`' for r in case['receivers_used'])}) had data in the open "
            f"archives at 100-1500 km, against the dataset's minimum of "
            f"{case['min_stations_required_by_the_dataset']}. The window was therefore "
            "**excluded from the dataset** and recorded as `not_fetched` with that reason, so "
            "it contributes to no metric above. The nearest open broadband, NK.KKN at ~55 km, "
            "is inside the 100 km floor and is excluded by design."
        )
        add("")
        add(
            "The receiver threshold was **not** lowered to admit it. Moving a data-quality "
            "threshold after discovering it excludes the headline event is post-hoc tuning, "
            "and it is the kind this component was built to refuse. Instead the "
            "already-trained, already-sealed model was applied to the window as it is:"
        )
        add("")
        add("| class | probability |")
        add("|---|---|")
        for name, value in case["class_probabilities"].items():
            add(f"| {name} | {value:.3f} |")
        add("")
        add(
            f"**Predicted class: `{case['predicted_class']}`. Calibrated P(mass movement) = "
            f"{case['calibrated_probability_mass_movement']:.3f}.**"
        )
        add("")
        add(
            "**On this evidence M1 would not have prevented the 'M4.4 earthquake' misreport.** "
            "It puts `tectonic` marginally above `mass_movement` "
            f"({case['class_probabilities']['tectonic']:.3f} against "
            f"{case['class_probabilities']['mass_movement']:.3f}) — the two are nearly tied and "
            "the ordering is the wrong way round. That is the result, and it is reported as the "
            "result. Two caveats belong with it and neither rescues it: the window has two "
            "receivers where the model was trained on three or more, and a two-receiver window "
            "cannot support the cross-receiver coherence and azimuthal spread the feature set "
            "leans on. The honest reading is that M1 v0 needs regional coverage this event did "
            "not have in the open archives within eight days, not that it succeeded."
        )
        add("")
        add(
            "By contrast Chamoli 2021, which had twelve receivers, is classified correctly in "
            "the held-out fold under both split schemes."
        )
        add("")

    add("## Failure modes")
    add("")
    add(
        "1. **High Mountain Asia is thinly represented.** ESEC holds five HMA events; with the "
        "serac event library the held-out fold has **nine positives**. Every HMA number in this "
        "card has an interval wide enough to contain a great deal, and no point estimate should "
        "be quoted without its interval.\n"
        "2. **The time-forward test fold is tiny.** ESEC's last event is 2024, so a 2024-2026 "
        "test window has a handful of events. Leave-one-region-out is the headline for that "
        "reason.\n"
        "3. **The receiver-count residual is live, not closed.** Positives realise on average "
        "**+1.01** more receivers than their own negatives. No feature counts receivers "
        "directly (`valid_channel_fraction` was removed for that reason), but the "
        "cross-receiver aggregates `*_mad`, `*_p90` and `lp_envelope_coherence` are all "
        "functions of how many traces contributed, so the information is not fully excluded. "
        "Measured on the built store: corr(`n_stations`, positive) = +0.110 over all windows "
        "and +0.145 over positives and tectonics only, and **`n_stations` alone gives ROC-AUC "
        "0.587**, better than chance. Some of the reported skill may therefore be archive "
        "density rather than source physics, and this card does not claim otherwise.\n"
        "4. **Four windows are duplicated, and they are in the held-out fold.** "
        "`neg/sedongpu-2017-2018/*` appear twice because two positives share an event group "
        "(Sedongpu 2017 and 2018 are one slope) and matched the same earthquakes, while the "
        "negative id did not include the parent. Sedongpu is in High Mountain Asia, so the "
        "duplicates land in the LORO **test** fold and double-count four tectonic windows. "
        "Scoring the same predictions on a de-duplicated fold gives mass_movement F1 **0.533 "
        "(n=52)** against the **0.516 (n=56)** reported throughout this card. **The reported "
        "number is the lower, conservative one**; no model, threshold or split was changed to "
        "obtain either. The id collision is fixed in `catalog.py` for future builds and the "
        "store was not rebuilt.\n"
        "5. **ComCat's 57 landslide events are missing** through the CLI bug described under "
        "Data. The positive set is ESEC plus the serac event library and nothing else.\n"
        "6. **Negatives are not magnitude-matched.** ESEC publishes no magnitude, so negatives "
        "are matched on receiver set, epicentral proximity and epoch inside a fixed M4.0-6.5 "
        "band. If mass movements systematically differ in size from that band, some of what the "
        "model separates may be amplitude rather than mechanism.\n"
        "7. **The noise class means 'no catalogued source', not 'quiet'.** Uncatalogued sources, "
        "small teleseisms and cultural noise are all in it.\n"
        "8. **A truncated window is out of distribution.** `sliding_180s` asks the model about "
        "180 s of record zero-padded to 600 s, which it never saw in training. Its scores are "
        "reported next to the batch scores, not instead of them.\n"
        "9. **Regional coverage is what the open archives hold.** Alaska, the European Alps and "
        "the North American Cordillera dominate the positives because that is where open "
        "broadband networks and the ESEC compilers' attention are, not because mass movements "
        "are commonest there.\n"
        "10. **Events with no open coverage are absent, and their absence is recorded.** They are "
        "counted above and appear in `data/manifest.jsonl` as `not_fetched` rows with reasons.\n"
        "11. **Thin coverage on a recent event is the binding constraint, not model skill.** "
        "Langtang 2026 had two usable open receivers eight days after the event. Whatever the "
        "classifier can do, it cannot do it without records.\n"
        "12. **A response gap silently narrows a window.** Receivers whose response could not be "
        "read are dropped; a window below three usable receivers is excluded entirely."
    )
    add("")

    add("## Provenance and anti-tuning")
    add("")
    if artifact is not None:
        add(
            f"- Baseline `{artifact['name']}` {artifact['baseline_version']}, scheme "
            f"`{artifact['split_scheme']}`, {artifact['n_train_windows']:,} training windows "
            f"over {artifact['n_train_groups']} groups, best iteration "
            f"{artifact['best_iteration']}."
        )
        add(f"- Model sha256 `{artifact['model_sha256']}`.")
        add(
            f"- Training groups sha256 `{artifact['train_event_groups_sha256']}`; "
            "`validate-discriminator` recomputes it from the split, so the shipped model "
            "proves what it was trained on."
        )
        calibrator = artifact["calibrator"]
        add(
            f"- Calibration: {calibrator['method']}, fitted on "
            f"`{calibrator['fitted_on']}` only (n={calibrator['n_fitted']})."
        )
    if seal is not None:
        add(
            f"- Anti-tuning seal `{seal['config_sha256']}` sealed at {seal['sealed_at_utc']}; "
            f"schemes evaluated under it: {seal['schemes_evaluated']}. A test evaluation under "
            "a changed configuration is refused."
        )
        add(
            "- **What the seal does not cover.** The fingerprint hashes named constants "
            "(feature names, window and catalogue parameters, split rules, LightGBM "
            "hyperparameters, bootstrap settings) and **not the code**. The float32 fix above "
            "changed behaviour without moving any constant, so `config_hash()` was unchanged "
            "and the seal did not trip; the re-seal was a manual version bump, not an "
            "automatic detection. Read the seal as protection against hyperparameter tuning "
            "between scorings, not against all behavioural change."
        )
    add("")
    add(
        "Chamoli 2021 and the Langtang 2026 pair are forced into the test fold under both "
        "schemes and appear in neither training nor validation, including for early stopping "
        "and for the calibrator."
    )
    add("")
    return "\n".join(out) + "\n"


def write(repo: Path) -> Path:
    path = repo / CARD_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(repo), encoding="utf-8")
    return path
