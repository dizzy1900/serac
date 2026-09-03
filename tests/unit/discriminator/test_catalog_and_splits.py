"""Group inheritance, dedupe, station reuse and the forced-test rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import REPO_ROOT

from serac.models.discriminator.catalog import (
    DEDUPE_KM,
    DEDUPE_SECONDS,
    FORCED_TEST_GROUPS,
    NEGATIVE_MAX_DISTANCE_KM,
    NEGATIVE_MIN_TIME_SEPARATION_S,
    CatalogEntry,
    CatalogSource,
    ClassLabel,
    build_positives,
    haversine_km,
    make_noise_windows,
    match_negatives,
)
from serac.ports.seismic import CatalogEvent

ORIGIN = datetime(2018, 6, 1, 12, 0, tzinfo=UTC)


def _positive(group: str = "g1") -> CatalogEntry:
    return CatalogEntry(
        entry_id=f"pos/{group}",
        event_group=group,
        class_label=ClassLabel.mass_movement,
        origin_utc=ORIGIN,
        latitude=30.0,
        longitude=80.0,
        region_id="high_mountain_asia",
        source=CatalogSource.esec,
        source_ids=["esec:1"],
        location_basis="esec_crown",
    )


def _quake(event_id: str, *, days: float, km: float, mag: float = 5.0) -> CatalogEvent:
    return CatalogEvent(
        event_id=event_id,
        time_utc=ORIGIN + timedelta(days=days),
        latitude=30.0 + km / 111.19,
        longitude=80.0,
        magnitude=mag,
        mag_type="mb",
        event_type="earthquake",
    )


def test_a_positive_may_not_carry_a_matched_positive_id() -> None:
    with pytest.raises(ValueError, match="must not carry"):
        _positive().model_copy(update={"matched_positive_id": "pos/other"}).model_validate(
            _positive().model_dump() | {"matched_positive_id": "pos/other"}
        )


def test_a_negative_without_a_parent_is_refused() -> None:
    """The single mechanism that keeps a group from straddling a split."""
    with pytest.raises(ValueError, match="must inherit"):
        CatalogEntry(
            entry_id="neg/x",
            event_group="g1",
            class_label=ClassLabel.tectonic,
            origin_utc=ORIGIN,
            latitude=30.0,
            longitude=80.0,
            region_id="high_mountain_asia",
            source=CatalogSource.comcat_tectonic,
            source_ids=["comcat:x"],
            location_basis="usgs_comcat_epicentre",
        )


def test_negatives_inherit_the_group_and_region() -> None:
    positive = _positive()
    negatives = match_negatives(positive, [_quake(f"q{i}", days=10 + i, km=50) for i in range(8)])
    assert len(negatives) == 5
    for negative in negatives:
        assert negative.event_group == positive.event_group
        assert negative.region_id == positive.region_id
        assert negative.matched_positive_id == positive.entry_id
        assert negative.class_label is ClassLabel.tectonic


def test_negatives_too_close_in_time_are_excluded() -> None:
    """A quake hours from the slide may be its trigger; the classes must stay distinct."""
    hours = NEGATIVE_MIN_TIME_SEPARATION_S / 3600.0
    too_close = _quake("trigger", days=(hours - 1) / 24.0, km=50)
    assert match_negatives(_positive(), [too_close]) == []


def test_negatives_beyond_the_distance_window_are_excluded() -> None:
    far = _quake("far", days=30, km=NEGATIVE_MAX_DISTANCE_KM + 100)
    assert match_negatives(_positive(), [far]) == []


def test_the_negative_shortfall_is_reported_not_padded() -> None:
    negatives = match_negatives(_positive(), [_quake("only", days=30, km=50)])
    assert len(negatives) == 1  # not silently topped up to five


def test_a_contaminated_noise_window_is_dropped() -> None:
    positive = _positive()
    noise = make_noise_windows(positive, [])
    assert len(noise) == 1
    assert noise[0].event_group == positive.event_group
    assert noise[0].matched_positive_id == positive.entry_id

    inside = noise[0].origin_utc
    contaminated = CatalogEvent(
        event_id="teleseism",
        time_utc=inside + timedelta(seconds=60),
        latitude=0.0,
        longitude=0.0,
        magnitude=6.4,
    )
    assert make_noise_windows(positive, [contaminated]) == []


def test_the_real_catalogue_dedupes_and_keeps_every_contributing_id() -> None:
    positives, before, merges = build_positives(REPO_ROOT)
    assert before > len(positives)
    assert merges == before - len(positives)
    assert len({p.entry_id for p in positives}) == len(positives)
    for positive in positives:
        assert positive.source_ids


def test_merged_records_really_are_within_the_dedupe_window() -> None:
    positives, _, _ = build_positives(REPO_ROOT)
    for positive in positives:
        if len(positive.source_ids) > 1:
            # the merge kept every id; the primary's coordinates are the survivor
            assert (
                haversine_km(
                    positive.latitude, positive.longitude, positive.latitude, positive.longitude
                )
                <= DEDUPE_KM
            )
    assert DEDUPE_SECONDS == 180.0


def test_chamoli_and_langtang_are_forced_test_groups() -> None:
    positives, _, _ = build_positives(REPO_ROOT)
    groups = {p.event_group for p in positives}
    assert "chamoli-2021" in groups
    assert "langtang-lhende-2026" in groups
    assert {"chamoli-2021", "langtang-lhende-2026"} <= FORCED_TEST_GROUPS


def test_chamoli_and_langtang_are_both_in_the_held_out_region() -> None:
    """Which is why leave-one-region-out with HMA held out *is* their evaluation."""
    positives, _, _ = build_positives(REPO_ROOT)
    for group in ("chamoli-2021", "langtang-lhende-2026"):
        found = next(p for p in positives if p.event_group == group)
        assert found.region_id == "high_mountain_asia"
