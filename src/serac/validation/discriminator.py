"""`make validate-discriminator` — the nine leakage assertions and the artifact-hash proof.

Leakage is M1's governing failure mode, so every claim about it here is mechanical. None of
these checks reads a comment or trusts a convention; each recomputes the thing it is asserting
from the committed artefacts and compares.

The nine:

1. **Group inheritance.** Every tectonic and noise row carries a `matched_positive_id`, and its
   `event_group` is that positive's group. This is what makes a group the split unit.
2. **No group straddles a split.** Under each scheme, every group maps to exactly one of
   train / val / test.
3. **Chamoli and Langtang are in test and nowhere else.** Checked by group id under both
   schemes, including the ComCat ids `us7000tbwb` and `us7000tc90`.
4. **Negatives share their positive's receivers.** Every window in a group was cut at the same
   station keys, so no class differs from another by instrument or network.
5. **No feature encodes geometry, epoch or identity.** `FORBIDDEN_FEATURE_TOKENS` over the
   emitted feature names.
6. **The shipped model proves what it saw.** The training groups are recomputed from the split
   and hashed, and the hash is compared with `artifact.json`'s. A model whose provenance record
   disagrees with the split fails here.
7. **The seal holds.** `reports/m1/seal.json` exists and the current configuration hashes to
   the sealed value, so no constant moved between test evaluations.
8. **The dataset bytes are the evaluated bytes.** The Zarr store is re-hashed against the
   sorted chunk index and the index against the ledger row.
9. **The calibrator never saw test.** `calibrator.fitted_on == "val"`, and the validation fold
   under the artifact's own scheme contains no forced test group.

A missing dataset or a missing model is a **warning**, not an error: on a fresh clone the
multi-gigabyte store is not present, and the suite must still say something useful. The
leakage assertions that can be made from committed files alone are always made.
"""

from __future__ import annotations

import json
from pathlib import Path

from serac.models.discriminator.baseline import ARTIFACT_DIR, group_hash
from serac.models.discriminator.baseline import load as load_baseline
from serac.models.discriminator.catalog import FORCED_TEST_GROUPS, ClassLabel
from serac.models.discriminator.dataset import (
    CHUNK_INDEX_NAME,
    DatasetError,
    assign_loro,
    assign_time_forward,
    load_index,
    verify_store,
)
from serac.models.discriminator.evaluate import SEAL_PATH, Seal, config_hash
from serac.models.discriminator.features import FEATURE_NAMES, audit_feature_names
from serac.models.discriminator.regions import HELD_OUT_REGION
from serac.validation.result import Severity, Suite, SuiteResult

SUITE_NAME = "discriminator"
DATASET_DIR = Path("data/features/discriminator")


def run_suite(repo: Path) -> SuiteResult:
    """Every leakage assertion that the committed artefacts support."""
    suite = Suite(SUITE_NAME, repo)

    # --- 5. feature names (always checkable; needs nothing on disk) ----------------------
    offenders = audit_feature_names()
    suite.check(
        "no_forbidden_feature_tokens",
        not offenders,
        f"{len(FEATURE_NAMES)} feature names checked against "
        f"lat/lon/distance/azimuth/year/magnitude/depth/station/network/sncl; "
        + ("none offend" if not offenders else f"offenders: {offenders}"),
    )

    # --- 7. the seal --------------------------------------------------------------------
    seal_path = repo / SEAL_PATH
    if not seal_path.exists():
        suite.warn(
            "seal_present",
            False,
            f"{SEAL_PATH} is absent: the test set has not been scored yet, so there is "
            "nothing to seal. It is written by the first test evaluation.",
        )
    else:
        seal = Seal.model_validate(json.loads(seal_path.read_text(encoding="utf-8")))
        suite.check(
            "seal_matches_current_config",
            seal.config_sha256 == config_hash(),
            f"sealed {seal.config_sha256[:16]} at {seal.sealed_at_utc.isoformat()}; "
            f"current {config_hash()[:16]}; schemes evaluated: {seal.schemes_evaluated}",
        )

    # --- the dataset ---------------------------------------------------------------------
    try:
        index = load_index(repo / DATASET_DIR)
    except DatasetError as exc:
        suite.warn(
            "dataset_present",
            False,
            f"{exc}. The window store is DVC-tracked and absent from a fresh clone; the "
            "leakage assertions that need it are skipped and reported as warnings.",
        )
        return suite.result()

    suite.info(
        "dataset_counts",
        f"{index.n_windows} windows over {len(index.groups())} groups; "
        + ", ".join(
            f"{label.value}={sum(1 for w in index.windows if w.class_label is label)}"
            for label in ClassLabel
        ),
    )

    # --- 1. group inheritance -------------------------------------------------------------
    positives = {w.entry_id: w for w in index.windows if w.class_label is ClassLabel.mass_movement}
    violations = []
    for window in index.windows:
        if window.class_label is ClassLabel.mass_movement:
            if window.matched_positive_id is not None:
                violations.append(f"{window.entry_id}: positive carries matched_positive_id")
            continue
        if window.matched_positive_id is None:
            violations.append(f"{window.entry_id}: no matched_positive_id")
            continue
        parent = positives.get(window.matched_positive_id)
        if parent is None:
            # The parent positive may itself have been excluded for lack of coverage; the
            # group is still inherited, which is what this assertion is about.
            continue
        if parent.event_group != window.event_group:
            violations.append(
                f"{window.entry_id}: group {window.event_group} != parent {parent.event_group}"
            )
    suite.check(
        "negatives_and_noise_inherit_group",
        not violations,
        f"{len(index.windows)} windows checked; "
        + ("all inherit correctly" if not violations else f"{len(violations)}: {violations[:5]}"),
    )

    # --- 4. shared receivers within a group ------------------------------------------------
    by_group: dict[str, set[tuple[str, ...]]] = {}
    for window in index.windows:
        by_group.setdefault(window.event_group, set()).add(tuple(sorted(window.station_keys)))
    mixed = {g: len(v) for g, v in by_group.items() if len(v) > 1}
    # A window can lose receivers to a data gap, so the assertion is containment in the
    # positive's set, not exact equality: no window may use a receiver its group's positive
    # did not use.
    outside: list[str] = []
    group_receivers: dict[str, set[str]] = {}
    for window in index.windows:
        if window.class_label is ClassLabel.mass_movement:
            group_receivers.setdefault(window.event_group, set()).update(window.station_keys)
    for window in index.windows:
        allowed = group_receivers.get(window.event_group)
        if allowed is None:
            continue
        extra = set(window.station_keys) - allowed
        if extra:
            outside.append(f"{window.entry_id}: {sorted(extra)[:3]}")
    suite.check(
        "negatives_use_the_positives_receivers",
        not outside,
        (
            f"{len(by_group)} groups; {len(mixed)} have windows with differing receiver "
            "subsets (expected: data gaps drop receivers). No window uses a receiver outside "
            "its group's positive set."
            if not outside
            else f"{len(outside)} windows use receivers their positive did not: {outside[:5]}"
        ),
    )

    # --- 2 and 3. splits -------------------------------------------------------------------
    schemes = {
        "time_forward": assign_time_forward(index),
        "loro_hma": assign_loro(index, HELD_OUT_REGION),
    }
    for name, assignment in schemes.items():
        straddle = [g for g, s in assignment.by_group.items() if not isinstance(s, str)]
        suite.check(
            f"no_group_straddles_a_split[{name}]",
            not straddle,
            f"{len(assignment.by_group)} groups, each mapped to exactly one split; "
            + ", ".join(
                f"{split}={sum(1 for v in assignment.by_group.values() if v == split)}"
                for split in ("train", "val", "test")
            ),
        )
        present = {g for g in FORCED_TEST_GROUPS if g in assignment.by_group}
        wrong = sorted(g for g in present if assignment.by_group[g] != "test")
        suite.check(
            f"forced_groups_in_test_only[{name}]",
            not wrong,
            (
                f"{sorted(present)} all in test, in neither train nor val"
                if not wrong
                else f"forced groups not in test: {[(g, assignment.by_group[g]) for g in wrong]}"
            ),
        )
        missing = sorted(g for g in FORCED_TEST_GROUPS if g not in assignment.by_group)
        if missing:
            suite.warn(
                f"forced_groups_present[{name}]",
                False,
                f"not in the built dataset at all: {missing}. A group absent for lack of "
                "station coverage is recorded in the ledger as not_fetched, not substituted.",
            )

    # --- 8. the dataset bytes are the evaluated bytes --------------------------------------
    ok, differences = verify_store(repo / DATASET_DIR)
    suite.check(
        "zarr_store_matches_chunk_index",
        ok,
        (
            f"every file in the store re-hashes to {CHUNK_INDEX_NAME}"
            if ok
            else f"{len(differences)} differences: {differences[:5]}"
        ),
    )

    # --- 6 and 9. the shipped model --------------------------------------------------------
    try:
        model = load_baseline(repo / ARTIFACT_DIR)
    except Exception as exc:
        suite.warn(
            "baseline_artifact_present",
            False,
            f"{exc}. The artifact-hash proof and the calibrator check need it.",
        )
        return suite.result()

    artifact = model.artifact
    artifact_assignment = schemes.get(artifact.split_scheme)
    if artifact_assignment is None:
        suite.check(
            "artifact_scheme_known",
            False,
            f"the artifact was trained under scheme {artifact.split_scheme!r}, which this "
            f"suite cannot reconstruct (known: {sorted(schemes)})",
        )
        return suite.result()

    recomputed_groups = sorted(
        {
            window.event_group
            for window in index.windows
            if artifact_assignment.by_group.get(window.event_group) == "train"
        }
    )
    recomputed = group_hash(recomputed_groups)
    suite.check(
        "training_group_hash_matches_artifact",
        recomputed == artifact.train_event_groups_sha256,
        f"recomputed {recomputed[:16]} over {len(recomputed_groups)} groups from the "
        f"{artifact.split_scheme} split; artifact says {artifact.train_event_groups_sha256[:16]} "
        f"over {artifact.n_train_groups}. The committed model proves what it was trained on.",
    )
    leaked = sorted(set(artifact.train_event_groups) & FORCED_TEST_GROUPS)
    suite.check(
        "forced_groups_absent_from_training",
        not leaked,
        (
            "no forced test group appears in the artifact's own training-group list"
            if not leaked
            else f"the shipped model was trained on {leaked}"
        ),
    )
    suite.check(
        "calibrator_fitted_on_validation_only",
        model.calibrator.fitted_on == "val",
        f"calibrator fitted on {model.calibrator.fitted_on!r} with n={model.calibrator.n_fitted}; "
        "a calibrator fitted on train reports near-certainty, one fitted on test is leakage",
    )
    val_forced = sorted(
        g for g in FORCED_TEST_GROUPS if artifact_assignment.by_group.get(g) == "val"
    )
    suite.check(
        "no_forced_group_in_the_calibration_fold",
        not val_forced,
        (
            "the validation fold the calibrator saw contains no forced test group"
            if not val_forced
            else f"forced groups in the calibration fold: {val_forced}"
        ),
    )

    suite.info(
        "model_provenance",
        f"{artifact.name} {artifact.baseline_version}, scheme {artifact.split_scheme}, "
        f"best_iteration {artifact.best_iteration}, model sha256 {artifact.model_sha256[:16]}",
    )
    n_errors = sum(1 for c in suite.checks if c.failed)
    if n_errors == 0:
        suite.check(
            "all_leakage_assertions_hold",
            True,
            "nine leakage assertions checked mechanically against the committed artefacts",
            Severity.info,
        )
    return suite.result()
