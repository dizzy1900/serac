"""The line between "the code changed" and "the clock moved"."""

from __future__ import annotations

import pytest

from serac.validation.drift import (
    drift,
    is_volatile,
    measured_values,
    stable_text,
    stable_view,
)


@pytest.mark.parametrize(
    "key",
    ["started_utc", "finished_utc", "computed_utc", "generated_utc", "artifact_generated_utc"],
)
def test_a_timestamp_key_is_volatile(key: str) -> None:
    assert is_volatile(key)


@pytest.mark.parametrize("key", ["compute_seconds_total", "wall_clock_s"])
def test_a_measured_duration_is_volatile(key: str) -> None:
    assert is_volatile(key)


@pytest.mark.parametrize(
    "key", ["stopped_because", "chunks_ingested", "min_contributing_stations", "event_id", "fired"]
)
def test_everything_the_code_decides_is_not_volatile(key: str) -> None:
    assert not is_volatile(key)


def test_the_stable_view_drops_volatile_keys_at_every_depth() -> None:
    payload = {
        "started_utc": "2026-09-05T06:35:06+00:00",
        "stages": [{"wall_clock_s": 0.04, "outcome": "did_not_fire"}],
        "loss": {"computed_utc": "x", "by_asset": []},
    }
    assert stable_view(payload) == {
        "stages": [{"outcome": "did_not_fire"}],
        "loss": {"by_asset": []},
    }


def test_two_runs_that_differ_only_in_the_clock_do_not_drift() -> None:
    a = {"started_utc": "2026-09-05T06:35:06+00:00", "wall_clock_s": 0.043, "fired": False}
    b = {"started_utc": "2026-09-10T18:03:38+00:00", "wall_clock_s": 0.065, "fired": False}
    assert drift(a, b) == []


def test_a_change_in_what_the_code_did_is_reported_with_its_path() -> None:
    a = {"stages": [{"outcome": "did_not_fire"}]}
    b = {"stages": [{"outcome": "fired"}]}
    found = drift(a, b)
    assert len(found) == 1
    assert "$.stages[0].outcome" in found[0]
    assert "did_not_fire" in found[0]
    assert "fired" in found[0]


def test_an_added_and_a_removed_field_are_both_reported() -> None:
    found = drift({"a": 1, "gone": 2}, {"a": 1, "new": 3})
    assert any("gone" in f and "removed" in f for f in found)
    assert any("new" in f and "added" in f for f in found)


def test_a_list_that_changed_length_is_reported_once_not_element_by_element() -> None:
    found = drift({"s": [1, 2, 3]}, {"s": [1]})
    assert found == ["$.s: length 3 -> 1"]


def test_masking_a_document_hides_the_clock_and_keeps_the_prose() -> None:
    text = (
        "`serac cascade e2e` on serac 0.1.0, run 2026-09-05T06:35:06.176225+00:00.\n"
        '    "compute_seconds_total": 0.0348,\n'
        '    "wall_clock_s": 0.043,\n'
        "    stopped because no candidate in either mode\n"
    )
    masked = stable_text(text)
    assert "2026-09-05" not in masked
    assert "0.0348" not in masked
    assert "0.043" not in masked
    assert "stopped because no candidate in either mode" in masked
    assert masked.count("<utc>") == 1
    assert masked.count("<measured>") == 2


def test_two_documents_differing_only_in_the_clock_mask_to_the_same_text() -> None:
    a = 'run 2026-09-05T06:35:06.176225+00:00\n"wall_clock_s": 0.043\n'
    b = 'run 2026-09-10T18:03:38.431001+00:00\n"wall_clock_s": 0.065\n'
    assert stable_text(a) == stable_text(b)


def test_the_dropped_durations_are_still_collected_for_reporting() -> None:
    payload = {
        "stages": [{"wall_clock_s": 0.04}, {"measured": {"compute_seconds_total": 0.12}}],
        "started_utc": "ignored",
    }
    assert measured_values(payload) == {
        "$.stages[0].wall_clock_s": 0.04,
        "$.stages[1].measured.compute_seconds_total": 0.12,
    }


def test_a_timestamp_is_collected_as_a_duration_by_nobody() -> None:
    """`measured_values` is about numbers, so a clock string must not appear in it."""
    assert measured_values({"finished_utc": "2026-09-05T06:35:06+00:00"}) == {}
