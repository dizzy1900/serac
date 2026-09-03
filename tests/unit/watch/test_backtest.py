"""The walk-forward reporting layer, on fictional series with known answers."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from serac.models.watch.anomaly import Tier, UnitScore, UnitSeries, walk_forward
from serac.models.watch.backtest import (
    monthly_steps,
    observability_breakdown,
    summarise_steps,
)

RNG = np.random.default_rng(99)


def test_monthly_steps_are_first_of_month_and_inclusive_of_the_end() -> None:
    steps = monthly_steps(datetime(2020, 6, 15, tzinfo=UTC), datetime(2020, 10, 1, tzinfo=UTC))
    assert [s.date().isoformat() for s in steps] == [
        "2020-07-01",
        "2020-08-01",
        "2020-09-01",
        "2020-10-01",
    ]


def test_monthly_steps_cross_a_year_boundary() -> None:
    steps = monthly_steps(datetime(2020, 11, 1, tzinfo=UTC), datetime(2021, 2, 1, tzinfo=UTC))
    assert [s.date().isoformat() for s in steps] == [
        "2020-11-01",
        "2020-12-01",
        "2021-01-01",
        "2021-02-01",
    ]


def test_monthly_steps_is_empty_when_the_window_is_inverted() -> None:
    assert monthly_steps(datetime(2021, 2, 1, tzinfo=UTC), datetime(2020, 2, 1, tzinfo=UTC)) == []


def _score(unit: str, tier: Tier) -> UnitScore:
    return UnitScore(
        unit_id=unit,
        tier=tier,
        score=3.5 if tier is Tier.watch else 1.0,
        velocity_mm_yr=12.0,
        acceleration_mm_yr2=1.0,
        z_velocity=1.0,
        z_acceleration=1.0,
        n_samples=40,
        median_coherence=0.6,
    )


def test_other_watch_count_excludes_the_target_when_the_target_is_at_watch() -> None:
    steps = [datetime(2020, 1, 1, tzinfo=UTC)]
    scored = [
        {
            "target": _score("target", Tier.watch),
            "a": _score("a", Tier.watch),
            "b": _score("b", Tier.quiet),
        }
    ]
    rows = summarise_steps(steps, scored, "target")
    assert rows[0].n_watch == 2
    assert rows[0].n_other_watch == 1


def test_other_watch_count_is_the_full_count_when_the_target_is_not_at_watch() -> None:
    steps = [datetime(2020, 1, 1, tzinfo=UTC)]
    scored = [
        {
            "target": _score("target", Tier.quiet),
            "a": _score("a", Tier.watch),
            "b": _score("b", Tier.watch),
        }
    ]
    rows = summarise_steps(steps, scored, "target")
    assert rows[0].n_watch == 2
    assert rows[0].n_other_watch == 2


def test_a_missing_target_reports_insufficient_data_not_quiet() -> None:
    rows = summarise_steps(
        [datetime(2020, 1, 1, tzinfo=UTC)], [{"a": _score("a", Tier.quiet)}], "not-present"
    )
    assert rows[0].target_tier is Tier.insufficient_data
    assert rows[0].as_dict()["target_score"] is None


def test_step_rows_round_trip_to_plain_json_types() -> None:
    rows = summarise_steps(
        [datetime(2020, 1, 1, tzinfo=UTC)], [{"t": _score("t", Tier.watch)}], "t"
    )
    payload = rows[0].as_dict()
    assert payload["step"] == "2020-01-01"
    assert payload["target_tier"] == "watch"
    assert payload["target_score"] == pytest.approx(3.5)


def _series(unit_id: str, *, sensitivity: float = 0.8, inside: bool = True) -> UnitSeries:
    t = np.arange(160, dtype=np.float64) * 12.0
    return UnitSeries(
        unit_id=unit_id,
        t_days=t,
        los_mm=RNG.normal(0.0, 1.0, size=t.size),
        coherence=np.full(t.size, 0.7),
        los_sensitivity_signed=sensitivity,
        inside_footprint=inside,
    )


def test_observability_separates_never_observable_units_from_quiet_ones() -> None:
    units = {
        "seen-1": _series("seen-1"),
        "seen-2": _series("seen-2"),
        "blind": _series("blind", sensitivity=0.05),
        "outside": _series("outside", inside=False),
    }
    scored = walk_forward(units, list(np.arange(760.0, 1500.0, 30.4)))
    breakdown = observability_breakdown(scored)
    assert breakdown["n_units"] == 4
    assert breakdown["units_never_observable"] == 2
    assert breakdown["units_observable_at_any_step"] == 2
    reasons = breakdown["final_step_insufficient_by_reason"]
    assert reasons["low_los_sensitivity"] == 1
    assert reasons["outside_footprint"] == 1
    assert breakdown["final_step_quiet_and_observed"] == 2


def test_observability_of_an_empty_walk_forward_is_not_a_crash() -> None:
    assert observability_breakdown([]) == {"n_steps": 0}
