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
from typing import Annotated, Any

import numpy as np
import typer

app = typer.Typer(help="Train and evaluate serac's model components.", no_args_is_help=True)

DATASET_DIR = Path("data/features/discriminator")


class _LazyWindows:
    """Row-at-a-time access to the Zarr waveform array, with the numpy shape API the models use.

    Only `shape`, integer indexing and boolean-mask selection are supported, which is all the
    training and prediction paths need, and each of them reads one window at a time.
    """

    def __init__(self, array: Any, n_windows: int) -> None:
        self._array = array
        self.shape = (n_windows, *array.shape[1:])

    def __len__(self) -> int:
        return int(self.shape[0])

    def __getitem__(self, key: Any) -> np.ndarray:
        if isinstance(key, (int, np.integer)):
            return np.asarray(self._array[int(key)])
        rows = np.flatnonzero(np.asarray(key)) if np.asarray(key).dtype == bool else np.asarray(key)
        return np.stack([np.asarray(self._array[int(r)]) for r in rows])


REPORTS_DIR = Path("reports/m1")
FEATURE_CACHE = "features.npy"
FEATURE_CACHE_KEY = "features.key"


def _load_features(repo: Path, *, echo: bool = True) -> tuple[np.ndarray, _LazyWindows, np.ndarray]:
    """(features, waveforms, valids). Features are cached against the store's own hash.

    The Zarr arrays are allocated for every *requested* window and only the ones that yielded
    usable data are written, so the arrays are sliced to `index.n_windows`. Without the slice
    the trailing all-zero rows would line up against nothing in the index and quietly enter
    training as unlabelled silence.
    """
    from serac.models.discriminator.dataset import load_arrays, load_index, write_chunk_index
    from serac.models.discriminator.features import N_FEATURES, feature_matrix

    root = repo / DATASET_DIR
    _, store_hash, _ = write_chunk_index(root)
    cache, key_file = root / FEATURE_CACHE, root / FEATURE_CACHE_KEY
    n_windows = load_index(root).n_windows
    waveform, valid = load_arrays(root)
    # `valid` is a few hundred kilobytes and is materialised; `waveform` is ~3 GB and is left
    # as a lazy Zarr array, read a row at a time. On a 16 GB machine shared with three other
    # tracks, materialising it put the box into swap and made an epoch unbounded.
    valids = np.asarray(valid[:n_windows])
    waveforms = _LazyWindows(waveform, n_windows)
    if cache.exists() and key_file.exists() and key_file.read_text().strip() == store_hash:
        features = np.load(cache)
        if features.shape == (n_windows, N_FEATURES):
            if echo:
                typer.echo(f"features: reused cache for store {store_hash[:16]}")
            return features, waveforms, valids
    if echo:
        typer.echo(f"features: extracting {n_windows} windows...")
    features = np.zeros((n_windows, N_FEATURES), dtype=np.float64)
    for row in range(n_windows):
        features[row] = feature_matrix(
            np.asarray(waveform[row])[None, ...], valids[row][None, ...]
        )[0]
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
    raise typer.Exit(result.exit_code)


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


@app.command("case-study")
def case_study(
    repo: Annotated[Path, typer.Option(help="Repository root.")] = Path(),
    event_group: Annotated[str, typer.Option(help="Event group to score.")] = (
        "langtang-lhende-2026"
    ),
    scheme: Annotated[str, typer.Option(help="Which trained artifact to load.")] = "loro_hma",
) -> None:
    """Score one event's window with the sealed model, below the dataset's own quality bar.

    Some events are excluded from the dataset because fewer than `MIN_STATIONS_PER_WINDOW`
    receivers yielded response-removed data. Langtang 2026 is one: eight days after the event
    only two of its twelve selected receivers had data in the open archives.

    Lowering that threshold *after discovering it excludes the headline event* would be exactly
    the post-hoc tuning this component is built to avoid, so the threshold does not move. This
    command instead applies the already-trained, already-sealed model to the window as it
    actually is, and labels the result as what it is: **a single-window case study below the
    dataset's own quality bar, not a test-set metric.** It is never averaged into any score.
    """
    import json as _json
    import warnings

    from serac.models.discriminator import baseline as bl
    from serac.models.discriminator.catalog import build_positives
    from serac.models.discriminator.features import FEATURE_NAMES, compute_features
    from serac.models.discriminator.windows import (
        COMPONENTS,
        MAX_STATIONS_PER_EVENT,
        MIN_STATIONS_PER_WINDOW,
        N_SAMPLES,
        StationChoice,
        process_station_window,
    )

    positives, _, _ = build_positives(repo)
    positive = next((p for p in positives if p.event_group == event_group), None)
    if positive is None:
        typer.secho(f"no positive with group {event_group!r}", fg=typer.colors.RED)
        raise typer.Exit(2)

    raw = repo / "data/raw/discriminator/waveforms" / f"{positive.entry_id.replace('/', '_')}.mseed"
    selection = repo / "data/interim/discriminator/stations" / f"{event_group}.json"
    if not raw.exists() or not selection.exists():
        typer.secho("run `serac data build-discriminator-set` first", fg=typer.colors.RED)
        raise typer.Exit(2)

    stations = [
        StationChoice.model_validate(row)
        for row in _json.loads(selection.read_text(encoding="utf-8"))["stations"]
    ]
    waveform = np.zeros((MAX_STATIONS_PER_EVENT, len(COMPONENTS), N_SAMPLES), dtype=np.float32)
    valid = np.zeros((MAX_STATIONS_PER_EVENT, len(COMPONENTS)), dtype=bool)
    used: list[str] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from obspy import read, read_inventory

        stream = read(str(raw), format="MSEED")
        for station in stations:
            response = (
                repo
                / "data/raw/discriminator/responses"
                / f"{station.key.replace('.', '_')}_{positive.origin_utc.year}.xml"
            )
            if not response.exists() or response.stat().st_size == 0:
                continue
            picked = stream.select(
                network=station.network, station=station.station, channel=f"{station.band_code}H?"
            )
            if station.location:
                picked = picked.select(location=station.location)
            if len(picked) == 0:
                continue
            try:
                block, mask = process_station_window(
                    picked, read_inventory(str(response), format="STATIONXML"), positive, station
                )
            except Exception:
                continue
            if not mask.any():
                continue
            waveform[len(used)] = block
            valid[len(used)] = mask
            used.append(station.key)

    model = bl.load(repo / bl.ARTIFACT_DIR / scheme)
    features = np.array(
        [[compute_features(waveform, valid)[name] for name in FEATURE_NAMES]], dtype=np.float64
    )
    class_probabilities = model.class_probabilities(features)[0]
    probability = float(model.calibrated_probability(features)[0])
    predicted = bl.CLASSES[int(np.argmax(class_probabilities))]

    below_bar = len(used) < MIN_STATIONS_PER_WINDOW
    record = {
        "event_group": event_group,
        "origin_utc": positive.origin_utc.isoformat(),
        "receivers_selected": len(stations),
        "receivers_with_response_removed_data": len(used),
        "receivers_used": used,
        "min_stations_required_by_the_dataset": MIN_STATIONS_PER_WINDOW,
        "below_the_datasets_quality_bar": below_bar,
        "predicted_class": predicted,
        "calibrated_probability_mass_movement": probability,
        "class_probabilities": {
            name: float(v) for name, v in zip(bl.CLASSES, class_probabilities, strict=True)
        },
        "model": {
            "name": model.artifact.name,
            "split_scheme": model.artifact.split_scheme,
            "model_sha256": model.artifact.model_sha256,
            "train_event_groups_sha256": model.artifact.train_event_groups_sha256,
            "event_group_in_training": event_group in model.artifact.train_event_groups,
        },
        "caveat": (
            "This is a single-window case study, not a test-set metric, and it is never "
            f"averaged into one. The window has {len(used)} receiver(s) with response-removed "
            f"data against the dataset's minimum of {MIN_STATIONS_PER_WINDOW}, so it was "
            "excluded from the built dataset and recorded as `not_fetched` with that reason. "
            "The threshold was deliberately NOT lowered to admit it: moving a data-quality "
            "threshold after discovering it excludes the headline event is post-hoc tuning. "
            "The model here is the already-trained, already-sealed one, applied unchanged."
        ),
    }
    out = repo / REPORTS_DIR / f"case_study_{event_group}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    typer.echo(json.dumps(record, indent=2))
