"""Gap 17: equalising realised receiver counts so `n_stations` stops being a classifier."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from serac.models.discriminator.balance import (
    POSITIVE_LABEL,
    balance_report,
    group_quotas,
    n_stations_auc,
    orphaned_windows,
    roc_auc,
    station_masks,
)
from serac.models.discriminator.dataset import DatasetIndex, WindowRecord, load_index

STORE = Path("data/features/discriminator")


def _window(
    index: int,
    entry_id: str,
    label: str,
    group: str,
    stations: list[str],
    matched: str | None = None,
) -> WindowRecord:
    return WindowRecord(
        index=index,
        entry_id=entry_id,
        event_group=group,
        class_label=label,
        origin_utc=datetime(2020, 1, 1, tzinfo=UTC),
        region_id="r",
        decade="2020s",
        source="test",
        source_ids=["x"],
        matched_positive_id=matched,
        station_keys=stations,
        n_stations=len(stations),
        n_valid_channels=len(stations) * 3,
    )


def _index(windows: list[WindowRecord]) -> DatasetIndex:
    return DatasetIndex(
        built_at_utc=datetime(2020, 1, 1, tzinfo=UTC),
        n_windows=len(windows),
        sampling_rate_hz=20.0,
        bandpass_hz=(0.01, 1.0),
        windows=windows,
    )


@pytest.fixture
def toy() -> DatasetIndex:
    """One group that leaks the way the real store does: the positive realised more receivers
    than either of its negatives, so a classifier given only the count separates them exactly."""
    return _index(
        [
            _window(0, "p1", POSITIVE_LABEL, "g1", ["A", "B", "C", "D", "E"]),
            _window(1, "n1", "tectonic", "g1", ["A", "B", "C"], matched="p1"),
            _window(2, "n2", "tectonic", "g1", ["A", "B", "C", "D"], matched="p1"),
        ]
    )


# ---------------------------------------------------------------- the metric


def test_a_perfectly_separating_count_scores_one() -> None:
    assert roc_auc([9, 8, 7], [3, 2, 1]) == pytest.approx(1.0)


def test_an_identical_count_scores_a_half_through_mid_ranks() -> None:
    """Receiver counts are small integers, so ties carry most of the mass."""
    assert roc_auc([5, 5, 5], [5, 5, 5]) == pytest.approx(0.5)


def test_an_empty_class_has_no_auc() -> None:
    import math

    assert math.isnan(roc_auc([], [1, 2]))


# ---------------------------------------------------------------- the rule


def test_the_quota_is_the_smallest_realised_count_in_the_group(toy: DatasetIndex) -> None:
    quota = group_quotas(toy)["p1"]
    assert quota.quota == 3
    assert quota.realised == (5, 3, 4)
    assert quota.receivers_dropped == (5 - 3) + (3 - 3) + (4 - 3)


def test_every_member_of_a_group_keeps_the_same_number_of_slots(toy: DatasetIndex) -> None:
    masks = station_masks(toy)
    assert {len(m) for m in masks.values()} == {3}


def test_slots_are_kept_from_the_front_so_identity_matching_survives(toy: DatasetIndex) -> None:
    masks = station_masks(toy)
    assert masks["p1"] == (0, 1, 2)
    kept = [toy.windows[0].station_keys[i] for i in masks["p1"]]
    assert kept == ["A", "B", "C"]


def test_the_rule_removes_the_signal_it_exists_to_remove(toy: DatasetIndex) -> None:
    assert n_stations_auc(toy) == pytest.approx(1.0), "the toy must leak, or it tests nothing"
    assert n_stations_auc(toy, masks=station_masks(toy)) == pytest.approx(0.5)


def test_a_window_in_no_group_keeps_every_slot_it_realised() -> None:
    """Balancing is a statement about a comparison; an unmatched window has none to make."""
    idx = _index([_window(0, "solo", "noise", "g9", ["A", "B"], matched=None)])
    assert station_masks(idx)["solo"] == (0, 1)


def test_an_orphan_is_named_rather_than_silently_carried() -> None:
    idx = _index(
        [
            _window(0, "p1", POSITIVE_LABEL, "g1", ["A", "B"]),
            _window(1, "n1", "tectonic", "g1", ["A"], matched="p1"),
            _window(2, "n9", "tectonic", "g9", ["A", "B", "C", "D"], matched="absent"),
        ]
    )
    assert orphaned_windows(idx) == ("n9",)
    assert station_masks(idx)["n9"] == (0, 1, 2, 3), "an orphan cannot be balanced"


# ---------------------------------------------------------------- the committed index


@pytest.fixture(scope="module")
def real_index() -> DatasetIndex:
    if not (STORE / "windows.json").exists():
        pytest.skip(f"{STORE}/windows.json is not in this clone")
    return load_index(STORE)


def test_the_leak_is_present_and_the_rule_closes_it(real_index: DatasetIndex) -> None:
    """The measurement Gap 17 reports, and the one this module adds."""
    report = balance_report(real_index)
    assert report.auc_before > 0.55, "the leak the ledger describes must still be there"
    assert report.auc_after < 0.52
    assert report.auc_after_excluding_orphans == pytest.approx(0.5, abs=0.01)


def test_the_rule_costs_receivers_and_the_cost_is_reported(real_index: DatasetIndex) -> None:
    report = balance_report(real_index)
    before = report.mean_receivers_before[POSITIVE_LABEL]
    after = report.mean_receivers_after[POSITIVE_LABEL]
    assert after < before
    assert report.receivers_dropped > 0
    assert report.n_groups_emptied == 0, "no group should be lost entirely by the count quota"


def test_truncation_does_not_degrade_the_identity_matching(real_index: DatasetIndex) -> None:
    """Slots are in the requested order, so the front k are largely the same stations."""
    report = balance_report(real_index)
    assert report.mean_station_identity_agreement > 0.8


def test_the_committed_index_carries_orphans_and_they_are_the_residual(
    real_index: DatasetIndex,
) -> None:
    """A defect nothing checked before: a negative whose matched positive is not in the index."""
    report = balance_report(real_index)
    assert len(report.orphan_ids) == 34
    assert report.auc_after_excluding_orphans < report.auc_after
