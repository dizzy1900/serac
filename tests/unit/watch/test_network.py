"""The SBAS network shape, on a fictional 12-day archive with hand-countable answers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from serac.models.watch.network import (
    Acquisition,
    acquisitions_from_bursts,
    budget,
    credits_for,
    plan_pairs,
)


def _archive(n: int, step_days: int = 12, start: str = "2016-01-05") -> list[Acquisition]:
    t0 = datetime.fromisoformat(start).replace(tzinfo=UTC)
    return [
        Acquisition(
            acquired_at=t0 + timedelta(days=step_days * i),
            granules=(
                f"S1_100001_IW1_{(t0 + timedelta(days=step_days * i)):%Y%m%dT%H%M%S}_VV_ABCD-BURST",
            ),
        )
        for i in range(n)
    ]


def test_n_conn_two_gives_two_pairs_per_acquisition_except_at_the_end() -> None:
    pairs = plan_pairs(_archive(5), n_conn=2, max_bt_days=36.0, annual_anchors=False)
    # 5 acquisitions: 4 first-neighbour + 3 second-neighbour = 7.
    assert len(pairs) == 7
    assert {p.kind for p in pairs} == {"short"}


def test_temporal_baseline_cap_drops_the_pairs_that_exceed_it() -> None:
    pairs = plan_pairs(_archive(5), n_conn=2, max_bt_days=12.0, annual_anchors=False)
    assert len(pairs) == 4
    assert max(p.temporal_baseline_days for p in pairs) == pytest.approx(12.0)


def test_a_gap_in_the_archive_does_not_create_a_long_pair() -> None:
    t0 = datetime(2016, 1, 5, tzinfo=UTC)
    acqs = [
        Acquisition(acquired_at=t0, granules=("a",)),
        Acquisition(acquired_at=t0 + timedelta(days=12), granules=("b",)),
        Acquisition(acquired_at=t0 + timedelta(days=200), granules=("c",)),
    ]
    pairs = plan_pairs(acqs, n_conn=2, max_bt_days=36.0, annual_anchors=False)
    assert [p.pair_id for p in pairs] == ["20160105_20160117"]


def test_annual_anchors_span_about_a_year_and_are_labelled() -> None:
    pairs = plan_pairs(_archive(120), n_conn=2, max_bt_days=36.0, annual_anchors=True)
    anchors = [p for p in pairs if p.kind == "anchor"]
    assert anchors, "a four-year archive should produce annual anchors"
    for anchor in anchors:
        assert 335.0 <= anchor.temporal_baseline_days <= 396.0


def test_anchors_never_duplicate_a_short_pair() -> None:
    pairs = plan_pairs(_archive(120), n_conn=2, max_bt_days=36.0, annual_anchors=True)
    ids = [p.pair_id for p in pairs]
    assert len(ids) == len(set(ids))


def test_planning_is_deterministic_and_order_independent() -> None:
    archive = _archive(30)
    first = plan_pairs(archive, n_conn=2, max_bt_days=36.0)
    second = plan_pairs(list(reversed(archive)), n_conn=2, max_bt_days=36.0)
    assert [p.pair_id for p in first] == [p.pair_id for p in second]


def test_plan_pairs_rejects_nonsense_parameters() -> None:
    with pytest.raises(ValueError, match="n_conn"):
        plan_pairs(_archive(3), n_conn=0)
    with pytest.raises(ValueError, match="max_bt_days"):
        plan_pairs(_archive(3), max_bt_days=0.0)


def test_incomplete_passes_are_dropped_rather_than_processed_short() -> None:
    t0 = datetime(2020, 6, 8, 12, 47, tzinfo=UTC)
    rows = [
        (t0, "S1_275111_IW3_20200608T124746_VV_7D8B-BURST"),
        (t0, "S1_275112_IW3_20200608T124749_VV_7D8B-BURST"),
        (t0 + timedelta(days=12), "S1_275111_IW3_20200620T124747_VV_5D11-BURST"),
    ]
    acqs = acquisitions_from_bursts(rows, required_burst_ids=["275111_IW3", "275112_IW3"])
    assert len(acqs) == 1
    assert acqs[0].acquired_at == t0


def test_credits_refuse_to_guess_outside_the_one_credit_tier() -> None:
    assert credits_for("20x4", 4) == 1
    assert credits_for("10x2", 3) == 1
    with pytest.raises(ValueError, match="1-credit tier"):
        credits_for("20x4", 5)
    with pytest.raises(ValueError, match="unknown looks"):
        credits_for("3x1", 1)


def test_budget_reports_unknown_transient_bytes_before_a_product_exists() -> None:
    pairs = plan_pairs(_archive(10), n_conn=2, max_bt_days=36.0, annual_anchors=False)
    b = budget(
        pairs,
        aoi_id="chamoli-rishiganga",
        path_number=129,
        looks="20x4",
        n_acquisitions=10,
        n_bursts=4,
        crop_pixels=400 * 450,
        retained_rasters=2,
        measured_product_bytes=None,
    )
    assert b.transient_bytes_estimate is None
    assert b.peak_disk_bytes_estimate is None
    assert any("no product size" in w for w in b.warnings)
    assert b.credits_total == len(pairs)
    assert b.retained_bytes_estimate == 400 * 450 * 4 * 2 * len(pairs)


def test_budget_uses_a_measured_size_once_one_exists() -> None:
    pairs = plan_pairs(_archive(4), n_conn=2, max_bt_days=36.0, annual_anchors=False)
    b = budget(
        pairs,
        aoi_id="chamoli-rishiganga",
        path_number=129,
        looks="20x4",
        n_acquisitions=4,
        n_bursts=4,
        crop_pixels=1000,
        retained_rasters=2,
        measured_product_bytes=50_000_000,
    )
    assert b.transient_bytes_estimate == 50_000_000 * len(pairs)
    assert b.warnings == []
    assert b.peak_disk_bytes_estimate is not None
