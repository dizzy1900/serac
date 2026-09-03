"""Golden test: the stub's ratio sequence on the real Chamoli fixture is pinned.

The golden file records values, not a verdict. Regenerate deliberately with
`serac stream golden --update` or by running this test with `SERAC_UPDATE_GOLDEN=1`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from serac.streaming.golden import (
    compute_golden,
    diff_golden,
    golden_path,
    load_golden,
    write_golden,
)

EVENT = "chamoli-2021"


@pytest.fixture(scope="module")
def actual(repo_root: Path) -> dict[str, object]:
    return compute_golden(repo_root, EVENT)


def test_golden_ratio_sequence_matches(repo_root: Path, actual: dict[str, object]) -> None:
    path = golden_path(repo_root, EVENT)
    if os.environ.get("SERAC_UPDATE_GOLDEN") == "1":
        write_golden(actual, path)
    assert path.exists(), f"{path} missing; run `serac stream golden --update`"
    expected = load_golden(path)
    diff = diff_golden(expected, actual)
    assert diff == [], "\n".join(diff)


def test_golden_records_the_real_fixture_hashes(repo_root: Path, actual: dict[str, object]) -> None:
    fixtures = actual["fixtures"]
    assert isinstance(fixtures, list) and len(fixtures) == 3
    assert all(f["provenance"] == "real" for f in fixtures)
    assert all(f["path"].startswith("data/fixtures/seismic/chamoli-2021/") for f in fixtures)


def test_golden_fired_is_an_observation_not_a_target(actual: dict[str, object]) -> None:
    # Whatever the count, it is recorded; nothing here requires it to be zero or non-zero.
    assert isinstance(actual["n_fired"], int)
    assert actual["params"]["threshold_is_placeholder"] is True  # type: ignore[index]
    assert "not a target" in str(actual["note"])
