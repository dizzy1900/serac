"""Reading the upstream components' artifacts, the ensemble prior, and the printed table.

`test_the_press_timings_are_unreadable_downstream` is the one that matters to a reviewer: the
M4 sanity artifact carries press-reported timings, and nothing that feeds a forecast may read
them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from serac.cascade.compute import compute_avoided_loss
from serac.cascade.evidence import (
    FORBIDDEN_SANITY_KEYS,
    EvidenceError,
    Execution,
    StageOutcome,
    _percentile,
    discriminator_latency,
    ensemble_arrivals,
    lfh_outcome,
    read_sanity_key,
    surrogate_latency_s,
)
from serac.cascade.exposure import bundle_from, load_exposure
from serac.cascade.prior import (
    NOT_A_FORECAST,
    ensemble_prior_forecast,
    issue_delay,
)
from serac.cascade.table import NO_VALIDATED_FORECAST, print_loss_table, render_loss_table
from serac.cascade.underwriting import UNDERWRITING_AOI, UNDERWRITING_EVENT, underwriting_table

# -- the guard on the press-derived keys ----------------------------------------------------------


@pytest.mark.parametrize("key", sorted(FORBIDDEN_SANITY_KEYS))
def test_the_press_timings_are_unreadable_downstream(repo_root: Path, key: str) -> None:
    with pytest.raises(EvidenceError, match="press-reported timings"):
        read_sanity_key(repo_root, key)


def test_a_permitted_key_still_reads(repo_root: Path) -> None:
    assert read_sanity_key(repo_root, "frozen_design_hash")


def test_the_arrival_reader_only_touches_the_modelled_arrivals(repo_root: Path) -> None:
    stats, evidence = ensemble_arrivals(repo_root)
    assert evidence.execution is Execution.artifact
    assert evidence.artifact_sha256
    reached = [s for s in stats if s.members_reaching]
    assert reached, "at least one transect must be reached by at least one member"
    for stat in stats:
        assert stat.members_total > 0
        if stat.members_reaching:
            assert stat.p5_min is not None and stat.p95_min is not None
            assert stat.p5_min <= stat.p95_min
        else:
            assert stat.p5_min is None
    # Not one press-derived key appears in what the reader carried forward.
    payload = json.dumps(evidence.measured)
    for forbidden in FORBIDDEN_SANITY_KEYS:
        assert forbidden not in payload


def test_percentiles_interpolate() -> None:
    assert _percentile([1.0, 2.0, 3.0], 0.5) == pytest.approx(2.0)
    assert _percentile([1.0, 3.0], 0.5) == pytest.approx(2.0)
    assert _percentile([5.0], 0.95) == 5.0


# -- upstream outcomes ----------------------------------------------------------------------------


def test_m2_refusals_are_read_as_refusals_and_block_downstream(repo_root: Path) -> None:
    for event in ("chamoli-2021", "langtang-lhende-2026"):
        evidence = lfh_outcome(repo_root, event)
        assert evidence.outcome is StageOutcome.refused
        assert evidence.blocks_downstream
        assert "REFUSED" in evidence.summary
        assert evidence.measured["mass"] is None


def test_m1_reports_whether_the_detector_fired(repo_root: Path) -> None:
    langtang = discriminator_latency(repo_root, "langtang-lhende-2026")
    assert langtang.outcome is StageOutcome.did_not_fire
    assert langtang.blocks_downstream

    chamoli = discriminator_latency(repo_root, "chamoli-2021")
    assert chamoli.outcome is StageOutcome.produced
    assert not chamoli.blocks_downstream


def test_a_missing_artifact_is_unavailable_rather_than_an_exception(tmp_path: Path) -> None:
    evidence = discriminator_latency(tmp_path, "nothing")
    assert evidence.outcome is StageOutcome.unavailable
    assert evidence.blocks_downstream


def test_the_surrogate_latency_is_read_from_m4s_own_metrics(repo_root: Path) -> None:
    latency = surrogate_latency_s(repo_root)
    assert latency is not None and 0 < latency < 2.0


# -- the prior ------------------------------------------------------------------------------------


def test_the_prior_forecast_never_claims_a_central_estimate(repo_root: Path) -> None:
    stats, _ = ensemble_arrivals(repo_root)
    from datetime import UTC, datetime

    when = datetime(2026, 8, 26, 2, 52, 10, tzinfo=UTC)
    forecast = ensemble_prior_forecast(
        repo_root,
        aoi_id=UNDERWRITING_AOI,
        event_id=UNDERWRITING_EVENT,
        stats=stats,
        origin_time_utc=when,
        issued_utc=when,
    )
    assert forecast.confidence_tier.value == "unqualified"
    assert forecast.source_volume_m3.best is None
    assert forecast.runout_km.best is None
    assert forecast.footprint is None
    assert forecast.source_location is None
    assert NOT_A_FORECAST in forecast.assumptions
    for arrival in forecast.transect_arrivals:
        assert arrival.arrival_time_min.best is None
        # The committed ensemble artifact records arrivals, not stages. Nothing fills this in.
        assert arrival.peak_stage_m is None
        assert NOT_A_FORECAST in (arrival.arrival_time_min.notes or "")


def test_the_counterfactual_delay_is_built_from_measured_terms(repo_root: Path) -> None:
    delay = issue_delay(repo_root, UNDERWRITING_EVENT)
    assert delay.total_s is not None
    assert delay.detection_s > 0 and delay.lfh_s is not None and delay.surrogate_s is not None
    note = delay.as_note()
    assert note.startswith("COUNTERFACTUAL")
    assert "not a delivered lead time" in note


# -- exposure -------------------------------------------------------------------------------------


def test_the_lhende_exposure_is_read_and_its_gaps_are_stated(repo_root: Path) -> None:
    exposure = load_exposure(repo_root, UNDERWRITING_AOI)
    assert len(exposure.items) == 14
    assert exposure.capacities["rasuwagadhi-hep"].best == 111.0
    assert all(item.replacement_value is None for item in exposure.items)
    assert all(item.population is None for item in exposure.items)
    joined = " ".join(exposure.gaps)
    assert "population=null" in joined
    assert "no replacement value" in joined


def test_bundle_from_needs_no_aoi_directory(repo_root: Path) -> None:
    exposure = load_exposure(repo_root, UNDERWRITING_AOI)
    rebuilt = bundle_from(
        __import__("serac.pipelines.aoi_build", fromlist=["read_aoi_dir"])
        .read_aoi_dir(repo_root / "data" / "aoi" / UNDERWRITING_AOI)
        .aoi,
        exposure.assets,
        exposure.transects,
    )
    assert [i.asset_id for i in rebuilt.items] == [i.asset_id for i in exposure.items]


# -- the table ------------------------------------------------------------------------------------


def test_the_underwriting_table_computes_and_costs_nothing_it_cannot(repo_root: Path) -> None:
    table = underwriting_table(repo_root)
    assert table.total == 14
    assert table.costed == 0
    assert len(table.result.undetermined) == 14
    assert table.result.response.model is not None


def test_every_rendering_leads_with_the_provenance_header(repo_root: Path) -> None:
    table = underwriting_table(repo_root)
    text = render_loss_table(table.result, table.exposure)
    lines = print_loss_table(table.result, table.exposure)
    for rendering in (text, "\n".join(lines)):
        assert "INPUT PROVENANCE" in rendering
        assert NO_VALIDATED_FORECAST in rendering
        assert "FROZEN ENSEMBLE'S DESIGN PRIOR" in rendering
        assert "ASSUMPTION" in rendering
    # Every asset appears, so none of them can be read as an asset with no loss.
    for item in table.exposure.items:
        assert item.asset_id in text
    assert "UNDETERMINED" in "\n".join(lines)


def test_the_table_shows_a_costed_row_when_the_input_supports_one() -> None:
    from serac.alerting.example import check_request

    result = compute_avoided_loss(check_request())
    exposure = bundle_from(
        __import__("serac.domain.geo", fromlist=["AOI"]).AOI.model_construct(
            id="fictional-check-aoi", name="fictional", transects=[]
        ),
        [],
        [],
    )
    text = render_loss_table(result, exposure)
    assert "costed" in text
    assert "Totals" in text
