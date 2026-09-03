"""NISAR product-level constraints, encoded as dated constants (founding brief, 2026-09-03).

What the brief states and what the ASF probe of 2026-09-03
(`data/fixtures/asf/nisar_probe_2026-09-03.json`, 159 science granules over Lhende) confirms:

* **BETA** products cover acquisitions Oct 2025 - Jan 2026 (probe: 2025-11-25 .. 2026-01-15,
  72 granules, crid `X05009`/`X05010`) and are **not inter-comparable** with later products.
* **PROVISIONAL** (calibrated) products exist only for acquisitions from 17 Jun 2026, released
  20 Jul 2026 (probe: 2026-06-20 .. 2026-08-31, 87 granules, crid `P05023`).
* Permanent **instrument gap** 27 Jul - 10 Aug 2026 (probe: no acquisition 2026-07-27 ..
  2026-08-10).
* The level discriminator is the CMR **`collectionName`** (`NISAR_L<n>_<LEVEL>_BETA_V1` vs
  `NISAR_L<n>_<LEVEL>_PROVISIONAL_V1`). `productionConfiguration` is `"PR"` on every
  science granule of both levels and is **not** a discriminator; the `crid` prefix (`X` for
  BETA, `P` for PROVISIONAL) is used only as a consistency check.
* Raw listings are dominated by ancillary files (`ECMWF_SMST`, `RRSD`, SCLKSCET, orbit
  files); only the science levels in `SCIENCE_LEVELS` are ever considered.

Anything that does not match the rule is `NisarLevel.unknown` and is always refused.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from enum import StrEnum

from serac.errors import IngestRefusedError

CONSTRAINTS_AS_OF = date(2026, 9, 3)

BETA_ACQUISITION_WINDOW: tuple[date, date] = (date(2025, 10, 1), date(2026, 1, 31))
"""Brief: BETA products Oct 2025 - Jan 2026, not inter-comparable with PROVISIONAL."""
PROVISIONAL_ACQUISITIONS_FROM = date(2026, 6, 17)
"""Brief: calibrated PROVISIONAL products exist only for acquisitions from this date."""
PROVISIONAL_RELEASED_ON = date(2026, 7, 20)
INSTRUMENT_GAP: tuple[date, date] = (date(2026, 7, 27), date(2026, 8, 10))
"""Brief: permanent instrument gap (inclusive)."""

SCIENCE_LEVELS: frozenset[str] = frozenset(
    {"RSLC", "GSLC", "GCOV", "RIFG", "RUNW", "GUNW", "ROFF", "GOFF", "SME2"}
)
"""`processingLevel` values that are science products; everything else is ancillary."""
ANCILLARY_HINTS: tuple[str, ...] = ("ECMWF", "SMST", "RRSD", "SCLKSCET", "ORBIT", "POEORB")

_COLLECTION_RE = re.compile(
    r"^NISAR_L(?P<tier>\d)_(?P<level>[A-Z0-9]+)_(?P<maturity>BETA|PROVISIONAL)_V(?P<version>\d+)$"
)
_CRID_PREFIX: dict[str, str] = {"X": "beta", "P": "provisional"}


class NisarLevel(StrEnum):
    beta = "beta"
    provisional = "provisional"
    unknown = "unknown"


class MixedProductLevelError(IngestRefusedError):
    """BETA and PROVISIONAL granules in one request without an explicit `--level`."""


def classify_collection(collection_name: str | None, crid: str | None = None) -> NisarLevel:
    """Level from `collectionName`; `crid`, when given, must agree or the result is unknown."""
    if not collection_name:
        return NisarLevel.unknown
    m = _COLLECTION_RE.match(collection_name.strip())
    if m is None or m["level"] not in SCIENCE_LEVELS:
        return NisarLevel.unknown
    level = NisarLevel(m["maturity"].lower())
    if crid:
        expected = _CRID_PREFIX.get(crid.strip()[:1].upper())
        if expected is not None and expected != level.value:
            return NisarLevel.unknown
    return level


def is_science_product(processing_level: str | None, file_id: str | None = None) -> bool:
    """True for granules whose `processingLevel` is a science level (ancillary filtered out)."""
    if processing_level is None or processing_level.upper() not in SCIENCE_LEVELS:
        return False
    return not (file_id and any(h in file_id.upper() for h in ANCILLARY_HINTS))


def in_instrument_gap(when: datetime | date) -> bool:
    d = when.date() if isinstance(when, datetime) else when
    return INSTRUMENT_GAP[0] <= d <= INSTRUMENT_GAP[1]


def overlaps_instrument_gap(start: datetime | None, end: datetime | None) -> bool:
    """True when [start, end] intersects the instrument gap (unbounded ends count as open)."""
    s = start.astimezone(UTC).date() if start else date.min
    e = end.astimezone(UTC).date() if end else date.max
    return s <= INSTRUMENT_GAP[1] and e >= INSTRUMENT_GAP[0]


def expected_level_for_acquisition(when: datetime | date) -> NisarLevel:
    """What the brief's windows say an acquisition date should carry (a plausibility check)."""
    d = when.date() if isinstance(when, datetime) else when
    if BETA_ACQUISITION_WINDOW[0] <= d <= BETA_ACQUISITION_WINDOW[1]:
        return NisarLevel.beta
    if d >= PROVISIONAL_ACQUISITIONS_FROM:
        return NisarLevel.provisional
    return NisarLevel.unknown


def gap_warning() -> str:
    return (
        f"NISAR instrument gap {INSTRUMENT_GAP[0]} .. {INSTRUMENT_GAP[1]} (permanent; brief as of "
        f"{CONSTRAINTS_AS_OF}): no acquisitions exist inside it"
    )


def beta_warning() -> str:
    return (
        f"BETA products (acquisitions {BETA_ACQUISITION_WINDOW[0]} .. {BETA_ACQUISITION_WINDOW[1]})"
        " are not inter-comparable with PROVISIONAL products; never mix them in one series"
    )
