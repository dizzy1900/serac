"""The Langtang comparison may only compare against figures the event record holds.

The regression these tests exist for: M4 held four transect timings as a literal in its own
source (`PUBLIC_TIMINGS_MIN = {7.5, 13.5, 45.0, 30.0}`) and the write-up called all four
"press-attributed figures … the corresponding fields carry `best: null`". Two of the four are
not in `data/events/langtang-lhende-2026.json` at all — the record refuses them, in as many
words, because no retrievable source states them — one is a **stage-rise** window and not an
arrival time, and the fourth was a midpoint of a span the record does not carry either.

So the tests here are about provenance, not arithmetic:

* the targets are derived from the record and nowhere else, and the refused figures are absent;
* the module carries no place to type a transect timing;
* the committed artifact agrees with the committed record, and the committed write-up is
  exactly what the artifact renders to;
* and `verify_targets_against_record` — the gate `validate-runout` runs — fails on each way the
  old shape could come back.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from serac.models.runout import langtang, observed
from serac.models.runout.langtang import SANITY_FILENAME, SANITY_JSON, render
from serac.models.runout.observed import (
    ObservedTimingError,
    TransectTarget,
    comparison_targets,
    load_transect_targets,
    targets_from_record,
    verify_targets_against_record,
)

REFUSED_PUBLIC_FIGURES = (7.5, 45.0, 30.0, 13.5)
"""The four numbers the module used to hold. None of them may reappear as a target: 7.5 and 45
circulate without attribution, 30 is the window of a +9 m stage rise, 13.5 was a midpoint."""


@pytest.fixture(scope="module")
def targets(repo_root: Path) -> tuple[TransectTarget, ...]:
    return load_transect_targets(repo_root)


@pytest.fixture(scope="module")
def payload(repo_root: Path) -> dict[str, Any]:
    return json.loads((repo_root / "reports" / "runout" / SANITY_JSON).read_text(encoding="utf-8"))


# -- what the record holds -------------------------------------------------------------------


def test_only_the_sourced_arrival_becomes_a_comparison_target(
    targets: tuple[TransectTarget, ...],
) -> None:
    compared = comparison_targets(targets)
    assert set(compared) == {"syabrubesi"}, (
        "syabrubesi is the only transect whose arrival_time_min is a Range in the record"
    )
    syabrubesi = compared["syabrubesi"]
    assert (syabrubesi.arrival_low_min, syabrubesi.arrival_high_min) == (13.0, 13.0)
    assert syabrubesi.arrival_best_min is None, "press-only, so the record asserts no best"
    assert syabrubesi.arrival_source_refs == ("kp-2026-09-02-alert",)


def test_the_refused_public_figures_are_not_targets(
    targets: tuple[TransectTarget, ...],
) -> None:
    for target in targets:
        for figure in REFUSED_PUBLIC_FIGURES:
            assert target.arrival_low_min != figure and target.arrival_high_min != figure, (
                f"{target.transect_id} carries {figure}, which the event record does not"
            )


def test_every_untargeted_transect_carries_the_records_own_reason(
    targets: tuple[TransectTarget, ...],
) -> None:
    absent = {t.transect_id: t for t in targets if not t.is_comparison_target}
    assert set(absent) == {"rasuwagadhi-gyirong", "betrawati", "galchhi"}
    for target in absent.values():
        assert target.absent_reason and "null" in target.absent_reason
        assert target.arrival_source_refs == ()
    assert "without attribution" in (absent["rasuwagadhi-gyirong"].absent_reason or "")
    assert "without attribution" in (absent["betrawati"].absent_reason or "")
    # Galchhi's 30 minutes is the window of a stage rise, so the record's stage figure must be
    # reported as what it is and never as an arrival.
    assert absent["galchhi"].other_observations, "the +9 m stage rise must still be reported"
    assert "stage_rise_m" in absent["galchhi"].other_observations[0]


def test_every_target_carries_the_records_sources(targets: tuple[TransectTarget, ...]) -> None:
    for target in comparison_targets(targets).values():
        assert target.arrival_source_refs, "a comparison target with no source is unattributed"


def test_a_silent_null_arrival_is_refused() -> None:
    """A null with no explanation cannot be reported as a reason, so the loader raises."""
    from serac.domain.events import TransectObservation

    observation = TransectObservation(
        transect_id="nowhere", arrival_time_min=None, source_refs=["some-source"]
    )
    with pytest.raises(ObservedTimingError, match="no description"):
        observed._target_from_observation(observation)


def test_a_duplicate_transect_is_refused(repo_root: Path) -> None:
    from serac.domain.events import MassMovementEvent

    event = MassMovementEvent.model_validate_json(
        (repo_root / "data" / "events" / "langtang-lhende-2026.json").read_bytes()
    )
    doubled = event.model_copy(
        update={"transect_observations": list(event.transect_observations) * 2}
    )
    with pytest.raises(ObservedTimingError, match="appears twice"):
        targets_from_record(doubled)


def test_a_missing_record_raises_rather_than_defaulting(tmp_path: Path) -> None:
    with pytest.raises(ObservedTimingError, match="will not supply its own"):
        load_transect_targets(tmp_path)


# -- the mismatch is against the recorded interval, not a midpoint ---------------------------


def test_signed_gap_is_zero_inside_the_recorded_interval() -> None:
    target = TransectTarget(
        transect_id="t",
        arrival_low_min=13.0,
        arrival_high_min=14.0,
        arrival_best_min=None,
        arrival_source_refs=("s",),
        arrival_notes=None,
        absent_reason=None,
        other_observations=(),
    )
    assert target.signed_gap_min(13.5) == 0.0
    assert target.signed_gap_min(20.0) == pytest.approx(6.0)
    assert target.signed_gap_min(10.0) == pytest.approx(-3.0)
    assert target.label == "13-14"


def test_no_mismatch_can_be_computed_where_there_is_no_recorded_arrival(
    targets: tuple[TransectTarget, ...],
) -> None:
    betrawati = next(t for t in targets if t.transect_id == "betrawati")
    with pytest.raises(ObservedTimingError, match="holds no arrival time"):
        betrawati.signed_gap_min(45.0)


# -- the module has nowhere to type a timing --------------------------------------------------


def test_the_module_holds_no_transect_timing_literal(
    repo_root: Path, targets: tuple[TransectTarget, ...]
) -> None:
    """A literal cannot be checked against the record, so there must not be one."""
    assert not hasattr(langtang, "PUBLIC_TIMINGS_MIN")
    source = (repo_root / "src" / "serac" / "models" / "runout" / "langtang.py").read_text(
        encoding="utf-8"
    )
    for target in targets:
        pattern = rf'["\']{re.escape(target.transect_id)}["\']\s*:\s*-?\d'
        assert not re.search(pattern, source), (
            f"{target.transect_id} is mapped to a number in langtang.py; comparison targets "
            "come from the event record"
        )


# -- the committed artifacts ------------------------------------------------------------------


def test_the_committed_artifact_matches_the_committed_record(
    repo_root: Path, payload: dict[str, Any]
) -> None:
    assert verify_targets_against_record(payload, repo_root) == []
    assert payload["n_comparison_targets"] == 1
    assert payload["observation_source"] == "data/events/langtang-lhende-2026.json"
    assert payload["member_arrivals_source"]


def test_the_committed_write_up_is_exactly_what_the_artifact_renders(
    repo_root: Path, payload: dict[str, Any]
) -> None:
    committed = (repo_root / "reports" / "runout" / SANITY_FILENAME).read_text(encoding="utf-8")
    assert render(payload) == committed, (
        "the write-up must be a pure function of the gated payload, so prose cannot come to "
        "claim what the record does not hold"
    )


def test_the_write_up_never_presents_a_refused_figure_as_a_target(
    repo_root: Path, payload: dict[str, Any]
) -> None:
    text = (repo_root / "reports" / "runout" / SANITY_FILENAME).read_text(encoding="utf-8")
    held = [
        line
        for line in text.split("### Transects with no recorded arrival time")[0].splitlines()
        if line.startswith("| `")
    ]
    assert held, "the table of recorded arrivals must not be empty"
    recorded_columns = {cell.strip() for line in held for cell in line.split("|")[2:4]}
    for figure in REFUSED_PUBLIC_FIGURES:
        assert f"{figure:g}" not in recorded_columns, (
            f"{figure:g} is presented as a figure the record holds"
        )
    assert "press-attributed" not in text.lower(), (
        "only one of these figures is attributed at all; the write-up must not describe the set "
        "as press-attributed"
    )
    mismatch_section = text.split("## Mismatch against the recorded arrivals")[1].split("##")[0]
    for transect_id in ("rasuwagadhi-gyirong", "betrawati", "galchhi"):
        assert transect_id not in mismatch_section, (
            f"{transect_id} has no recorded arrival, so it cannot appear in a mismatch table"
        )


# -- the gate ---------------------------------------------------------------------------------


def test_the_gate_catches_a_target_the_record_refuses(
    repo_root: Path, payload: dict[str, Any]
) -> None:
    """The exact shape of the original defect: the ~7.5 min figure re-entered as a target."""
    doctored = json.loads(json.dumps(payload))
    doctored["transect_targets"].append(
        {
            "transect_id": "rasuwagadhi-gyirong",
            "is_comparison_target": True,
            "arrival_low_min": 7.5,
            "arrival_high_min": 7.5,
            "arrival_best_min": None,
            "arrival_unit": "min",
            "arrival_source_refs": ["kp-2026-08-27-what-happened"],
            "arrival_notes": None,
            "absent_reason": None,
            "other_observations": [],
        }
    )
    problems = verify_targets_against_record(doctored, repo_root)
    assert any("rasuwagadhi-gyirong" in p for p in problems), problems


def test_the_gate_catches_a_per_transect_arrival_the_record_does_not_hold(
    repo_root: Path, payload: dict[str, Any]
) -> None:
    doctored = json.loads(json.dumps(payload))
    doctored["per_transect"]["betrawati"]["recorded_arrival_min"] = {
        "low": 45.0,
        "high": 45.0,
        "best": None,
        "unit": "min",
        "source_refs": [],
    }
    problems = verify_targets_against_record(doctored, repo_root)
    assert any("betrawati" in p and "holds none" in p for p in problems), problems


def test_the_gate_catches_a_widened_interval(repo_root: Path, payload: dict[str, Any]) -> None:
    doctored = json.loads(json.dumps(payload))
    entry = next(t for t in doctored["transect_targets"] if t["transect_id"] == "syabrubesi")
    entry["arrival_high_min"] = 14.0  # the "13-14 min" span the record does not carry
    problems = verify_targets_against_record(doctored, repo_root)
    assert any("syabrubesi" in p for p in problems), problems


def test_the_gate_catches_an_invented_source(repo_root: Path, payload: dict[str, Any]) -> None:
    doctored = json.loads(json.dumps(payload))
    entry = next(t for t in doctored["transect_targets"] if t["transect_id"] == "syabrubesi")
    entry["arrival_source_refs"] = ["some-paper-2027"]
    problems = verify_targets_against_record(doctored, repo_root)
    assert any("cites" in p for p in problems), problems


def test_the_gate_catches_an_artifact_with_no_provenance_block(
    repo_root: Path, payload: dict[str, Any]
) -> None:
    """An artifact in the old shape has no way to show where its targets came from."""
    doctored = json.loads(json.dumps(payload))
    doctored.pop("transect_targets")
    problems = verify_targets_against_record(doctored, repo_root)
    assert any("transect_targets" in p for p in problems), problems


def test_the_gate_catches_a_dropped_transect(repo_root: Path, payload: dict[str, Any]) -> None:
    doctored = json.loads(json.dumps(payload))
    doctored["transect_targets"] = [
        t for t in doctored["transect_targets"] if t["transect_id"] != "galchhi"
    ]
    problems = verify_targets_against_record(doctored, repo_root)
    assert any("galchhi" in p for p in problems), problems


def test_the_gate_reports_an_unreadable_record_rather_than_passing(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    problems = verify_targets_against_record(payload, tmp_path)
    assert problems and "could not be read" in problems[0]


# -- the writer ---------------------------------------------------------------------------------


def test_the_recorded_arrivals_round_trip_out_of_the_artifact(repo_root: Path) -> None:
    members, transect_ids = langtang.member_arrivals_from_artifact(
        repo_root / "reports" / "runout" / SANITY_JSON
    )
    assert len(members) == 230
    assert set(transect_ids) == {"rasuwagadhi-gyirong", "syabrubesi", "betrawati", "galchhi"}
    assert all(m.run_id and m.parameters for m in members)


def test_a_member_without_modelled_arrivals_is_refused(tmp_path: Path) -> None:
    path = tmp_path / SANITY_JSON
    path.write_text(json.dumps({"all_members": [{"run_id": "m0000"}]}), encoding="utf-8")
    with pytest.raises(ObservedTimingError, match="modelled_arrival_min"):
        langtang.member_arrivals_from_artifact(path)


def test_the_committed_comparison_reproduces_from_the_record_and_the_artifact(
    repo_root: Path, tmp_path: Path
) -> None:
    """Rebuild the write-up from the committed record and the committed modelled arrivals.

    Nothing here recomputes a modelled number — the per-member rasters are DVC-tracked and a
    clone need not have them — so what this asserts is that the committed comparison is exactly
    what the committed *inputs* produce.
    """
    reports = tmp_path / "reports" / "runout"
    reports.mkdir(parents=True)
    source = repo_root / "reports" / "runout"
    for name in (
        "ENSEMBLE_FROZEN.md",
        "ensemble_design.json",
        "verification.json",
        "terrain.json",
        SANITY_JSON,
    ):
        shutil.copy(source / name, reports / name)

    written = langtang.write_sanity_check(repo_root, reports_dir=reports, from_artifact=True)

    assert written.read_text(encoding="utf-8") == (source / SANITY_FILENAME).read_text(
        encoding="utf-8"
    )
    rebuilt = json.loads((reports / SANITY_JSON).read_text(encoding="utf-8"))
    committed = json.loads((source / SANITY_JSON).read_text(encoding="utf-8"))
    for doc in (rebuilt, committed):
        doc.pop("generated_utc")
    assert rebuilt == committed
