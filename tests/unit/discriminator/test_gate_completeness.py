"""The gate must fail on the leak the reviewer demonstrated, and must not go green on nothing.

Two regressions are pinned here.

**The orphan leak.** 34 windows in the built store have a parent positive that was excluded
for thin coverage. The gate used to `continue` past them, so their `event_group` -- the only
thing keeping Langtang's five rows out of training -- was taken entirely on trust. The
reviewer moved one orphan into a training group and every assertion still passed. That exact
mutation is reproduced below and must now fail.

**Green on an empty tree.** The suite used to return early with a warning when the window
store was absent, which is the CI case, so it reported `passed` having proved almost nothing.
The window index is now committed, so the leakage assertions run on a fresh clone; when even
the index is gone the suite must fail rather than go green.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from tests.conftest import REPO_ROOT

from serac.validation.discriminator import DATASET_DIR, run_suite
from serac.validation.result import Severity

# The orphan the reviewer mutated, its true group, and the training group it was moved into.
ORPHAN_ENTRY_ID = "neg/esec-9/uw10454828"
ORPHAN_TRUE_GROUP = "esec-9"
A_TRAINING_GROUP = "esec-7"

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / DATASET_DIR / "windows.json").exists(),
    reason="the committed window index is required for the gate regression tests",
)


def _repo_with_index(tmp_path: Path) -> Path:
    """A repo view carrying everything the gate reads, with a writable window index."""
    for name in ("data/fixtures", "data/events", "data/interim", "baselines", "reports"):
        source = REPO_ROOT / name
        if not source.exists():
            continue
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(source, target_is_directory=True)
    features = tmp_path / DATASET_DIR
    features.mkdir(parents=True, exist_ok=True)
    for name in ("windows.json", "chunk_hashes.tsv"):
        shutil.copy2(REPO_ROOT / DATASET_DIR / name, features / name)
    return tmp_path


def _mutate_group(repo: Path, entry_id: str, new_group: str) -> None:
    path = repo / DATASET_DIR / "windows.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    hits = [w for w in document["windows"] if w["entry_id"] == entry_id]
    assert hits, f"{entry_id} is not in the committed index"
    for window in hits:
        window["event_group"] = new_group
    path.write_text(json.dumps(document), encoding="utf-8")


def test_the_unmutated_index_has_no_group_inheritance_failure(tmp_path: Path) -> None:
    result = run_suite(_repo_with_index(tmp_path))
    check = next(c for c in result.checks if c.name == "negatives_and_noise_inherit_group")
    assert check.ok, check.details
    assert "orphans verified against the catalogue" in check.details


def test_moving_an_orphan_into_a_training_group_is_caught(tmp_path: Path) -> None:
    """The exact mutation that passed all nine assertions before."""
    repo = _repo_with_index(tmp_path)
    _mutate_group(repo, ORPHAN_ENTRY_ID, A_TRAINING_GROUP)
    result = run_suite(repo)

    check = next(c for c in result.checks if c.name == "negatives_and_noise_inherit_group")
    assert not check.ok, "the orphan's group was taken on trust again"
    assert ORPHAN_ENTRY_ID in check.details
    assert ORPHAN_TRUE_GROUP in check.details

    leakage = next(c for c in result.checks if c.name == "no_training_set_leakage")
    assert not leakage.ok
    assert leakage.severity is Severity.error


def test_an_orphan_moved_into_a_forced_test_group_is_also_caught(tmp_path: Path) -> None:
    """The mirror image: smuggling rows *into* the held-out group is a leak too."""
    repo = _repo_with_index(tmp_path)
    _mutate_group(repo, ORPHAN_ENTRY_ID, "chamoli-2021")
    result = run_suite(repo)
    check = next(c for c in result.checks if c.name == "negatives_and_noise_inherit_group")
    assert not check.ok


def test_a_missing_window_index_fails_rather_than_reporting_green(tmp_path: Path) -> None:
    """`passed` must mean `proved`. An empty tree proves nothing and must not go green."""
    result = run_suite(tmp_path)
    assert not result.passed, "the suite reported green having proved nothing about leakage"
    unprovable = next(c for c in result.checks if c.name == "leakage_criteria_provable")
    assert not unprovable.ok
    assert unprovable.severity is Severity.error


def test_an_absent_zarr_store_is_a_named_warning_not_a_silent_skip(tmp_path: Path) -> None:
    """Only the byte re-hash needs the multi-gigabyte store; everything else still runs."""
    repo = _repo_with_index(tmp_path)
    result = run_suite(repo)
    names = {c.name for c in result.checks}
    assert "zarr_store_present_for_byte_verification" in names
    assert "zarr_store_matches_chunk_index" not in names
    skipped = next(c for c in result.checks if c.name == "zarr_store_present_for_byte_verification")
    assert not skipped.ok
    assert skipped.severity is Severity.warning
    # ...and the leakage assertions that do not need the store all ran.
    for required in (
        "negatives_and_noise_inherit_group",
        "no_group_straddles_a_split[loro_hma]",
        "forced_groups_in_test_only[loro_hma]",
        "training_group_hash_matches_artifact",
        "calibrator_fitted_on_validation_only",
    ):
        assert required in names, f"{required} did not run without the store"


def test_the_brief_criteria_are_checked_and_the_unmet_one_fails(tmp_path: Path) -> None:
    """Langtang is not detected; a gate that goes green anyway is the thing we cannot ship."""
    result = run_suite(_repo_with_index(tmp_path))
    names = {c.name for c in result.checks}
    assert "forced_groups_detected[loro_hma]" in names
    assert "f1_beats_the_trivial_baseline[loro_hma]" in names
    assert "shipped_default_f1_at_least_the_alternative" in names

    detection = next(c for c in result.checks if c.name == "forced_groups_detected[loro_hma]")
    assert detection.severity is Severity.criterion_unmet
    assert not detection.ok, "Langtang is not detected, so this criterion is unmet"
    assert detection.failed, "an unmet criterion must fail the suite"
    assert not result.passed
    assert "forced_groups_detected[loro_hma]" in result.unmet_criteria


def test_a_forced_id_that_names_no_group_at_all_is_an_error(tmp_path: Path) -> None:
    """A forced id nothing resolves is not 'fine', it means nothing keeps it out of training."""
    from serac.models.discriminator import catalog as cat

    repo = _repo_with_index(tmp_path)
    original = cat.FORCED_TEST_ALIASES.copy()
    try:
        cat.FORCED_TEST_ALIASES.pop("us7000tbwb")
        result = run_suite(repo)
        check = next(c for c in result.checks if c.name == "forced_groups_in_test_only[loro_hma]")
        assert not check.ok
        assert "us7000tbwb" in check.details
    finally:
        cat.FORCED_TEST_ALIASES.clear()
        cat.FORCED_TEST_ALIASES.update(original)
