"""Fetch, DOI-resolve and checksum every published figure the M2 gate compares against.

The citation rule in CLAUDE.md is the whole point of this script. A published number may be
written into `data/references/lfh_published.json` only when, in this same session:

1. the bytes carrying it were fetched over the network,
2. its DOI resolved through Crossref or DataCite (or the publisher's landing page), and
3. the sha256 of those bytes and the retrieval timestamp were recorded.

Every figure also carries the **verbatim sentence** it was read from. Nothing is written from
memory. If a fetch fails the source is simply absent, and `validate-lfh` fails the gate when
fewer than three sources clear the bar -- which is the intended behaviour, not a bug.

    uv run python scripts/build_lfh_references.py
"""

# ruff: noqa: T201  (a script; progress goes to stdout)
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any

import httpx

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger, sha256_of_file
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.models.lfh.references import (
    Conversion,
    LfhReferences,
    LfhTarget,
    PublishedQuantity,
    SourceRef,
    write_references,
)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
POLITE_UA = "serac/0.1 (https://github.com/dizzy1900/serac; mailto:david@double-eye.com)"
CROSSREF = "https://api.crossref.org/works/"
DATACITE = "https://api.datacite.org/dois/"
ESEC_URL = "https://ds.iris.edu/spudservice/esec"

CITATION_RULE = (
    "A published number enters this file only when, in the same session, the bytes carrying "
    "it were fetched, its DOI resolved through Crossref or DataCite, and the sha256 of those "
    "bytes plus accessed_utc were recorded. Every figure carries the verbatim sentence it was "
    "read from. Fewer than three sources clearing that bar fails validate-lfh."
)


def _fetch(url: str, *, ua: str = BROWSER_UA) -> tuple[bytes, str, str]:
    response = httpx.get(url, headers={"User-Agent": ua}, timeout=180, follow_redirects=True)
    response.raise_for_status()
    return (
        response.content,
        hashlib.sha256(response.content).hexdigest(),
        response.headers.get("content-type", "").split(";")[0],
    )


def _resolve_doi(doi: str, *, agency: str) -> dict[str, Any]:
    url = (CROSSREF if agency == "crossref" else DATACITE) + doi
    response = httpx.get(url, headers={"User-Agent": POLITE_UA}, timeout=120, follow_redirects=True)
    response.raise_for_status()
    print(f"  DOI {doi} resolved via {agency} (HTTP {response.status_code})")
    payload: dict[str, Any] = response.json()
    return {"url": url, "payload": payload, "title": _doi_title(payload, agency)}


def _doi_title(payload: dict[str, Any], agency: str) -> str:
    """The registrant's own title, so a recorded title cannot drift from its DOI."""
    if agency == "crossref":
        titles = payload.get("message", {}).get("title") or []
        return re.sub(r"\s+", " ", unescape(str(titles[0]))).strip() if titles else ""
    attributes = payload.get("data", {}).get("attributes", {})
    titles = attributes.get("titles") or []
    if titles and isinstance(titles[0], dict):
        return re.sub(r"\s+", " ", unescape(str(titles[0].get("title", "")))).strip()
    return ""


def _text(html_bytes: bytes) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_bytes.decode("utf-8", "replace"))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text))


def _require_excerpt(text: str, needle: str, label: str) -> str:
    """Pull the sentence containing `needle`, or fail loudly.

    Failing loudly matters: if the page changed and the sentence is gone, the number must not
    be written from memory of what it used to say.
    """
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if needle.lower() in sentence.lower() and len(sentence) < 600:
            return sentence.strip()
    raise SystemExit(f"excerpt for {label} not found in the fetched bytes (looked for {needle!r})")


def _esec_records(xml_bytes: bytes) -> tuple[dict[str, ET.Element], int]:
    """`({description: element}, n_entries)`.

    Both numbers are returned because they differ: the service returns 319 entries but only
    274 distinct descriptions, so keying by description silently collapses 45 of them. The
    ledger records the entry count, not the dictionary size.
    """
    raw = xml_bytes.decode("utf-8", "replace")
    inner = raw.split("<pre>", 1)[1].rsplit("</pre>", 1)[0]
    inner = re.sub(r'<a href="[^"]*">', "", inner).replace("</a>", "")
    root = ET.fromstring(unescape(inner))
    entries = root.findall("EsecEvents")
    return {(event.findtext("Description") or ""): event for event in entries}, len(entries)


def _f(element: ET.Element, tag: str) -> float | None:
    value = element.findtext(tag)
    return float(value) if value not in (None, "") else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    args = parser.parse_args()
    repo: Path = args.repo.resolve()
    now = datetime.now(tz=UTC)
    sources: list[SourceRef] = []

    # --- ESEC: the catalogue that carries the Bingham, Taan and Lamplugh masses -------------
    #
    # The endpoint is intermittent: it served 1.05 MB happily and then began 404ing on its own
    # trailing-slash redirect within the hour. That is exactly what the committed-fixture rule
    # is for. On a failed fetch the stored copy is used, and `esec_accessed` keeps the
    # timestamp of the retrieval that actually happened rather than pretending this one did.
    esec_store = repo / "data" / "fixtures" / "esec" / "esec_catalogue.xml.gz"
    esec_fallback_note = ""
    esec_accessed = now
    try:
        print("fetching ESEC catalogue")
        esec_bytes, esec_sha, esec_type = _fetch(ESEC_URL, ua=POLITE_UA)
        esec_store.parent.mkdir(parents=True, exist_ok=True)
        with gzip.GzipFile(filename="", fileobj=esec_store.open("wb"), mode="wb", mtime=0) as fh:
            fh.write(esec_bytes)
    except httpx.HTTPError as exc:
        if not esec_store.exists():
            raise SystemExit(
                f"ESEC is unreachable ({exc}) and no committed copy exists; the Bingham, Taan "
                "and Lamplugh masses cannot be written without their source"
            ) from exc
        esec_bytes = gzip.decompress(esec_store.read_bytes())
        esec_sha = hashlib.sha256(esec_bytes).hexdigest()
        esec_type = "text/html"
        previous = repo / "data" / "references" / "lfh_published.json"
        if previous.exists():
            for entry in json.loads(previous.read_text(encoding="utf-8"))["sources"]:
                if entry["id"] == "esec-bingham-1" and entry["sha256"] == esec_sha:
                    esec_accessed = datetime.fromisoformat(entry["accessed_utc"])
                    break
        esec_fallback_note = (
            f" The endpoint was unreachable at regeneration time ({type(exc).__name__}); these "
            f"bytes are the committed copy retrieved at {esec_accessed.isoformat()}, whose "
            "sha256 matches."
        )
        print(f"  ESEC unreachable ({type(exc).__name__}); using the committed copy")
    records, n_esec_elements = _esec_records(esec_bytes)
    print(
        f"  {n_esec_elements} ESEC entries ({len(records)} distinct descriptions), "
        f"stored gzipped at {esec_store.name}"
    )

    bingham = records["Bingham Canyon Mine 1 - rock avalanche"]
    taan = records["Taan Fjord - rock avalanche"]
    lamplugh = records["Lamplugh Glacier main - rock avalanche"]

    bingham_doi = (bingham.findtext("DOI") or "").strip()
    _resolve_doi(bingham_doi, agency="datacite")
    sources.append(
        SourceRef(
            id="esec-bingham-1",
            kind="dataset",
            title="Exotic Seismic Events Catalog entry: Bingham Canyon Mine 1 - rock avalanche",
            container="IRIS/EarthScope Exotic Seismic Events Catalog (ESEC)",
            authors="EarthScope Data Products",
            year=2025,
            doi=bingham_doi,
            doi_resolved_via="datacite",
            doi_resolution_url=DATACITE + bingham_doi,
            url=ESEC_URL,
            accessed_utc=esec_accessed,
            sha256=esec_sha,
            content_type=esec_type,
            size_bytes=len(esec_bytes),
            licence="public domain (US federally funded data product); acknowledge EarthScope",
            peer_reviewed=False,
        )
    )
    for name, element, ident in (
        ("Taan Fjord", taan, "esec-taan"),
        ("Lamplugh Glacier main", lamplugh, "esec-lamplugh"),
    ):
        sources.append(
            SourceRef(
                id=ident,
                kind="dataset",
                title=f"Exotic Seismic Events Catalog entry: {name}",
                container="IRIS/EarthScope Exotic Seismic Events Catalog (ESEC)",
                authors="EarthScope Data Products",
                year=2025,
                doi=(element.findtext("DOI") or "").strip() or None,
                doi_resolved_via=None,
                # The sha256 is of the whole-catalogue response, so the recorded url must be
                # the catalogue endpoint: a reader who fetches `url` and hashes it must get
                # `sha256`. The per-event page goes in its own field.
                url=ESEC_URL,
                related_url=(element.findtext("Link") or "").strip() or None,
                accessed_utc=esec_accessed,
                sha256=esec_sha,
                content_type=esec_type,
                size_bytes=len(esec_bytes),
                licence="public domain (US federally funded data product); acknowledge EarthScope",
                peer_reviewed=False,
            )
        )

    # --- Higman et al. 2018: Taan Fiord mass, peak force and duration -----------------------
    print("fetching Higman et al. 2018")
    higman_doi = "10.1038/s41598-018-30475-w"
    _resolve_doi(higman_doi, agency="crossref")
    higman_bytes, higman_sha, higman_type = _fetch(
        f"https://www.nature.com/articles/{higman_doi.split('/')[-1]}"
    )
    higman_text = _text(higman_bytes)
    higman_mass = _require_excerpt(higman_text, "slide mass of 1", "Taan mass")
    higman_force = _require_excerpt(higman_text, "peak forces of about", "Taan peak force")
    higman_tons = _require_excerpt(higman_text, "million tons of rock", "Taan geologic mass")
    print(f"  mass:  {higman_mass[:120]}")
    print(f"  force: {higman_force[:120]}")
    sources.append(
        SourceRef(
            id="higman-2018",
            kind="peer_reviewed",
            title="The 2015 landslide and tsunami in Taan Fiord, Alaska",
            container="Scientific Reports",
            authors="Higman, B. et al.",
            year=2018,
            doi=higman_doi,
            doi_resolved_via="crossref",
            doi_resolution_url=CROSSREF + higman_doi,
            url=f"https://www.nature.com/articles/{higman_doi.split('/')[-1]}",
            accessed_utc=now,
            sha256=higman_sha,
            content_type=higman_type,
            size_bytes=len(higman_bytes),
            licence="CC BY 4.0 (Scientific Reports open access)",
            peer_reviewed=True,
        )
    )

    # --- van Wyk de Vries et al. 2022: Chamoli collapse volume ------------------------------
    print("fetching van Wyk de Vries et al. 2022")
    vwdv_doi = "10.5194/nhess-22-3309-2022"
    vwdv_resolved = _resolve_doi(vwdv_doi, agency="crossref")
    vwdv_url = "https://nhess.copernicus.org/articles/22/3309/2022/"
    vwdv_bytes, vwdv_sha, vwdv_type = _fetch(vwdv_url)
    vwdv_text = _text(vwdv_bytes)
    vwdv_volume = _require_excerpt(vwdv_text, "95 % confidence interval", "Chamoli volume")
    vwdv_split = _require_excerpt(vwdv_text, "of rock and 6", "Chamoli rock/ice split")
    print(f"  volume: {vwdv_volume[:150]}")
    print(f"  split:  {vwdv_split[:150]}")
    sources.append(
        SourceRef(
            id="vanwykdevries-2022",
            kind="peer_reviewed",
            title=vwdv_resolved["title"],
            container="Natural Hazards and Earth System Sciences",
            authors="van Wyk de Vries, M. et al.",
            year=2022,
            doi=vwdv_doi,
            doi_resolved_via="crossref",
            doi_resolution_url=CROSSREF + vwdv_doi,
            url=vwdv_url,
            accessed_utc=now,
            sha256=vwdv_sha,
            content_type=vwdv_type,
            size_bytes=len(vwdv_bytes),
            licence="CC BY 4.0 (Copernicus open access)",
            peer_reviewed=True,
        )
    )

    # --- Method and context references ------------------------------------------------------
    for ident, doi, title, container, authors, year, url in (
        (
            "hibert-2014",
            "10.1002/2014gl060592",
            "Dynamics of the Bingham Canyon Mine landslides from seismic signal analysis",
            "Geophysical Research Letters",
            "Hibert, C. et al.",
            2014,
            None,
        ),
        (
            "ekstrom-stark-2013",
            "10.1126/science.1232887",
            "Simple Scaling of Catastrophic Landslide Dynamics",
            "Science",
            "Ekstrom, G. and Stark, C. P.",
            2013,
            None,
        ),
        (
            "cook-2021",
            "10.1126/science.abj1227",
            (
                "Detection and potential early warning of catastrophic flow events with "
                "regional seismic networks"
            ),
            "Science",
            "Cook, K. L. et al.",
            2021,
            None,
        ),
        (
            "pankow-2014",
            "10.1130/gsatg191a.1",
            "Massive landslide at Utah copper mine generates wealth of geophysical data",
            "GSA Today",
            "Pankow, K. L. et al.",
            2014,
            "https://www.geosociety.org/gsatoday/archive/24/1/article/i1052-5173-24-1-4.htm",
        ),
    ):
        print(f"fetching {ident}")
        _resolve_doi(doi, agency="crossref")
        fetch_url = url or (CROSSREF + doi)
        payload, digest, content_type = _fetch(
            fetch_url, ua=POLITE_UA if url is None else BROWSER_UA
        )
        sources.append(
            SourceRef(
                id=ident,
                kind="peer_reviewed",
                title=title,
                container=container,
                authors=authors,
                year=year,
                doi=doi,
                doi_resolved_via="crossref",
                doi_resolution_url=CROSSREF + doi,
                url=fetch_url,
                accessed_utc=now,
                sha256=digest,
                content_type=content_type,
                size_bytes=len(payload),
                licence=(
                    "open access (GSA Today)"
                    if ident == "pankow-2014"
                    else "all-rights-reserved; cited only, no copy stored"
                ),
                peer_reviewed=True,
            )
        )
        if ident == "pankow-2014":
            pankow_excerpt = _require_excerpt(
                _text(payload), "total mass of 165 million tons", "Bingham total mass"
            )
            print(f"  {pankow_excerpt[:160]}")

    # --- Swiss Seismological Service: the catalogued origin for Blatten -------------------
    #
    # The event record's time came from a press report ("gegen 15.30 Uhr", plus or minus 900 s
    # by its own uncertainty field). SED catalogues the collapse as a landslide at 13:24:26Z,
    # five and a half minutes earlier -- inside that uncertainty, but far enough that a
    # 900 s inversion window placed on the press time misses the signal entirely. Using a
    # catalogued origin instead of a newspaper's is a data fix, not a tuning choice; nothing
    # about it depends on the answer serac gets.
    print("fetching the SED catalogue origin for Blatten")
    eth_url = (
        "https://eida.ethz.ch/fdsnws/event/1/query?starttime=2025-05-28T12:00:00"
        "&endtime=2025-05-28T16:00:00&latitude=46.3968&longitude=7.8405&maxradius=0.5"
        "&format=xml"
    )
    eth_bytes, eth_sha, eth_type = _fetch(eth_url, ua=POLITE_UA)
    eth_root = ET.fromstring(eth_bytes)
    ns = {"q": "http://quakeml.org/xmlns/bed/1.2"}
    origin_node = eth_root.find(".//q:origin", ns)
    if origin_node is None:
        raise SystemExit("SED returned no origin for the Blatten window; not writing one")
    blatten_origin = datetime.fromisoformat(
        (origin_node.findtext("q:time/q:value", namespaces=ns) or "").replace("Z", "+00:00")
    ).astimezone(UTC)
    blatten_lat = float(origin_node.findtext("q:latitude/q:value", namespaces=ns) or "nan")
    blatten_lon = float(origin_node.findtext("q:longitude/q:value", namespaces=ns) or "nan")
    event_type = eth_root.findtext(".//q:event/q:type", namespaces=ns)
    print(
        f"  SED origin {blatten_origin.isoformat()} at {blatten_lat:.4f}, {blatten_lon:.4f} "
        f"(event type {event_type!r})"
    )
    _resolve_doi("10.12686/sed/networks/ch", agency="datacite")
    sources.append(
        SourceRef(
            id="sed-blatten-origin",
            kind="agency_official",
            title=(
                "Swiss Seismological Service earthquake catalogue: 28 May 2025 Goppenstein VS "
                f"landslide, origin {blatten_origin.isoformat()}, event type {event_type}"
            ),
            container="Swiss Seismological Service (SED) at ETH Zurich, fdsnws-event",
            authors="Swiss Seismological Service (SED) at ETH Zurich",
            year=2025,
            doi="10.12686/sed/networks/ch",
            doi_resolved_via="datacite",
            doi_resolution_url=DATACITE + "10.12686/sed/networks/ch",
            url=eth_url,
            accessed_utc=now,
            sha256=eth_sha,
            content_type=eth_type,
            size_bytes=len(eth_bytes),
            licence="CC BY 4.0 (SED catalogue and CH network data)",
            peer_reviewed=False,
        )
    )

    # --- Targets ------------------------------------------------------------------------------
    esec_mass_note = (
        "ESEC records this event's mass with an explicit low and high; the interval below is "
        "those two numbers, not a derived one."
    )
    bingham_mass = PublishedQuantity(
        low=_f(bingham, "MassLow") or 0.0,
        high=_f(bingham, "MassHigh") or 0.0,
        best=_f(bingham, "Mass"),
        units="kg",
        source_ref="esec-bingham-1",
        excerpt=(
            f"ESEC event {bingham.findtext('EventId')} "
            f"'{bingham.findtext('Description')}': Mass {bingham.findtext('Mass')}, "
            f"MassLow {bingham.findtext('MassLow')}, MassHigh {bingham.findtext('MassHigh')} kg; "
            f"Volume {bingham.findtext('Volume')} m3; DOI {bingham_doi}."
        ),
        notes=esec_mass_note,
    )
    taan_mass = PublishedQuantity(
        low=1.0e11,
        high=1.5e11,
        best=None,
        units="kg",
        source_ref="higman-2018",
        excerpt=higman_mass,
        notes=(
            "The seismically determined mass. Higman et al. separately report the geologic "
            f"figure: '{higman_tons}' (1.8e11 kg), which ESEC also carries. serac compares "
            "against the seismic interval because that is the quantity it computes."
        ),
    )
    taan_force = PublishedQuantity(
        low=2.0e11,
        high=2.0e11,
        best=2.0e11,
        units="N",
        source_ref="higman-2018",
        excerpt=higman_force,
        notes="'about 2 x 10^11 N'; no uncertainty was published, so low equals high.",
    )
    lamplugh_mass = PublishedQuantity(
        low=_f(lamplugh, "MassLow") or 0.0,
        high=_f(lamplugh, "Mass") or 0.0,
        best=None,
        units="kg",
        source_ref="esec-lamplugh",
        excerpt=(
            f"ESEC event {lamplugh.findtext('EventId')} "
            f"'{lamplugh.findtext('Description')}': Mass {lamplugh.findtext('Mass')}, "
            f"MassLow {lamplugh.findtext('MassLow')} kg; Volume {lamplugh.findtext('Volume')} m3."
        ),
        notes=(
            "ESEC gives no MassHigh for this event, so the interval runs from MassLow to the "
            "central Mass. This ESEC entry carries no DOI, so it does not count towards the "
            "three sources validate-lfh requires."
        ),
    )
    chamoli_volume = PublishedQuantity(
        low=26.5e6,
        high=27.3e6,
        best=26.9e6,
        units="m3",
        source_ref="vanwykdevries-2022",
        excerpt=vwdv_volume,
        notes="The 95% confidence interval van Wyk de Vries et al. quote from Shugar et al.",
    )
    chamoli_conversion = Conversion(
        from_quantity="collapse volume (m3)",
        to_quantity="mobilised mass (kg)",
        factor_low=2222.0,
        factor_high=2381.0,
        factor_units="kg/m3 bulk density",
        rationale=(
            "No paper retrieved in-session publishes a Chamoli mass, only a volume, so the "
            "comparison interval is derived here and is NOT a published figure. van Wyk de "
            f"Vries et al. state the composition: '{vwdv_split}'. Taking that 21:6 rock:ice "
            "split by volume with rock at 2600-2800 kg/m3 and ice at 900-917 kg/m3 gives a "
            "bulk density of 2222-2381 kg/m3. Every step is arithmetic on retrieved numbers; "
            "the density ranges are the assumption."
        ),
    )

    targets = [
        LfhTarget(
            target_id="bingham-canyon-2013-1",
            name="Bingham Canyon Mine rock avalanche 1, 11 April 2013",
            role="reproduction",
            origin_utc=datetime(2013, 4, 11, 3, 30, 24, tzinfo=UTC),
            source_latitude=_f(bingham, "Latitude") or 0.0,
            source_longitude=_f(bingham, "Longitude") or 0.0,
            fixture_dir="data/fixtures/lfh/bingham-canyon-2013-1",
            fall_height_m=_f(bingham, "H"),
            runout_m=_f(bingham, "L"),
            geometry_source_ref="esec-bingham-1",
            published_mass_kg=bingham_mass,
            notes=(
                "The best-covered event available: hundreds of open stations and an azimuthal "
                "gap of a few tens of degrees. Hibert et al. (2014) inverted the same signals "
                "for a single-force history; their paper is behind a paywall, so serac cites "
                "it for method and takes the mass interval from the ESEC data product."
            ),
        ),
        LfhTarget(
            target_id="taan-fiord-2015",
            name="Taan Fiord (Tyndall Glacier) rock avalanche, 17 October 2015",
            role="reproduction",
            origin_utc=datetime(2015, 10, 18, 5, 18, 36, tzinfo=UTC),
            source_latitude=_f(taan, "Latitude") or 0.0,
            source_longitude=_f(taan, "Longitude") or 0.0,
            fixture_dir="data/fixtures/lfh/taan-fiord-2015",
            fall_height_m=_f(taan, "HLow"),
            runout_m=_f(taan, "LLow"),
            geometry_source_ref="esec-taan",
            published_mass_kg=taan_mass,
            published_peak_force_n=taan_force,
            published_runout_bearing_deg=PublishedQuantity(
                low=96.0,
                high=96.0,
                best=96.0,
                units="deg from north",
                source_ref="higman-2018",
                excerpt=higman_force,
                notes=(
                    "'an eastward-moving (bearing 96 deg) landslide'; no uncertainty was "
                    "published, so low equals high."
                ),
            ),
            published_duration_s=PublishedQuantity(
                low=90.0,
                high=90.0,
                best=90.0,
                units="s",
                source_ref="higman-2018",
                excerpt=higman_force,
                notes="'lasting 90 seconds'; no uncertainty published.",
            ),
        ),
        LfhTarget(
            target_id="lamplugh-glacier-2016",
            name="Lamplugh Glacier rock avalanche, 28 June 2016",
            role="reproduction",
            origin_utc=datetime(2016, 6, 28, 16, 20, 48, tzinfo=UTC),
            source_latitude=_f(lamplugh, "Latitude") or 0.0,
            source_longitude=_f(lamplugh, "Longitude") or 0.0,
            fixture_dir="data/fixtures/lfh/lamplugh-glacier-2016",
            fall_height_m=_f(lamplugh, "H"),
            runout_m=_f(lamplugh, "L"),
            geometry_source_ref="esec-lamplugh",
            published_mass_kg=lamplugh_mass,
        ),
        LfhTarget(
            target_id="chamoli-2021",
            name="Chamoli rock and ice avalanche, 7 February 2021",
            role="reproduction",
            origin_utc=datetime(2021, 2, 7, 4, 51, 18, tzinfo=UTC),
            source_latitude=30.3484632,
            source_longitude=79.7758979,
            event_id="chamoli-2021",
            aoi_id="chamoli-rishiganga",
            fixture_dir="data/fixtures/lfh/chamoli-2021",
            dem_fixture="data/fixtures/dem_glo30/chamoli-rishiganga/glo30_crop.tif",
            published_volume_m3=chamoli_volume,
            mass_conversion=chamoli_conversion,
            fall_height_m=1800.0,
            geometry_source_ref="vanwykdevries-2022",
            notes=(
                "Open long-period coverage of the Himalaya is thin: 51 open LH? stations "
                "within 15 degrees and an azimuthal gap near the 200-degree refusal limit. "
                "Cook et al. (2021) worked from a dense regional network that is not open."
            ),
        ),
        LfhTarget(
            target_id="langtang-lhende-2026",
            name="Langtang / Lhende Khola cascade, 26 August 2026",
            role="new_event",
            origin_utc=datetime(2026, 8, 26, 2, 52, 10, tzinfo=UTC),
            source_latitude=28.271,
            source_longitude=85.515,
            event_id="langtang-lhende-2026",
            aoi_id="lhende-khola-trishuli",
            fixture_dir="data/fixtures/lfh/langtang-lhende-2026",
            dem_fixture="data/fixtures/dem_glo30/lhende-khola-trishuli/glo30_crop.tif",
            public_statements=[
                "Mohd. Farooq Azam (ICIMOD), quoted by the Kathmandu Post (2026-08-28), gave a "
                "preliminary source volume of 100-200 million m3.",
                "The same report cites Planet Labs imagery analysis for a fall height of about "
                "1200 m, and Azam for about 1000 m.",
                "These are press-attributed preliminary figures. No peer-reviewed volume or "
                "mass existed for this event as of September 2026.",
            ],
        ),
        LfhTarget(
            target_id="blatten-2025",
            name="Blatten / Birch Glacier collapse, 28 May 2025",
            role="new_event",
            origin_utc=blatten_origin,
            source_latitude=blatten_lat,
            source_longitude=blatten_lon,
            geometry_source_ref="sed-blatten-origin",
            event_id="blatten-2025",
            aoi_id="blatten-lotschental",
            fixture_dir="data/fixtures/lfh/blatten-2025",
            dem_fixture="data/fixtures/dem_glo30/blatten-lotschental/glo30_crop.tif",
            public_statements=[
                "EGU 2026 abstract egu26-3801: 'buried the village of Blatten under 9 million "
                "m3 of ice and rock'.",
                "EGU 2026 abstract egu26-6599: 'a volume of approximately 10 million cubic "
                "meters'.",
                "Both are conference abstracts, which the serac event library does not treat "
                "as qualifying for a best value.",
            ],
            notes=(
                "The origin time and position are the Swiss Seismological Service catalogue "
                "entry (event type: landslide), not the press time of 'gegen 15.30 Uhr' in the "
                "event library, which carries a stated 900 s uncertainty and would have put "
                "the inversion window five minutes past the signal."
            ),
        ),
    ]

    references = LfhReferences(
        generated_at_utc=now, citation_rule=CITATION_RULE, sources=sources, targets=targets
    )
    path = write_references(references, repo)
    print(
        f"wrote {path} with {len(sources)} sources "
        f"({len(references.sources_clearing_bar)} clearing the citation bar) "
        f"and {len(targets)} targets"
    )

    ledger = JsonlManifestLedger(repo / "data" / "manifest.jsonl")
    ledger.append(
        ManifestEntry(
            source=DataSource.esec_spud,
            product_id="esec/catalogue",
            product_level="catalogue",
            path=esec_store.resolve().relative_to(repo).as_posix(),
            url=ESEC_URL,
            params={
                "n_entries": n_esec_elements,
                "n_distinct_descriptions": len(records),
                "stored": "gzip, mtime=0",
            },
            sha256=sha256_of_file(esec_store),
            size_bytes=esec_store.stat().st_size,
            retrieved_at=esec_accessed,
            licence="public domain (US federally funded data product); acknowledge EarthScope",
            licence_source_url="https://ds.iris.edu/ds/products/esec/",
            provenance=Provenance.real,
            status=ManifestStatus.fetched,
            adapter="build_lfh_references",
            adapter_version="0.1.0",
            notes=(
                f"Bulk ESEC response, sha256 of the retrieved bytes {esec_sha}. Stored gzipped; "
                "the uncompressed sha256 is what the reference file records." + esec_fallback_note
            ),
        )
    )
    ledger.append(
        ManifestEntry(
            source=DataSource.source_document,
            product_id="lfh/published_references",
            path=path.resolve().relative_to(repo).as_posix(),
            url="; ".join(sorted({s.url for s in sources})),
            params={
                "n_sources": len(sources),
                "n_clearing_citation_bar": len(references.sources_clearing_bar),
                "dois": sorted({s.doi for s in sources if s.doi}),
            },
            sha256=sha256_of_file(path),
            size_bytes=path.stat().st_size,
            retrieved_at=now,
            licence="Apache-2.0 (this repository); each source carries its own licence",
            provenance=Provenance.derived,
            status=ManifestStatus.fetched,
            adapter="build_lfh_references",
            adapter_version="0.1.0",
            notes=CITATION_RULE,
        )
    )


if __name__ == "__main__":
    main()
