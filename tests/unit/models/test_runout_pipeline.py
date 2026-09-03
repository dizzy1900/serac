"""Runner caching, ensemble freezing, cascade rules and the forecast contract.

All offline: the corridor terrain these exercise is a small synthetic ramp built in-process,
never written under `data/`, so none of this depends on the fetched corridor DEM.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from serac.domain.geo import GridSpec
from serac.models.runout.cascade import (
    DAMMING_V0_LABEL,
    Constriction,
    breach_hydrograph,
    damming_index,
    find_constrictions,
    index_to_probability,
    secondary_surge,
)
from serac.models.runout.ensemble import (
    EnsembleDesign,
    design_from_payload,
    latin_hypercube,
    read_frozen_design,
    write_frozen,
)
from serac.models.runout.langtang import FORBIDDEN_VOCABULARY
from serac.models.runout.params import NOT_RAVAFLOW, SOLVER_VERSION, VoellmyParameters
from serac.models.runout.release import emplace_release
from serac.models.runout.terrain import CorridorTerrain, priority_flood_fill


def _ramp_terrain(tmp_path: Path) -> CorridorTerrain:
    """A 40 x 120 corridor sloping steadily downhill, with a straight centreline."""
    height, width, res = 40, 120, 30.0
    grid = GridSpec(
        aoi_id="test-aoi",
        epsg=32645,
        resolution_m=res,
        x_min=300_000.0,
        y_min=3_100_020.0,
        x_max=300_000.0 + width * res,
        y_max=3_100_020.0 + height * res,
        width=width,
        height=height,
    )
    x = np.arange(width) * res
    bed = (4000.0 - 0.15 * x)[None, :] * np.ones((height, 1))
    mask = np.zeros((height, width), dtype=bool)
    mask[14:26, :] = True
    outflow = np.zeros((height, width), dtype=bool)
    outflow[14:26, -1] = True
    chainage = np.broadcast_to(x[None, :], (height, width)).astype(np.float32).copy()
    offset = np.zeros((height, width), dtype=np.float32)
    return CorridorTerrain(
        grid=grid,
        elevation_raw=bed.astype(np.float32),
        elevation=bed.astype(np.float32),
        domain_mask=mask,
        outflow_mask=outflow,
        erodible_depth=(mask * 1.0).astype(np.float32),
        chainage_m=chainage,
        offset_m=offset,
        frame_valid=mask,
        fill_cells=0,
        fill_volume_m3=0.0,
        unreachable_cells=0,
        dem_sha256="0" * 64,
        dem_path="tests/synthetic-ramp",
    )


# -- terrain conditioning --------------------------------------------------------------------


def test_priority_flood_fills_a_pit_and_reports_what_it_reached() -> None:
    elevation = np.array(
        [
            [10.0, 10.0, 10.0, 10.0],
            [10.0, 2.0, 3.0, 10.0],
            [10.0, 3.0, 4.0, 10.0],
            [10.0, 10.0, 10.0, 5.0],
        ]
    )
    mask = np.ones((4, 4), dtype=bool)
    seeds = np.zeros((4, 4), dtype=bool)
    seeds[3, 3] = True

    filled, reached = priority_flood_fill(elevation, mask, seeds, 1e-3)

    assert reached.all()
    assert filled[1, 1] > elevation[1, 1], "the pit must be filled"
    assert filled[3, 3] == pytest.approx(elevation[3, 3]), "the outlet must not move"
    assert (filled >= elevation - 1e-12).all(), "filling never lowers ground"


def test_priority_flood_reports_cells_it_cannot_reach() -> None:
    """An island inside the mask must be flagged, not left unconditioned and silent."""
    elevation = np.zeros((5, 5))
    mask = np.zeros((5, 5), dtype=bool)
    mask[0, 0] = True  # the seed
    mask[4, 4] = True  # disconnected
    seeds = np.zeros((5, 5), dtype=bool)
    seeds[0, 0] = True

    _, reached = priority_flood_fill(elevation, mask, seeds, 1e-3)

    assert reached[0, 0]
    assert not reached[4, 4]


# -- release emplacement ----------------------------------------------------------------------


def test_release_is_emplaced_and_conserves_the_requested_volume(tmp_path: Path) -> None:
    terrain = _ramp_terrain(tmp_path)
    parameters = VoellmyParameters(
        release_volume_m3=2.0e6,
        ice_fraction=0.4,
        release_elevation_band_m=(3900.0, 4000.0),
        entrainment_coefficient=0.001,
        mu=0.1,
        xi_m_s2=1000.0,
    )

    emplacement = emplace_release(terrain, parameters)

    assert emplacement.cells > 0
    assert emplacement.emplaced_volume_m3 == pytest.approx(2.0e6, rel=1e-9)
    assert emplacement.shortfall_fraction < 1e-9


# -- ensemble design --------------------------------------------------------------------------


def test_latin_hypercube_is_deterministic_and_stratified() -> None:
    a = latin_hypercube(50, 7, seed=1)
    b = latin_hypercube(50, 7, seed=1)
    assert np.array_equal(a, b)
    assert latin_hypercube(50, 7, seed=2).tolist() != a.tolist()
    for d in range(7):
        # exactly one sample per stratum
        strata = np.floor(a[:, d] * 50).astype(int)
        assert sorted(strata) == list(range(50))


def _design(members: int = 12) -> EnsembleDesign:
    return EnsembleDesign(
        n_members=members,
        seed=7,
        resolutions=((30.0, members),),
        settings_template={"cfl": 0.45, "max_time_s": 600.0},
    )


def test_design_hash_changes_when_any_bound_moves() -> None:
    base = _design()
    same = _design()
    assert base.design_hash == same.design_hash

    moved = EnsembleDesign(
        n_members=base.n_members,
        seed=base.seed,
        resolutions=base.resolutions,
        settings_template=base.settings_template,
        critical_shear_pa=base.critical_shear_pa + 1.0,
    )
    assert moved.design_hash != base.design_hash


def test_design_hash_survives_a_round_trip_through_the_freeze(tmp_path: Path) -> None:
    design = _design()
    write_frozen(design, tmp_path, "test notes")

    payload = read_frozen_design(tmp_path)
    rebuilt = design_from_payload(payload)

    assert rebuilt.design_hash == design.design_hash
    text = (tmp_path / "ENSEMBLE_FROZEN.md").read_text(encoding="utf-8")
    assert design.design_hash in text
    assert SOLVER_VERSION in text
    assert "NOT r.avaflow" in text


def test_frozen_report_uses_no_forbidden_vocabulary(tmp_path: Path) -> None:
    write_frozen(_design(), tmp_path, "sized against the measured per-member cost")
    text = (tmp_path / "ENSEMBLE_FROZEN.md").read_text(encoding="utf-8").lower()
    for word in FORBIDDEN_VOCABULARY:
        assert word not in text, f"{word!r} appears in the frozen design report"


def test_members_are_reproducible_and_inside_their_bounds() -> None:
    design = _design(20)
    first = design.all_members()
    second = design.all_members()
    assert [m[0] for m in first] == [m[0] for m in second]
    for run_id, parameters, settings in first:
        assert run_id.startswith("m")
        assert 5.0e6 <= parameters.release_volume_m3 <= 3.0e8
        assert 0.2 <= parameters.ice_fraction <= 0.8
        assert 0.02 <= parameters.mu <= 0.30
        assert 200.0 <= parameters.xi_m_s2 <= 3000.0
        assert settings.resolution_m == 30.0


# -- runner caching ----------------------------------------------------------------------------


def test_runner_caches_by_input_hash_and_does_not_recompute(tmp_path: Path) -> None:
    from serac.models.runout.params import SolverSettings
    from serac.models.runout.runner import RunoutRunner

    terrain = _ramp_terrain(tmp_path)
    data_dir = tmp_path / "data"
    runner = RunoutRunner(tmp_path, terrain, aoi_id="test-aoi", data_dir=data_dir)
    parameters = VoellmyParameters(
        release_volume_m3=1.0e6,
        ice_fraction=0.4,
        release_elevation_band_m=(3900.0, 4000.0),
        entrainment_coefficient=0.0,
        mu=0.08,
        xi_m_s2=1500.0,
    )
    settings = SolverSettings(resolution_m=30.0, cfl=0.45, max_time_s=60.0)

    first = runner.run("m0000", parameters, settings)
    raster = first.directory / "max_depth.tif"
    digest_before = raster.read_bytes()

    second = runner.run("m0000", parameters, settings)

    assert not first.cached
    assert second.cached
    assert second.input_hash == first.input_hash
    assert raster.read_bytes() == digest_before, "a cache hit must not rewrite the artifacts"


def test_runner_ledgers_every_artifact_as_derived_simulation_output(tmp_path: Path) -> None:
    from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
    from serac.domain.manifest import DataSource, Provenance
    from serac.models.runout.params import SolverSettings
    from serac.models.runout.runner import RunoutRunner

    terrain = _ramp_terrain(tmp_path)
    data_dir = tmp_path / "data"
    runner = RunoutRunner(tmp_path, terrain, aoi_id="test-aoi", data_dir=data_dir)
    parameters = VoellmyParameters(
        release_volume_m3=1.0e6,
        ice_fraction=0.4,
        release_elevation_band_m=(3900.0, 4000.0),
        entrainment_coefficient=0.0,
        mu=0.08,
        xi_m_s2=1500.0,
    )
    outcome = runner.run("m0001", parameters, SolverSettings(resolution_m=30.0, max_time_s=30.0))

    entries = list(JsonlManifestLedger(data_dir / "manifest.jsonl").entries())
    assert entries, "the runner must write ledger rows"
    written = {Path(e.path or "").name for e in entries}
    assert {"max_depth.tif", "arrival_time.tif", "corridor.parquet", "run.json"} <= written
    for entry in entries:
        assert entry.source == DataSource.simulation_output
        assert entry.provenance == Provenance.derived
        assert entry.sha256 is not None
        assert entry.notes is not None and "NOT r.avaflow" in entry.notes

    run_json = json.loads((outcome.directory / "run.json").read_text(encoding="utf-8"))
    assert "mass_balance" in run_json
    assert "numerical_flags" in run_json
    assert any("NOT r.avaflow" in a for a in run_json["assumptions"])


# -- cascade rules v0 ---------------------------------------------------------------------------


def test_damming_index_is_a_ratio_with_a_stated_band() -> None:
    site = Constriction(
        chainage_m=20_000.0, channel_depth_m=40.0, channel_width_m=60.0, bed_elevation_m=1500.0
    )
    indicator = damming_index(site, deposit_depth_m=20.0)

    assert indicator.index == pytest.approx(0.5)
    assert indicator.index_low < indicator.index < indicator.index_high
    assert indicator.dam_height_m == pytest.approx(20.0)
    assert any("30 m DEM" in a for a in indicator.assumptions)
    assert indicator.as_dict()["label"] == DAMMING_V0_LABEL


def test_damming_probability_is_monotone_and_bounded() -> None:
    values = [index_to_probability(i) for i in (0.0, 0.5, 1.0, 2.0, 5.0)]
    assert values == sorted(values)
    assert 0.0 < values[0] < values[-1] < 1.0
    assert index_to_probability(1.0) == pytest.approx(0.5)


def test_breach_hydrograph_area_equals_the_impounded_volume() -> None:
    site = Constriction(
        chainage_m=30_000.0, channel_depth_m=50.0, channel_width_m=80.0, bed_elevation_m=1200.0
    )
    indicator = damming_index(site, deposit_depth_m=45.0)
    hydrograph = breach_hydrograph(indicator)

    area = 0.5 * hydrograph.peak_discharge_m3s * hydrograph.total_time_s
    assert area == pytest.approx(indicator.lake_volume_m3, rel=1e-9)
    assert hydrograph.discharge_at(0.0) == 0.0
    assert hydrograph.discharge_at(hydrograph.total_time_s) == 0.0
    assert hydrograph.discharge_at(hydrograph.rise_time_s) == pytest.approx(
        hydrograph.peak_discharge_m3s
    )
    assert any(
        "not routed" in a or "not a breach model" in a.lower() for a in hydrograph.assumptions
    )


def test_secondary_surge_refuses_to_travel_upstream() -> None:
    site = Constriction(
        chainage_m=40_000.0, channel_depth_m=30.0, channel_width_m=60.0, bed_elevation_m=900.0
    )
    indicator = damming_index(site, deposit_depth_m=25.0)
    hydrograph = breach_hydrograph(indicator)

    assert secondary_surge(indicator, hydrograph, to_chainage_m=30_000.0) is None
    downstream = secondary_surge(indicator, hydrograph, to_chainage_m=60_000.0)
    assert downstream is not None
    assert downstream.travel_time_s > 0.0
    assert any("not a solved flood routing" in a for a in downstream.assumptions)


def test_find_constrictions_skips_reaches_the_dem_cannot_resolve() -> None:
    chainage = np.arange(0.0, 100_000.0, 250.0)
    flat = np.full_like(chainage, 500.0)
    assert find_constrictions(chainage, flat) == []


# -- the disclaimer travels ----------------------------------------------------------------------


def test_not_ravaflow_names_the_substitute_and_the_outstanding_work() -> None:
    assert "NOT r.avaflow" in NOT_RAVAFLOW
    assert "serac-swe-voellmy" in NOT_RAVAFLOW
    assert "outstanding" in NOT_RAVAFLOW


def test_model_card_and_acquisition_record_exist_and_disclaim() -> None:
    card = Path("reports/MODEL_CARD_runout.md").read_text(encoding="utf-8")
    assert "NOT r.avaflow" in card
    assert "phase separation" in card
    assert "under 60 m wide" in card

    record = Path("infra/docker/ravaflow/README.md").read_text(encoding="utf-8")
    assert "404" in record
    assert "registration" in record.lower()
    assert "2026-09-03" in record
