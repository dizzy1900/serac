"""Rebuild every AOI directory from the committed Overpass fixtures (or refresh them online).

    uv run python scripts/build_aois.py                 # offline: replay data/fixtures/osm/
    uv run python scripts/build_aois.py --online        # re-query Overpass, refuse to overwrite
                                                        # a fixture whose bytes differ (--force)
    uv run python scripts/build_aois.py --record-ledger # append missing fixture rows to
                                                        # data/manifest.jsonl and FIXTURES.md

Nothing is synthesised: the offline build is a pure function of the fixture bytes and the
specs in `serac.pipelines.aoi_specs`. Ledger rows are appended only when no row with the same
path and sha256 exists; the ledger is never rewritten.
"""

# ruff: noqa: T201  (a script; progress goes to stdout)
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

from serac import __version__
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger, sha256_of_file
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.pipelines.aoi_build import (
    ADAPTER_NAME,
    OSM_ATTRIBUTION,
    OSM_LICENCE,
    OSM_LICENCE_URL,
    OVERPASS_ENDPOINT,
    FixtureOverpassClient,
    HttpxOverpassClient,
    OverpassClient,
    build_aoi,
    write_aoi_dir,
)
from serac.pipelines.aoi_specs import AOI_SPECS

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = REPO_ROOT / "data" / "manifest.jsonl"
FIXTURES_MD = REPO_ROOT / "data" / "fixtures" / "FIXTURES.md"


def _bbox_of_query(query: str) -> tuple[float, float, float, float] | None:
    """The first `(s,w,n,e)` bbox in the query, as (w, s, e, n)."""
    import re

    m = re.search(r"\((-?\d+\.\d+),(-?\d+\.\d+),(-?\d+\.\d+),(-?\d+\.\d+)\)", query)
    if not m:
        return None
    s, w, n, e = (float(x) for x in m.groups())
    return (w, s, e, n)


def record_fixture(ledger: JsonlManifestLedger, aoi_id: str, fixture: Path) -> bool:
    spec = AOI_SPECS[aoi_id]
    sha = sha256_of_file(fixture)
    rel = fixture.relative_to(REPO_ROOT).as_posix()
    if any(e.path == rel and e.sha256 == sha for e in ledger.entries()):
        return False
    ledger.append(
        ManifestEntry(
            source=DataSource.osm_overpass,
            product_id=f"overpass/{aoi_id}/{fixture.stem.rsplit('_', 1)[-1]}",
            aoi_id=aoi_id,
            path=rel,
            url=OVERPASS_ENDPOINT,
            params={"data": " ".join(spec.overpass_query.split()), "out": "json geom"},
            sha256=sha,
            size_bytes=fixture.stat().st_size,
            retrieved_at=spec.fixture_retrieved_utc,
            licence=OSM_LICENCE,
            licence_source_url=OSM_LICENCE_URL,
            provenance=Provenance.real,
            status=ManifestStatus.fetched,
            bbox_4326=_bbox_of_query(spec.overpass_query),
            adapter=ADAPTER_NAME,
            adapter_version=__version__,
            notes=(
                f"Raw Overpass API response (application/json, out geom) committed verbatim. "
                f"{OSM_ATTRIBUTION}; data licensed under {OSM_LICENCE}."
            ),
        )
    )
    text = FIXTURES_MD.read_text(encoding="utf-8")
    if f"`{rel}`" not in text:
        row = (
            f"| `{rel}` | {OVERPASS_ENDPOINT} (POST, query in ledger params) | "
            f"{spec.fixture_retrieved_utc.isoformat()} | `{sha}` | {fixture.stat().st_size} | "
            f"{OSM_LICENCE} ({OSM_ATTRIBUTION}) |\n"
        )
        FIXTURES_MD.write_text(text.rstrip("\n") + "\n" + row, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--aoi", action="append", help="AOI id (repeatable); default all")
    parser.add_argument("--online", action="store_true", help="query Overpass instead of fixtures")
    parser.add_argument("--force", action="store_true", help="overwrite a differing fixture")
    parser.add_argument("--record-ledger", action="store_true", help="append fixture ledger rows")
    args = parser.parse_args(argv)

    ids = args.aoi or sorted(AOI_SPECS)
    ledger = JsonlManifestLedger(LEDGER_PATH)
    rc = 0
    for aoi_id in ids:
        spec = AOI_SPECS[aoi_id]
        fixture = REPO_ROOT / spec.fixture_path
        client: OverpassClient
        accessed: datetime
        if args.online:
            client = HttpxOverpassClient()
            accessed = datetime.now(tz=UTC)
        else:
            if not fixture.exists():
                print(f"{aoi_id}: fixture missing at {fixture}; run with --online")
                rc = 2
                continue
            client = FixtureOverpassClient(fixture)
            accessed = spec.fixture_retrieved_utc
        built = build_aoi(spec, client, accessed_utc=accessed)
        if args.online:
            new_sha = hashlib.sha256(built.raw_response).hexdigest()
            if fixture.exists() and sha256_of_file(fixture) != new_sha and not args.force:
                print(
                    f"{aoi_id}: Overpass response differs from {fixture.name}; "
                    "not overwriting (pass --force to replace)"
                )
                rc = 2
                continue
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_bytes(built.raw_response)
            print(f"{aoi_id}: wrote {fixture} ({len(built.raw_response)} bytes, sha256 {new_sha})")
            print("  update fixture_retrieved_utc in aoi_specs.py and re-run --record-ledger")
        for path in write_aoi_dir(built, REPO_ROOT / "data" / "aoi" / aoi_id):
            print(f"  {path.relative_to(REPO_ROOT)}")
        r = built.report
        print(
            f"{aoi_id}: centreline {r.centreline_length_km:.1f} km "
            f"({'clipped' if r.clipped else 'full'} of {r.full_path_length_km:.1f} km); "
            f"start offset {r.start_offset_m:.0f} m; transects "
            + ", ".join(f"{k}={v:.1f} km" for k, v in r.transect_chainage_km.items())
        )
        if args.record_ledger and fixture.exists():
            added = record_fixture(ledger, aoi_id, fixture)
            print(f"  ledger row {'added' if added else 'already present'} for {fixture.name}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
