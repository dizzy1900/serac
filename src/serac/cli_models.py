"""`serac models ...` — train and evaluate the M1 discriminator.

`train-discriminator` fits the baseline (and optionally the deep model) under one split
scheme, writes the committed artifacts, and scores the held-out fold **once** under the seal.
Running it twice under the same configuration is safe; running it after changing a
configuration constant is refused by `evaluate.check_seal`, which is the point.

Features are cached to `data/features/discriminator/features.npy` keyed by the dataset's chunk
index hash, so the ~2 200-window extraction runs once and a second scheme reuses it. The cache
is invalidated automatically when the store changes, because its key is the store's own hash.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import numpy as np
import typer

app = typer.Typer(help="Train and evaluate serac's model components.", no_args_is_help=True)

DATASET_DIR = Path("data/features/discriminator")
REPORTS_DIR = Path("reports/m1")
FEATURE_CACHE = "features.npy"
FEATURE_CACHE_KEY = "features.key"


def _load_features(repo: Path, *, echo: bool = True) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(features, waveforms, valids). Features are cached against the store's own hash."""
    from serac.models.discriminator.dataset import load_arrays, write_chunk_index
    from serac.models.discriminator.features import N_FEATURES, feature_matrix

    root = repo / DATASET_DIR
    _, store_hash, _ = write_chunk_index(root)
    cache, key_file = root / FEATURE_CACHE, root / FEATURE_CACHE_KEY
    waveform, valid = load_arrays(root)
    waveforms = np.asarray(waveform[:])
    valids = np.asarray(valid[:])
    if cache.exists() and key_file.exists() and key_file.read_text().strip() == store_hash:
        features = np.load(cache)
        if features.shape == (waveforms.shape[0], N_FEATURES):
            if echo:
                typer.echo(f"features: reused cache for store {store_hash[:16]}")
            return features, waveforms, valids
    if echo:
        typer.echo(f"features: extracting {waveforms.shape[0]} windows...")
    features = feature_matrix(waveforms, valids)
    np.save(cache, features)
    key_file.write_text(store_hash + "\n", encoding="utf-8")
    return features, waveforms, valids


@app.command("train-discriminator")
def train_discriminator(
    repo: Annotated[Path, typer.Option(help="Repository root.")] = Path(),
    scheme: Annotated[
        str, typer.Option(help="`loro_hma` (headline) or `time_forward`.")
    ] = "loro_hma",
    deep: Annotated[bool, typer.Option("--deep", help="Also train the deep challenger.")] = False,
    epochs: Annotated[int, typer.Option(help="Deep-model epochs.")] = 40,
) -> None:
    """Fit, then score the held-out fold once under the anti-tuning seal."""
    from serac.models.discriminator import baseline as bl
    from serac.models.discriminator import deep as dp
    from serac.models.discriminator import evaluate as ev
    from serac.models.discriminator.catalog import FORCED_TEST_GROUPS
    from serac.models.discriminator.dataset import assign_loro, assign_time_forward, load_index
    from serac.models.discriminator.regions import HELD_OUT_REGION

    index = load_index(repo / DATASET_DIR)
    features, waveforms, valids = _load_features(repo)
    assignment = (
        assign_loro(index, HELD_OUT_REGION) if scheme == "loro_hma" else assign_time_forward(index)
    )
    labels = np.array([bl.CLASSES.index(w.class_label.value) for w in index.windows])
    groups = [w.event_group for w in index.windows]
    regions = np.array([w.region_id for w in index.windows])
    splits = assignment.for_windows(index.windows)

    typer.echo(f"scheme {scheme}: " + json.dumps(assignment.counts(index.windows)))
    for note in assignment.notes:
        typer.echo(f"  note: {note}")

    artifact_dir = repo / bl.ARTIFACT_DIR / scheme
    artifact = bl.train(
        features,
        labels,
        splits,
        groups,
        split_scheme=scheme,
        artifact_dir=artifact_dir,
    )
    typer.echo(
        f"baseline: {artifact.n_train_windows} train / {artifact.n_val_windows} val windows, "
        f"best iteration {artifact.best_iteration}, groups sha256 "
        f"{artifact.train_event_groups_sha256[:16]}"
    )

    is_test = splits == "test"
    if not is_test.any():
        typer.secho("no test rows under this scheme; nothing to score.", fg=typer.colors.RED)
        raise typer.Exit(2)

    model = bl.load(artifact_dir)
    predicted = model.class_probabilities(features[is_test]).argmax(axis=1)
    probabilities = model.calibrated_probability(features[is_test])
    result = ev.evaluate(
        scheme=scheme,
        model_name=bl.BASELINE_NAME,
        truth=labels[is_test],
        predicted=predicted,
        probabilities=probabilities,
        groups=np.array(groups)[is_test],
        regions=regions[is_test],
        group_ids=[g for g, keep in zip(groups, is_test, strict=True) if keep],
        repo=repo,
        forced_groups=sorted(FORCED_TEST_GROUPS),
        notes=list(assignment.notes),
    )
    reports = repo / REPORTS_DIR
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"eval_{scheme}_baseline.json").write_text(
        result.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    _echo_result(result)

    if not deep:
        return

    deep_dir = repo / dp.ARTIFACT_DIR / scheme
    deep_artifact = dp.train(
        waveforms,
        valids,
        labels,
        splits,
        groups,
        split_scheme=scheme,
        artifact_dir=deep_dir,
        epochs=epochs,
        progress=lambda message: typer.echo(f"  {message}"),
    )
    typer.echo(
        f"deep: {deep_artifact.n_parameters:,} parameters, best epoch "
        f"{deep_artifact.best_epoch}, val macro F1 {deep_artifact.best_val_macro_f1:.3f}"
    )
    deep_predicted, deep_probabilities = dp.predict(waveforms[is_test], valids[is_test], deep_dir)
    deep_result = ev.evaluate(
        scheme=scheme,
        model_name=dp.DEEP_NAME,
        truth=labels[is_test],
        predicted=deep_predicted,
        probabilities=deep_probabilities[:, bl.POSITIVE_CLASS_INDEX],
        groups=np.array(groups)[is_test],
        regions=regions[is_test],
        group_ids=[g for g, keep in zip(groups, is_test, strict=True) if keep],
        repo=repo,
        forced_groups=sorted(FORCED_TEST_GROUPS),
        notes=[
            "deep model softmax used directly as the probability: it carries no separate "
            "calibrator, so its reliability numbers are for an uncalibrated softmax"
        ],
    )
    (reports / f"eval_{scheme}_deep.json").write_text(
        deep_result.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    _echo_result(deep_result)

    comparison = ev.paired_bootstrap(
        scheme=scheme,
        challenger=dp.DEEP_NAME,
        incumbent=bl.BASELINE_NAME,
        truth=labels[is_test],
        challenger_predicted=deep_predicted,
        incumbent_predicted=predicted,
        groups=np.array(groups)[is_test],
    )
    (reports / f"paired_{scheme}.json").write_text(
        comparison.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    typer.echo("")
    typer.echo(
        f"paired delta F1 (deep - baseline) = {comparison.delta_f1:+.3f} "
        f"[{comparison.delta_low:+.3f}, {comparison.delta_high:+.3f}] over "
        f"{comparison.n_resamples} group resamples"
    )
    typer.secho(
        f"promotion: {'DEEP becomes default' if comparison.promoted else 'BASELINE retained'} "
        f"— {comparison.rule}",
        fg=typer.colors.GREEN if comparison.promoted else typer.colors.YELLOW,
    )


def _echo_result(result: object) -> None:
    from serac.models.discriminator.evaluate import EvaluationResult

    assert isinstance(result, EvaluationResult)
    typer.echo("")
    typer.echo(
        f"{result.model_name} / {result.scheme}: {result.n_test_windows} test windows, "
        f"{result.n_test_groups} groups, {result.n_test_positives} positives"
    )
    for metrics in result.per_class:
        typer.echo(
            f"  {metrics.label:14s} n={metrics.support:<4d} P={metrics.precision:.3f} "
            f"R={metrics.recall:.3f} F1={metrics.f1:.3f}"
        )
    for name, interval in (
        ("macro F1", result.macro_f1),
        ("mass_movement F1", result.mass_movement_f1),
        ("mass_movement precision", result.mass_movement_precision),
        ("mass_movement recall", result.mass_movement_recall),
        ("ROC-AUC", result.roc_auc),
    ):
        typer.echo(
            f"  {name:26s} {interval.point:.3f} "
            f"[{interval.low:.3f}, {interval.high:.3f}] (95%, {interval.resample_unit})"
        )
    typer.echo(f"  Brier {result.reliability.brier:.4f}  ECE {result.reliability.ece:.4f}")
    for group, outcome in sorted(result.forced_group_outcomes.items()):
        typer.echo(f"  forced {group}: {json.dumps(outcome)}")


@app.command("validate-discriminator")
def validate_discriminator(
    repo: Annotated[Path, typer.Option(help="Repository root.")] = Path(),
) -> None:
    """Run the leakage gate and write `reports/validation/discriminator.json`."""
    from serac.validation.discriminator import run_suite
    from serac.validation.result import print_result, write_report

    result = run_suite(repo)
    print_result(result)
    write_report(result, repo / "reports" / "validation")
    raise typer.Exit(0 if result.passed else 1)


@app.command("measure-latency")
def measure_latency(
    repo: Annotated[Path, typer.Option(help="Repository root.")] = Path(),
    event_group: Annotated[
        str, typer.Option(help="Event group to replay, e.g. `langtang-lhende-2026`.")
    ] = "langtang-lhende-2026",
    scheme: Annotated[str, typer.Option(help="Which trained artifact to load.")] = "loro_hma",
    threshold: Annotated[float, typer.Option(help="Calibrated-probability threshold.")] = 0.5,
) -> None:
    """Replay one event's raw waveforms through the detector in both modes and time both clocks."""
    import json as _json

    from obspy import Inventory, read_inventory

    from serac.models.discriminator import baseline as bl
    from serac.models.discriminator import latency as lat
    from serac.models.discriminator import streaming as st
    from serac.models.discriminator.catalog import build_positives
    from serac.models.discriminator.windows import StationChoice

    positives, _, _ = build_positives(repo)
    positive = next((p for p in positives if p.event_group == event_group), None)
    if positive is None:
        typer.secho(f"no positive with group {event_group!r}", fg=typer.colors.RED)
        raise typer.Exit(2)

    raw = repo / "data/raw/discriminator/waveforms" / f"{positive.entry_id.replace('/', '_')}.mseed"
    if not raw.exists():
        typer.secho(
            f"{raw} is absent; run `serac data build-discriminator-set` first",
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    selection = repo / "data/interim/discriminator/stations" / f"{event_group}.json"
    stations = [
        StationChoice.model_validate(row)
        for row in _json.loads(selection.read_text(encoding="utf-8"))["stations"]
    ]
    inventory = Inventory()
    year = positive.origin_utc.year
    for station in stations:
        path = (
            repo
            / "data/raw/discriminator/responses"
            / f"{station.key.replace('.', '_')}_{year}.xml"
        )
        if path.exists() and path.stat().st_size > 0:
            inventory += read_inventory(str(path), format="STATIONXML")

    chunks = lat.chunk_stream_from_miniseed(raw, positive.origin_utc)
    typer.echo(
        f"{event_group}: {len(chunks)} chunks from {len(stations)} selected receivers, "
        f"{len(inventory.get_contents()['channels'])} responses loaded"
    )

    model = bl.load(repo / bl.ARTIFACT_DIR / scheme)
    results = []
    modes: tuple[st.Mode, ...] = ("batch_600s", "sliding_180s")
    for mode in modes:
        detector = st.DiscriminatorDetector(
            model=model,
            inventory=inventory,
            require_response=True,
            threshold=threshold,
            mode=mode,
        )
        result = lat.measure(detector, chunks, positive.origin_utc, mode=mode)
        results.append(result)
        typer.echo(
            f"  {mode:14s} fired={result.fired} "
            f"stream_latency={result.stream_latency_s} s "
            f"floor={result.theoretical_floor_s:.0f} s "
            f"compute_p95={result.compute_seconds_per_poll_p95 * 1000:.0f} ms "
            f"p={result.probability}"
        )

    report = lat.build_report(
        event_group,
        positive.origin_utc,
        results,
        n_receivers=len(stations),
        notes=[
            f"replayed from {raw.as_posix()} (raw counts, ledgered by the M1 build); the "
            "detector's own response removal is inside the timed section",
            f"model {model.artifact.name} trained under {model.artifact.split_scheme}; "
            f"{event_group} is a forced test group and was not in training",
        ],
    )
    out = repo / REPORTS_DIR / f"latency_{event_group}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    typer.echo("")
    typer.secho(
        report.verdict, fg=typer.colors.YELLOW if not report.budget_met else typer.colors.GREEN
    )


@app.command("write-model-card")
def write_model_card(
    repo: Annotated[Path, typer.Option(help="Repository root.")] = Path(),
) -> None:
    """Render `reports/MODEL_CARD_discriminator.md` from the committed JSON reports."""
    from serac.models.discriminator.model_card import write

    typer.echo(f"wrote {write(repo)}")
