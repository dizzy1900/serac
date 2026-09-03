"""Fetch the real seismic, catalogue and schema fixtures and record their provenance.

Run from the repository root with the network available:

    uv run python scripts/fetch_seismic_fixtures.py [--only seismic|comcat|cap] [--force]

What it does, per fixture:

* downloads the bytes exactly as the service returned them (no re-encoding);
* refuses to overwrite an existing file whose SHA-256 differs (pass `--force` to replace);
* writes `manifest.json` (`FixtureManifest`) next to waveform fixtures and
  `contracts/vendor/cap/MANIFEST.json` next to the vendored schemas;
* appends one `ManifestEntry` per file to `data/manifest.jsonl` (skipped when an identical
  path+sha256 row already exists);
* appends a row per file to `data/fixtures/FIXTURES.md`.

Re-runs are safe: identical bytes are reported as unchanged; a differing download is refused
(the existing file and its ledger row are kept, the run exits 2). StationXML is compared with
its `<Created>` timestamp masked because every response carries a fresh one; ComCat responses
embed a `generated` timestamp, so refreshing them is always an explicit `--force`.

Nothing here is ever synthesised: if a service returns no data the manifest says
`not_fetched`/`partial` and lists what is missing. Licence fields are `null` when the data
centre page read at fetch time carries no licence statement; the page URL is recorded instead.
"""

# ruff: noqa: T201  (a script; progress goes to stdout)
from __future__ import annotations

import argparse
import json
import re
import sys
import warnings
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from serac import __version__
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger, sha256_of_file
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.domain.replay import FixtureFile, FixtureManifest, FixtureRequest, TimeWindow
from serac.domain.seismic import Sncl

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "data" / "fixtures"
SEISMIC_DIR = FIXTURES_DIR / "seismic"
COMCAT_DIR = FIXTURES_DIR / "usgs_comcat"
CAP_DIR = REPO_ROOT / "contracts" / "vendor" / "cap"
LEDGER_PATH = REPO_ROOT / "data" / "manifest.jsonl"
FIXTURES_MD = FIXTURES_DIR / "FIXTURES.md"
ADAPTER = "fixture-fetch"
USER_AGENT = f"serac-fixture-fetch/{__version__} (+https://github.com/dizzy1900/serac)"

# --- what to fetch --------------------------------------------------------------------------


@dataclass(frozen=True)
class EventSpec:
    event_id: str
    start: datetime
    end: datetime
    sncls: tuple[str, ...]


EVENTS: tuple[EventSpec, ...] = (
    EventSpec(
        "chamoli-2021",
        datetime(2021, 2, 7, 4, 49, tzinfo=UTC),
        datetime(2021, 2, 7, 4, 57, tzinfo=UTC),
        ("NK.KKN..BHZ", "IC.LSA.00.BHZ"),
    ),
    EventSpec(
        "langtang-2026",
        datetime(2026, 8, 26, 2, 50, tzinfo=UTC),
        datetime(2026, 8, 26, 2, 58, tzinfo=UTC),
        ("NK.KKN..BHZ", "IO.EVN..BHZ"),
    ),
)

# FDSN client aliases tried in order; ObsPy resolves them to base URLs that we record.
FDSN_CLIENTS: tuple[str, ...] = ("EARTHSCOPE", "GEOFON")


@dataclass(frozen=True)
class LicenceNote:
    licence: str | None
    source_url: str
    statement: str


# Licence statements as read on 2026-09-03. `licence` stays None where the page carries
# no licence text, and `statement` quotes what the page does say.
DATA_CENTRE_LICENCE: dict[str, LicenceNote] = {
    "https://service.earthscope.org": LicenceNote(
        licence=None,
        source_url="https://www.earthscope.org/terms-of-service/",
        statement=(
            "EarthScope Terms of Service state no licence; they require that a User "
            "'must acknowledge EarthScope Consortium and our sponsors' (How to Cite: 'Data were "
            "accessed from the NSF NGF data archive operated by EarthScope Consortium (NSF award "
            "2435260)') and that network data be cited by DOI."
        ),
    ),
    "https://geofon.gfz.de": LicenceNote(
        licence=None,
        source_url="https://geofon.gfz.de/waveform/archive/",
        statement=(
            "GEOFON archive index marks per-network Creative Commons licences with an icon; "
            "no blanket licence statement. Check the network page before asserting CC-BY."
        ),
    ),
}

# Network DOIs as listed on the FDSN network pages (fetched 2026-09-03).
NETWORK_DOI: dict[str, tuple[str, str]] = {
    "NK": ("10.7914/SN/NK", "https://www.fdsn.org/networks/detail/NK/"),
    "IO": ("10.7914/SN/IO", "https://www.fdsn.org/networks/detail/IO/"),
    "IC": ("10.7914/SN/IC", "https://www.fdsn.org/networks/detail/IC/"),
}

COMCAT_QUERY = "https://earthquake.usgs.gov/fdsnws/event/1/query"
COMCAT_EVENT_IDS: tuple[str, ...] = ("us7000tbwb", "us7000tc90", "us20002926")
COMCAT_LANDSLIDE_START = date(2000, 1, 1)
USGS_LICENCE = LicenceNote(
    licence="US-PD",
    source_url=(
        "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits"
    ),
    statement=(
        "USGS-authored or produced data and information are considered to be in the "
        "U.S. Public Domain."
    ),
)

CAP_XSD_URL = "https://docs.oasis-open.org/emergency/cap/v1.2/CAP-v1.2.xsd"
XMLDSIG_XSD_URL = "http://www.w3.org/TR/xmldsig-core/xmldsig-core-schema.xsd"
CAP_LICENCE = LicenceNote(
    licence="Copyright OASIS Open 2010 All Rights Reserved (OASIS IPR Policy)",
    source_url="https://docs.oasis-open.org/emergency/cap/v1.2/CAP-v1.2-os.html",
    statement=(
        "CAP-v1.2-os notice: 'This document and translations of it may be copied and furnished "
        "to others, and derivative works that comment on or otherwise explain it or assist in "
        "its implementation may be prepared, copied, published, and distributed, in whole or in "
        "part, without restriction of any kind, provided that the above copyright notice and "
        "this section are included on all such copies and derivative works.' OASIS IPR Policy: "
        "https://www.oasis-open.org/policies-guidelines/ipr/"
    ),
)
XMLDSIG_LICENCE = LicenceNote(
    licence="W3C Software Notice and License (19980720)",
    source_url="http://www.w3.org/Consortium/Legal/copyright-software-19980720",
    statement=(
        "Schema header: 'This document is governed by the W3C Software License'. Licence text: "
        "'Permission to use, copy, modify, and distribute this software and its documentation, "
        "with or without modification, for any purpose and without fee or royalty is hereby "
        "granted' subject to retaining the notice."
    ),
)


# --- helpers --------------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


REFUSALS: list[str] = []

_CREATED = re.compile(rb"<Created>[^<]*</Created>")


def _mask_volatile(name: str, data: bytes) -> bytes:
    """Strip fields that change on every download but carry no data (StationXML `Created`)."""
    if name.endswith(".xml"):
        return _CREATED.sub(b"<Created/>", data)
    return data


def write_bytes(path: Path, data: bytes, *, force: bool) -> bool:
    """Write `data` unless an equivalent file exists; refuse to replace a differing one.

    A refusal keeps the existing file, records a message in `REFUSALS` and returns False so
    the caller carries on with the bytes already on disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_bytes()
        if _mask_volatile(path.name, existing) == _mask_volatile(path.name, data):
            print(f"  = unchanged {rel(path)}")
            return False
        if not force:
            message = (
                f"{rel(path)} exists with sha256 {sha256_of_file(path)[:12]} but the download "
                "differs; kept the existing file (pass --force to replace it)"
            )
            REFUSALS.append(message)
            print(f"  ! refused: {message}")
            return False
        print(f"  ! replacing {rel(path)} (--force)")
    path.write_bytes(data)
    print(f"  + wrote {rel(path)} ({len(data)} bytes)")
    return True


@dataclass
class Recorder:
    """Ledger + FIXTURES.md writer that skips rows already present."""

    ledger: JsonlManifestLedger
    md_path: Path
    rows: list[str] = field(default_factory=list)

    def known(self, path: str, sha256: str) -> bool:
        return any(e.path == path and e.sha256 == sha256 for e in self.ledger.entries())

    def retrieved_at_for(self, path: str, sha256: str) -> datetime | None:
        """When the ledger says these exact bytes were retrieved (the ledger is the truth)."""
        stamps = [
            e.retrieved_at
            for e in self.ledger.entries()
            if e.path == path and e.sha256 == sha256 and e.retrieved_at is not None
        ]
        return max(stamps) if stamps else None

    def record(self, entry: ManifestEntry, *, licence_display: str) -> None:
        if entry.path is None or entry.sha256 is None:
            raise ValueError("fixture entries must carry path and sha256")
        if self.known(entry.path, entry.sha256):
            print(f"  = ledger already has {entry.path}")
        else:
            self.ledger.append(entry)
            print(f"  + ledger {entry.path}")
        retrieved = entry.retrieved_at.isoformat() if entry.retrieved_at else ""
        self.rows.append(
            f"| `{entry.path}` | {entry.url or ''} | {retrieved} | `{entry.sha256}` | "
            f"{entry.size_bytes} | {licence_display} |"
        )

    def flush_markdown(self) -> None:
        header = (
            "# Fixtures\n\n"
            "Real, small, licence-recorded samples used by offline tests and replay. Every "
            "file has a matching `fetched` row in `data/manifest.jsonl`. Nothing synthetic "
            "lives here (synthetic doubles are under `tests/fixtures/synthetic/`).\n\n"
            "Licence `null` means the data-centre page linked in the ledger's "
            "`licence_source_url` carries no licence statement; attribution requirements are "
            "recorded in the ledger `notes`.\n\n"
            "| path | source URL | retrieved_at | sha256 | size | licence |\n"
            "|---|---|---|---|---|---|\n"
        )
        existing = self.md_path.read_text(encoding="utf-8") if self.md_path.exists() else ""
        if not existing.startswith("# Fixtures"):
            existing = header + existing
        lines = existing.rstrip("\n").split("\n")
        present = {line.split("|")[1].strip() for line in lines if line.startswith("| `")}
        for row in self.rows:
            key = row.split("|")[1].strip()
            if key not in present:
                lines.append(row)
                present.add(key)
        self.md_path.parent.mkdir(parents=True, exist_ok=True)
        self.md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def http_get(url: str, params: dict[str, str] | None = None) -> tuple[bytes, str]:
    """GET with a descriptive user agent; returns bytes and the final URL."""
    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=120.0, follow_redirects=True
    ) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.content, str(response.url)


# --- seismic ---------------------------------------------------------------------------------


def _bulk_row(sncl: Sncl, spec: EventSpec) -> list[str]:
    return [
        sncl.network,
        sncl.station,
        sncl.location or "--",
        sncl.channel,
        spec.start.strftime("%Y-%m-%dT%H:%M:%S"),
        spec.end.strftime("%Y-%m-%dT%H:%M:%S"),
    ]


def _dataselect_url(base_url: str, row: list[str]) -> str:
    net, sta, loc, cha, start, end = row
    return (
        f"{base_url}/fdsnws/dataselect/1/query?net={net}&sta={sta}&loc={loc}&cha={cha}"
        f"&start={start}&end={end}"
    )


def _station_url(base_url: str, rows: list[list[str]]) -> str:
    nets = ",".join(sorted({r[0] for r in rows}))
    stas = ",".join(sorted({r[1] for r in rows}))
    chas = ",".join(sorted({r[3] for r in rows}))
    return (
        f"{base_url}/fdsnws/station/1/query?net={nets}&sta={stas}&cha={chas}"
        f"&level=channel&start={rows[0][4]}&end={rows[0][5]}"
    )


def _read_mseed_summary(path: Path) -> dict[str, Any]:
    from obspy import read  # deferred: obspy is heavy

    stream = read(str(path), format="MSEED")
    starts = [tr.stats.starttime for tr in stream]
    ends = [tr.stats.endtime for tr in stream]
    rates = {float(tr.stats.sampling_rate) for tr in stream}
    if len(rates) != 1:
        raise RuntimeError(f"{path}: mixed sampling rates {rates}")
    return {
        "start": datetime.fromtimestamp(float(min(starts).timestamp), tz=UTC),
        "end": datetime.fromtimestamp(float(max(ends).timestamp), tz=UTC),
        "rate": rates.pop(),
        "npts": int(sum(tr.stats.npts for tr in stream)),
        "segments": len(stream),
    }


def fetch_event(spec: EventSpec, recorder: Recorder, *, force: bool) -> FixtureManifest:
    from obspy.clients.fdsn import Client
    from obspy.clients.fdsn.header import FDSNException

    print(f"== {spec.event_id}: {spec.start.isoformat()} to {spec.end.isoformat()}")
    dest = SEISMIC_DIR / spec.event_id
    dest.mkdir(parents=True, exist_ok=True)
    clients: dict[str, Any] = {}
    for alias in FDSN_CLIENTS:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            clients[alias] = Client(alias, timeout=120)
    files: list[FixtureFile] = []
    missing: list[str] = []
    served_by: dict[str, list[list[str]]] = {}  # alias -> bulk rows it served
    retrieved_at = now_utc()
    manifest_path = dest / "manifest.json"
    wrote_any = False
    for key in spec.sncls:
        sncl = Sncl.from_key(key)
        row = _bulk_row(sncl, spec)
        out = dest / f"{key}.mseed"
        served = False
        for alias, client in clients.items():
            base_url = str(client.base_url)
            tmp = out.with_suffix(".mseed.part")
            try:
                client.get_waveforms_bulk([row], filename=str(tmp))
            except FDSNException as exc:
                print(f"  - {key} not served by {alias} ({base_url}): {exc.__class__.__name__}")
                tmp.unlink(missing_ok=True)
                continue
            data = tmp.read_bytes()
            tmp.unlink()
            if not data:
                print(f"  - {key}: {alias} returned an empty body")
                continue
            wrote_any |= write_bytes(out, data, force=force)
            summary = _read_mseed_summary(out)
            sha = sha256_of_file(out)
            url = _dataselect_url(base_url, row)
            licence = DATA_CENTRE_LICENCE[base_url]
            doi, doi_url = NETWORK_DOI.get(sncl.network, (None, None))
            files.append(
                FixtureFile(
                    path=out.name,
                    kind="miniseed",
                    sha256=sha,
                    size_bytes=out.stat().st_size,
                    sncl=key,
                    start_utc=summary["start"],
                    end_utc=summary["end"],
                    sampling_rate_hz=summary["rate"],
                    npts=summary["npts"],
                    url=url,
                )
            )
            recorder.record(
                ManifestEntry(
                    source=DataSource.fdsn_waveforms,
                    product_id=f"{spec.event_id}/{out.name}",
                    event_id=spec.event_id,
                    path=rel(out),
                    url=url,
                    params={"client": alias, "base_url": base_url, "bulk": row},
                    sha256=sha,
                    size_bytes=out.stat().st_size,
                    retrieved_at=retrieved_at,
                    licence=licence.licence or "null: see licence_source_url",
                    licence_source_url=licence.source_url,
                    provenance=Provenance.real,
                    status=ManifestStatus.fetched,
                    time_start=summary["start"],
                    time_end=summary["end"],
                    adapter=ADAPTER,
                    adapter_version=__version__,
                    notes=(
                        f"{licence.statement} Network DOI {doi} ({doi_url}). "
                        f"{summary['segments']} MiniSEED segment(s), {summary['npts']} samples "
                        f"at {summary['rate']} Hz."
                    ),
                ),
                licence_display=licence.licence or "null",
            )
            served_by.setdefault(alias, []).append(row)
            served = True
            break
        if not served:
            missing.append(key)
    # StationXML from whichever data centre served waveforms.
    for index, (alias, rows) in enumerate(served_by.items()):
        client = clients[alias]
        base_url = str(client.base_url)
        out = dest / ("stations.xml" if index == 0 else f"stations.{alias.lower()}.xml")
        tmp = out.with_suffix(".xml.part")
        try:
            client.get_stations_bulk(rows, level="channel", filename=str(tmp))
        except FDSNException as exc:
            print(f"  - station metadata not served by {alias}: {exc.__class__.__name__}")
            tmp.unlink(missing_ok=True)
            continue
        data = tmp.read_bytes()
        tmp.unlink()
        wrote_any |= write_bytes(out, data, force=force)
        sha = sha256_of_file(out)
        url = _station_url(base_url, rows)
        licence = DATA_CENTRE_LICENCE[base_url]
        files.append(
            FixtureFile(
                path=out.name, kind="stationxml", sha256=sha, size_bytes=out.stat().st_size, url=url
            )
        )
        recorder.record(
            ManifestEntry(
                source=DataSource.fdsn_waveforms,
                product_id=f"{spec.event_id}/{out.name}",
                product_level="stationxml-channel",
                event_id=spec.event_id,
                path=rel(out),
                url=url,
                params={"client": alias, "base_url": base_url, "bulk": rows, "level": "channel"},
                sha256=sha,
                size_bytes=out.stat().st_size,
                retrieved_at=retrieved_at,
                licence=licence.licence or "null: see licence_source_url",
                licence_source_url=licence.source_url,
                provenance=Provenance.real,
                status=ManifestStatus.fetched,
                time_start=spec.start,
                time_end=spec.end,
                adapter=ADAPTER,
                adapter_version=__version__,
                notes=licence.statement,
            ),
            licence_display=licence.licence or "null",
        )
    if served_by:
        first_alias = next(iter(served_by))
        base_url = str(clients[first_alias].base_url)
        licence = DATA_CENTRE_LICENCE[base_url]
        request = FixtureRequest(
            client=first_alias,
            base_url=base_url,
            bulk=[row for rows in served_by.values() for row in rows],
            station_level="channel",
            tool=f"obspy {_obspy_version()}",
        )
    else:
        base_url = str(clients[FDSN_CLIENTS[0]].base_url)
        licence = DATA_CENTRE_LICENCE[base_url]
        request = FixtureRequest(
            client=FDSN_CLIENTS[0],
            base_url=base_url,
            bulk=[_bulk_row(Sncl.from_key(k), spec) for k in spec.sncls],
            station_level="channel",
            tool=f"obspy {_obspy_version()}",
        )
    status: Literal["fetched", "partial", "not_fetched"]
    if not files:
        status = "not_fetched"
    elif missing:
        status = "partial"
    else:
        status = "fetched"
    dois = ", ".join(
        f"{net}: {NETWORK_DOI[net][0]}"
        for net in sorted({Sncl.from_key(k).network for k in spec.sncls})
        if net in NETWORK_DOI
    )
    if not wrote_any and files:
        # Nothing on disk changed: the manifest must carry the ledger's retrieval time.
        stamps = [recorder.retrieved_at_for(rel(dest / f.path), f.sha256) for f in files]
        known = [stamp for stamp in stamps if stamp is not None]
        if known:
            retrieved_at = max(known)
    manifest = FixtureManifest(
        event_id=spec.event_id,
        window=TimeWindow(start_utc=spec.start, end_utc=spec.end),
        files=files,
        missing=missing,
        request=request,
        retrieved_at_utc=retrieved_at if files else None,
        licence=licence.licence,
        licence_source_url=licence.source_url,
        status=status,
        notes=(
            f"{licence.statement} Network DOIs: {dois}. Bytes are exactly as returned by "
            "fdsnws-dataselect; no re-encoding. Origin time is not stored here: replay reads it "
            "from the event-library record."
        ),
    )
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"  + wrote {rel(manifest_path)} status={status} missing={missing}")
    return manifest


def _obspy_version() -> str:
    from obspy import __version__ as obspy_version

    return str(obspy_version)


# --- ComCat ----------------------------------------------------------------------------------


def fetch_comcat(recorder: Recorder, *, force: bool) -> None:
    print("== USGS ComCat")
    COMCAT_DIR.mkdir(parents=True, exist_ok=True)
    today = now_utc().date()
    jobs: list[tuple[Path, dict[str, str], str]] = [
        (
            COMCAT_DIR
            / f"landslide_{COMCAT_LANDSLIDE_START.isoformat()}_{today.isoformat()}.geojson",
            {
                "format": "geojson",
                "eventtype": "landslide",
                "starttime": COMCAT_LANDSLIDE_START.isoformat(),
                "endtime": today.isoformat(),
                "orderby": "time-asc",
                "limit": "20000",
            },
            f"landslide_{COMCAT_LANDSLIDE_START.isoformat()}_{today.isoformat()}",
        ),
    ]
    jobs.extend(
        (COMCAT_DIR / f"{event_id}.geojson", {"eventid": event_id, "format": "geojson"}, event_id)
        for event_id in COMCAT_EVENT_IDS
    )
    for out, params, product_id in jobs:
        retrieved_at = now_utc()
        data, url = http_get(COMCAT_QUERY, params)
        parsed = json.loads(data)
        count = len(parsed.get("features", [])) if parsed.get("type") == "FeatureCollection" else 1
        write_bytes(out, data, force=force)
        sha = sha256_of_file(out)
        recorder.record(
            ManifestEntry(
                source=DataSource.usgs_comcat,
                product_id=product_id,
                path=rel(out),
                url=url,
                params=params,
                sha256=sha,
                size_bytes=out.stat().st_size,
                retrieved_at=retrieved_at,
                licence=USGS_LICENCE.licence or "null: see licence_source_url",
                licence_source_url=USGS_LICENCE.source_url,
                provenance=Provenance.real,
                status=ManifestStatus.fetched,
                adapter=ADAPTER,
                adapter_version=__version__,
                notes=f"{USGS_LICENCE.statement} {count} feature(s) in the response.",
            ),
            licence_display=USGS_LICENCE.licence or "null",
        )


# --- CAP schemas ------------------------------------------------------------------------------


def fetch_cap(recorder: Recorder, *, force: bool) -> None:
    print("== CAP 1.2 schemas")
    CAP_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = CAP_DIR / "MANIFEST.json"
    entries: list[dict[str, Any]] = []
    for url, name, licence in (
        (CAP_XSD_URL, "CAP-v1.2.xsd", CAP_LICENCE),
        (XMLDSIG_XSD_URL, "xmldsig-core-schema.xsd", XMLDSIG_LICENCE),
    ):
        out = CAP_DIR / name
        retrieved_at = now_utc()
        try:
            data, final_url = http_get(url)
        except httpx.HTTPError as exc:
            print(f"  - {name}: download failed: {exc}")
            entries.append(
                {
                    "file": name,
                    "url": url,
                    "status": "not_fetched",
                    "error": str(exc),
                    "licence": licence.licence,
                    "licence_source_url": licence.source_url,
                }
            )
            continue
        wrote = write_bytes(out, data, force=force)
        sha = sha256_of_file(out)
        if not wrote:
            retrieved_at = recorder.retrieved_at_for(rel(out), sha) or retrieved_at
        entries.append(
            {
                "file": name,
                "url": final_url,
                "status": "fetched",
                "retrieved_at_utc": retrieved_at.isoformat(),
                "sha256": sha,
                "size_bytes": out.stat().st_size,
                "licence": licence.licence,
                "licence_source_url": licence.source_url,
                "licence_notes": licence.statement,
            }
        )
        recorder.record(
            ManifestEntry(
                source=DataSource.vendored_schema,
                product_id=f"cap/{name}",
                path=rel(out),
                url=final_url,
                sha256=sha,
                size_bytes=out.stat().st_size,
                retrieved_at=retrieved_at,
                licence=licence.licence or "null: see licence_source_url",
                licence_source_url=licence.source_url,
                provenance=Provenance.real,
                status=ManifestStatus.fetched,
                adapter=ADAPTER,
                adapter_version=__version__,
                notes=licence.statement,
            ),
            licence_display=licence.licence or "null",
        )
    manifest = {
        "contract_version": "0.1.0",
        "description": (
            "Vendored XML Schemas for offline CAP 1.2 validation. CAP-v1.2.xsd does not import "
            'xmldsig; it declares <any namespace="http://www.w3.org/2000/09/xmldsig#" '
            'processContents="lax"/>, so validation works without the xmldsig schema. The '
            "xmldsig schema is vendored so a local lxml resolver can validate ds:Signature "
            "strictly if a future stage signs alerts."
        ),
        "files": entries,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"  + wrote {rel(manifest_path)}")


# --- main ------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--only",
        choices=("seismic", "comcat", "cap"),
        action="append",
        help="Restrict to one group (repeatable). Default: all.",
    )
    parser.add_argument("--force", action="store_true", help="Replace fixtures whose bytes differ.")
    args = parser.parse_args(argv)
    groups = set(args.only or ("seismic", "comcat", "cap"))
    recorder = Recorder(ledger=JsonlManifestLedger(LEDGER_PATH), md_path=FIXTURES_MD)
    try:
        if "seismic" in groups:
            for spec in EVENTS:
                fetch_event(spec, recorder, force=args.force)
        if "comcat" in groups:
            fetch_comcat(recorder, force=args.force)
        if "cap" in groups:
            fetch_cap(recorder, force=args.force)
    finally:
        recorder.flush_markdown()
    if REFUSALS:
        print(f"{len(REFUSALS)} download(s) refused; existing files kept:", file=sys.stderr)
        for message in REFUSALS:
            print(f"  {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
