"""The two `SourceRef` models must not drift apart on the fields they share.

serac has two: `domain/common.py` for the event library, and `models/lfh/references.py` for
the force-history references. That duplication is not itself a defect, because the LFH copy
carries extra resolution provenance the event library does not need. It became a defect once
a review fix was applied to one copy and silently missed the other: the reference builder
then raised on every run, and the wrong citation metadata stayed live in the committed data
while the script looked fixed.

This test makes that failure mode loud. It does not force the models to be identical; it
pins the fields whose meaning must stay the same in both, so a change to one that is not
mirrored in the other fails here rather than in a data file nobody re-reads.

Consolidating the two is tracked in RELEASE_STATUS.md's known gaps.
"""

from __future__ import annotations

from serac.domain.common import SourceRef as DomainSourceRef
from serac.models.lfh.references import SourceRef as LfhSourceRef

# Fields that carry the same meaning in both copies. `url` and `sha256` are the load-bearing
# pair: `url` must be the bytes that produced `sha256`, and anything else goes in
# `related_url`. That invariant is what the review found broken.
SHARED_FIELDS = frozenset(
    {
        "id",
        "kind",
        "title",
        "url",
        "related_url",
        "doi",
        "authors",
        "year",
        "accessed_utc",
        "content_type",
        "licence",
        "sha256",
    }
)


def test_both_copies_carry_every_shared_field() -> None:
    domain_fields = set(DomainSourceRef.model_fields)
    lfh_fields = set(LfhSourceRef.model_fields)
    assert domain_fields >= SHARED_FIELDS, SHARED_FIELDS - domain_fields
    assert lfh_fields >= SHARED_FIELDS, SHARED_FIELDS - lfh_fields


def test_neither_copy_accepts_extras() -> None:
    # Both forbid extras, which is why adding a field to one and not the other raises at
    # construction rather than silently dropping the value.
    assert DomainSourceRef.model_config.get("extra") == "forbid"
    assert LfhSourceRef.model_config.get("extra") == "forbid"


def test_url_and_related_url_are_documented_as_distinct() -> None:
    # The distinction is the whole point: a reader who fetches `url` and hashes it must get
    # `sha256`; a landing page that is not those bytes belongs in `related_url`.
    for model in (DomainSourceRef, LfhSourceRef):
        related = model.model_fields["related_url"]
        assert related.default is None
        assert (related.description or "").strip(), f"{model.__module__} must document it"
