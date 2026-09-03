"""The watch contract must make an unmeasurable slope impossible to confuse with a quiet one."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from serac.domain.watch import (
    SlopeWatchState,
    WatchInsufficientReason,
    WatchTier,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SHA = "a" * 64


def state(**kw: object) -> SlopeWatchState:
    base: dict[str, object] = {
        "aoi_id": "test-aoi",
        "unit_id": "su-00001",
        "as_of_utc": NOW,
        "tier": WatchTier.quiet,
        "score": 0.4,
        "n_samples": 12,
        "method_id": "test-method",
        "preregistration_sha256": SHA,
    }
    base.update(kw)
    return SlopeWatchState(**base)  # type: ignore[arg-type]


def test_an_assessed_unit_needs_a_score() -> None:
    with pytest.raises(ValidationError, match="requires a score"):
        state(score=None)


def test_insufficient_data_carries_no_score_and_needs_a_reason() -> None:
    with pytest.raises(ValidationError, match="has no score"):
        state(tier=WatchTier.insufficient_data)
    with pytest.raises(ValidationError, match="measured reason"):
        state(tier=WatchTier.insufficient_data, score=None)
    ok = state(
        tier=WatchTier.insufficient_data,
        score=None,
        insufficient_reason=WatchInsufficientReason.low_coherence,
    )
    assert ok.tier is WatchTier.insufficient_data


def test_an_assessed_unit_cannot_claim_it_was_unmeasurable() -> None:
    with pytest.raises(ValidationError, match="cannot carry an insufficient_reason"):
        state(insufficient_reason=WatchInsufficientReason.low_coherence)


def test_the_contract_cannot_express_a_failure_date_or_probability() -> None:
    # The point of the component: serac does not predict when a slope will fail, so there is
    # no field to put such a claim in, and extras are forbidden.
    names = set(SlopeWatchState.model_fields)
    assert not [n for n in names if "date" in n or "probability" in n or "forecast" in n]
    with pytest.raises(ValidationError):
        state(failure_date=NOW)
    with pytest.raises(ValidationError):
        state(probability_of_failure=0.3)


def test_score_is_named_as_a_z_score_not_a_likelihood() -> None:
    field = SlopeWatchState.model_fields["score"]
    assert "NOT a probability" in (field.description or "")
