from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from serac.domain import common
from serac.domain.common import (
    AttributedEstimate,
    FieldNote,
    FieldNoteReason,
    Range,
    RecordMeta,
    SourceKind,
    SourceRef,
    annotation_accepts,
    iter_field_paths,
    iter_models,
    iter_none_fields,
    iter_ranges,
    iter_source_ref_ids,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _source(**overrides: Any) -> SourceRef:
    data: dict[str, Any] = {
        "id": "test-src-1",
        "kind": SourceKind.peer_reviewed,
        "title": "Fictional",
        "url": "https://example.invalid/x",
        "accessed_utc": NOW,
        "sha256": "0" * 64,
        "content_type": "text/html",
        "licence": "CC-BY-4.0",
        "claims_supported": ["fall_height_m"],
        "peer_reviewed": True,
    }
    data.update(overrides)
    return SourceRef(**data)


# --- SourceRef ------------------------------------------------------------------------------


def test_source_ref_valid_with_doi() -> None:
    src = _source(doi="10.1234/abc.def-1", excerpt="x" * 300, year=2020)
    assert src.doi == "10.1234/abc.def-1"


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("doi", "doi:10.1234/x", "doi"),
        ("doi", "10.12/x", "doi"),
        ("id", "Bad_Id", "id"),
        ("url", "example.invalid/x", "url"),
        ("sha256", "abc", "sha256"),
        ("excerpt", "x" * 301, "excerpt"),
        ("claims_supported", [], "claims_supported"),
        ("claims_supported", ["bad path"], "claims_supported"),
        ("accessed_utc", datetime(2026, 1, 1), "accessed_utc"),
        ("year", 1700, "year"),
        ("title", "", "title"),
    ],
)
def test_source_ref_field_constraints(field: str, value: object, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _source(**{field: value})


def test_source_ref_peer_reviewed_flag_must_match_kind() -> None:
    with pytest.raises(ValidationError, match="peer_reviewed=False disagrees with kind"):
        _source(peer_reviewed=False)
    with pytest.raises(ValidationError, match="peer_reviewed=True disagrees with kind"):
        _source(kind=SourceKind.press_report, peer_reviewed=True)
    assert _source(kind=SourceKind.press_report, peer_reviewed=False).peer_reviewed is False


def test_source_ref_forbids_extra_and_is_frozen() -> None:
    with pytest.raises(ValidationError, match="extra"):
        _source(bogus=1)
    src = _source()
    with pytest.raises(ValidationError):
        src.title = "changed"  # frozen model: assignment must fail at runtime


# --- AttributedEstimate / Range -------------------------------------------------------------


def test_attributed_estimate_ordering() -> None:
    est = AttributedEstimate(low=1.0, high=1.0, unit="m3", source_ref="test-src-1")
    assert est.low == est.high
    with pytest.raises(ValidationError, match=r"low=2\.0 exceeds high=1\.0"):
        AttributedEstimate(low=2.0, high=1.0, unit="m3", source_ref="test-src-1")


def _range(**overrides: Any) -> Range:
    data: dict[str, Any] = {"low": 1.0, "high": 2.0, "unit": "m", "source_refs": ["test-src-1"]}
    data.update(overrides)
    return Range(**data)


def test_range_valid_with_best() -> None:
    rng = _range(best=1.5)
    assert rng.best == 1.5
    assert rng.disputed is False
    assert rng.estimates == []


def test_range_low_must_not_exceed_high() -> None:
    with pytest.raises(ValidationError, match=r"low=3\.0 exceeds high=2\.0"):
        _range(low=3.0)


def test_range_best_within_bounds() -> None:
    with pytest.raises(ValidationError, match=r"best=5.0 outside \[1.0, 2.0\]"):
        _range(best=5.0)
    assert _range(best=1.0).best == 1.0
    assert _range(best=2.0).best == 2.0


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
def test_range_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValidationError, match="finite"):
        _range(low=bad, high=bad)
    with pytest.raises(ValidationError, match="finite"):
        _range(best=bad)


def test_range_requires_at_least_one_source_and_no_duplicates() -> None:
    with pytest.raises(ValidationError, match="source_refs"):
        _range(source_refs=[])
    with pytest.raises(ValidationError, match="duplicates"):
        _range(source_refs=["test-src-1", "test-src-1"])


def test_range_estimate_units_must_match() -> None:
    est = AttributedEstimate(low=1.0, high=2.0, unit="km", source_ref="test-src-1")
    with pytest.raises(ValidationError, match=r"estimates\[0\].unit='km' differs from unit='m'"):
        _range(estimates=[est])


def _estimates(n: int = 2) -> list[AttributedEstimate]:
    return [
        AttributedEstimate(low=float(i), high=float(i + 1), unit="m", source_ref=f"test-src-{i}")
        for i in range(1, n + 1)
    ]


def test_disputed_range_rules() -> None:
    with pytest.raises(ValidationError, match="disputed range cannot carry best"):
        _range(disputed=True, best=1.5, estimates=_estimates(), notes="n")
    with pytest.raises(ValidationError, match="at least 2 attributed estimates"):
        _range(disputed=True, estimates=_estimates(1), notes="n")
    with pytest.raises(ValidationError, match="disputed range needs notes"):
        _range(disputed=True, estimates=_estimates())
    ok = _range(low=1.0, high=3.0, disputed=True, estimates=_estimates(), notes="two figures")
    assert ok.disputed and ok.best is None and len(ok.estimates) == 2


def test_range_reports_all_problems_at_once() -> None:
    with pytest.raises(ValidationError) as exc:
        _range(low=3.0, best=9.0)
    assert "low=3.0 exceeds high=2.0" in str(exc.value)
    assert "best=9.0 outside" in str(exc.value)


# --- FieldNote / RecordMeta -----------------------------------------------------------------


def test_field_note_requires_substantive_notes() -> None:
    with pytest.raises(ValidationError, match="notes"):
        FieldNote(reason=FieldNoteReason.not_public, notes="too short")
    note = FieldNote(
        reason=FieldNoteReason.no_peer_reviewed_estimate,
        public_estimates=_estimates(),
        notes="fictional: only attributed public figures exist",
    )
    assert len(note.public_estimates) == 2


def test_record_meta_review_pair() -> None:
    with pytest.raises(ValidationError, match="must be set together"):
        RecordMeta(created_utc=NOW, created_by="t", reviewed_by="r")
    with pytest.raises(ValidationError, match="must be set together"):
        RecordMeta(created_utc=NOW, created_by="t", review_utc=NOW)
    with pytest.raises(ValidationError, match="review_utc precedes created_utc"):
        RecordMeta(
            created_utc=NOW,
            created_by="t",
            reviewed_by="r",
            review_utc=datetime(2020, 1, 1, tzinfo=UTC),
        )
    ok = RecordMeta(created_utc=NOW, created_by="t", reviewed_by="r", review_utc=NOW)
    assert ok.reviewed_by == "r"


# --- tree walkers ---------------------------------------------------------------------------


class Leaf(BaseModel):
    value: Range | None = None
    label: str | None = None


class Tree(BaseModel):
    top: Range
    leaves: list[Leaf]
    by_key: dict[str, Leaf]
    nested: Leaf
    plain: int = 0


def _tree() -> Tree:
    return Tree(
        top=_range(),
        leaves=[Leaf(value=_range(low=0.0, high=0.5)), Leaf()],
        by_key={"k-1": Leaf(value=_range(estimates=_estimates()))},
        nested=Leaf(label="x"),
    )


def test_iter_ranges_walks_lists_dicts_and_nested_models() -> None:
    paths = [path for path, _ in iter_ranges(_tree())]
    assert paths == ["top", "leaves[0].value", "by_key.k-1.value"]


def test_iter_ranges_on_a_bare_range_yields_root() -> None:
    assert [p for p, _ in iter_ranges(_range())] == [""]


def test_iter_models_yields_root_first() -> None:
    paths = [path for path, _ in iter_models(_tree())]
    assert paths[0] == ""
    assert "leaves[1]" in paths
    assert "by_key.k-1.value.estimates[1]" in paths


def test_iter_none_fields_reports_path_and_annotation() -> None:
    found = dict(iter_none_fields(_tree()))
    assert set(found) == {
        "top.best",
        "top.notes",
        "leaves[0].value.best",
        "leaves[0].value.notes",
        "leaves[0].label",
        "leaves[1].value",
        "leaves[1].label",
        "by_key.k-1.value.best",
        "by_key.k-1.value.notes",
        "by_key.k-1.value.estimates[0].qualifier",
        "by_key.k-1.value.estimates[1].qualifier",
        "by_key.k-1.label",
        "nested.value",
    }
    assert annotation_accepts(found["leaves[1].value"], Range)
    assert not annotation_accepts(found["leaves[1].label"], Range)


def test_annotation_accepts_variants() -> None:
    from typing import Annotated, Optional  # Optional form tested on purpose

    assert annotation_accepts(Range, Range)
    assert annotation_accepts(Range | None, Range)
    assert annotation_accepts(Optional[Range], Range)  # noqa: UP045
    assert annotation_accepts(Annotated[Range | None, "meta"], Range)
    assert not annotation_accepts(list[Range], Range)
    assert not annotation_accepts(str | None, Range)


def test_iter_field_paths_and_source_ref_ids() -> None:
    paths = set(iter_field_paths(_tree()))
    assert {"top", "top.low", "leaves[0].value.source_refs", "by_key.k-1.value"} <= paths
    refs = list(iter_source_ref_ids(_tree()))
    assert ("top.source_refs[0]", "test-src-1") in refs
    assert ("by_key.k-1.value.estimates[0].source_ref", "test-src-1") in refs
    assert ("by_key.k-1.value.estimates[1].source_ref", "test-src-2") in refs


def test_contracts_table_registered() -> None:
    assert {"source-ref": SourceRef} == common.CONTRACTS
    assert SourceKind.press_report not in common.BEST_QUALIFYING_KINDS
    assert SourceKind.agency_official not in common.SINGLE_FORCE_QUALIFYING_KINDS
