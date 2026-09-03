"""The source-fetch protocol behind `serac sources fetch`.

A `SourceRef` may enter an event record only after the document was actually retrieved in
the session that wrote it. This module does the retrieval honestly and leaves a trail:

1. GET the URL with a timeout and an identifying user agent, following redirects;
2. hash the bytes exactly as received and note the final URL and content type;
3. if a DOI was given, resolve it through the Crossref API in the same session and take the
   bibliographic metadata from the resolved record (an unresolved DOI is refused, never
   written);
4. optionally store the bytes under `data/raw/sources/<event_id>/` (only when the licence
   allows; paywalled documents are cited, never stored);
5. append a `ManifestEntry(source=source_document)` to the provenance ledger, `fetched` when
   a copy was stored and `listed` when the page was only read;
6. emit the `SourceRef` for the record, and with `apply=True` insert it into
   `data/events/<event_id>.json`.

The HTTP client is a `Protocol` so the pipeline is unit-tested offline against a fake.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from serac import __version__
from serac.domain.common import SourceKind, SourceRef
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.errors import SeracError
from serac.ports.ledger import ManifestLedger

ADAPTER_NAME = "serac sources fetch"
USER_AGENT = f"serac-sources-fetch/{__version__} (+https://github.com/dizzy1900/serac)"
DEFAULT_TIMEOUT_S = 60.0
CROSSREF_API = "https://api.crossref.org/works/"
SOURCES_DIR = Path("data") / "raw" / "sources"
EVENTS_DIR = Path("data") / "events"

_EXTENSIONS: dict[str, str] = {
    "text/html": "html",
    "application/xhtml+xml": "html",
    "application/pdf": "pdf",
    "application/json": "json",
    "application/geo+json": "geojson",
    "text/plain": "txt",
    "application/xml": "xml",
    "text/xml": "xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}


class SourceFetchError(SeracError):
    """The retrieval, DOI resolution or record update could not be completed honestly."""


@dataclass(frozen=True)
class HttpResponse:
    """What an HTTP GET actually returned."""

    status_code: int
    final_url: str
    content_type: str
    content: bytes


class HttpClient(Protocol):
    """Minimal GET interface; `HttpxClient` is the real one, tests pass a fake."""

    def get(self, url: str, *, timeout: float) -> HttpResponse: ...


class HttpxClient:
    """`httpx`-backed client following redirects with an identifying user agent."""

    def __init__(self, user_agent: str = USER_AGENT) -> None:
        self.user_agent = user_agent

    def get(self, url: str, *, timeout: float) -> HttpResponse:
        import httpx

        headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
            response = client.get(url)
        return HttpResponse(
            status_code=response.status_code,
            final_url=str(response.url),
            content_type=response.headers.get("content-type", "application/octet-stream"),
            content=response.content,
        )


@dataclass(frozen=True)
class FetchRequest:
    """Everything `serac sources fetch` needs to know about one retrieval."""

    url: str
    event_id: str
    source_id: str
    kind: SourceKind
    licence: str
    claims: tuple[str, ...]
    doi: str | None = None
    store: bool = False
    title: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    publisher: str | None = None
    excerpt: str | None = None
    licence_source_url: str | None = None
    notes: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class CrossrefRecord:
    """The bibliographic fields taken from a resolved Crossref work record."""

    doi: str
    title: str | None
    authors: tuple[str, ...]
    year: int | None
    publisher: str | None
    container_title: str | None
    url: str
    sha256: str


@dataclass(frozen=True)
class FetchOutcome:
    """The result of one honest retrieval."""

    source: SourceRef
    entry: ManifestEntry
    response: HttpResponse
    stored_path: Path | None
    crossref: CrossrefRecord | None
    applied_to: Path | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


# --- helpers ----------------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def media_type(content_type: str) -> str:
    """`text/html; charset=utf-8` -> `text/html`."""
    return content_type.split(";", 1)[0].strip().lower() or "application/octet-stream"


def extension_for(content_type: str) -> str:
    return _EXTENSIONS.get(media_type(content_type), "bin")


_TITLE_META = re.compile(
    r'<meta\s+[^>]*?(?:name|property)\s*=\s*["\'](?:citation_title|dc\.title|og:title)["\']'
    r'[^>]*?content\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE | re.DOTALL,
)
_TITLE_META_REVERSED = re.compile(
    r'<meta\s+[^>]*?content\s*=\s*["\']([^"\']+)["\'][^>]*?(?:name|property)\s*=\s*'
    r'["\'](?:citation_title|dc\.title|og:title)["\']',
    re.IGNORECASE | re.DOTALL,
)
_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_WS = re.compile(r"\s+")


def _unescape(text: str) -> str:
    import html

    return _WS.sub(" ", html.unescape(text)).strip()


def extract_title(content: bytes, content_type: str) -> str | None:
    """Best-effort title from an HTML page or a Crossref/JSON document; None otherwise."""
    kind = media_type(content_type)
    if kind in ("text/html", "application/xhtml+xml"):
        text = content.decode("utf-8", errors="replace")
        for pattern in (_TITLE_META, _TITLE_META_REVERSED, _TITLE_TAG):
            match = pattern.search(text)
            if match:
                title = _unescape(match.group(1))
                if title:
                    return title
        return None
    if kind == "application/json":
        try:
            doc = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        message = doc.get("message", doc) if isinstance(doc, dict) else None
        if isinstance(message, dict):
            titles = message.get("title")
            if isinstance(titles, list) and titles and isinstance(titles[0], str):
                return _unescape(titles[0])
            if isinstance(titles, str):
                return _unescape(titles)
    return None


def _crossref_year(message: dict[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        parts = (
            message.get(key, {}).get("date-parts") if isinstance(message.get(key), dict) else None
        )
        if parts and parts[0] and isinstance(parts[0][0], int):
            return int(parts[0][0])
    return None


def parse_crossref(doi: str, response: HttpResponse) -> CrossrefRecord:
    """Turn a Crossref `works/<doi>` response into a `CrossrefRecord`; raise if it is not one."""
    if response.status_code != 200:
        raise SourceFetchError(
            f"DOI {doi} did not resolve via Crossref (HTTP {response.status_code})"
        )
    try:
        doc = json.loads(response.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceFetchError(f"DOI {doi}: Crossref returned non-JSON") from exc
    message = doc.get("message") if isinstance(doc, dict) else None
    if not isinstance(message, dict) or doc.get("status") != "ok":
        raise SourceFetchError(f"DOI {doi}: Crossref response has no work record")
    resolved = str(message.get("DOI", "")).lower()
    if resolved != doi.lower():
        raise SourceFetchError(f"DOI {doi}: Crossref resolved a different DOI {resolved!r}")
    titles = message.get("title") or []
    authors: list[str] = []
    for person in message.get("author") or []:
        if not isinstance(person, dict):
            continue
        family = person.get("family")
        given = person.get("given")
        name = person.get("name")
        if family:
            authors.append(f"{family}, {given}" if given else str(family))
        elif name:
            authors.append(str(name))
    containers = message.get("container-title") or []
    return CrossrefRecord(
        doi=str(message.get("DOI")),
        title=_unescape(titles[0]) if titles else None,
        authors=tuple(authors),
        year=_crossref_year(message),
        publisher=message.get("publisher"),
        container_title=containers[0] if containers else None,
        url=response.final_url,
        sha256=sha256_bytes(response.content),
    )


def resolve_doi(client: HttpClient, doi: str, timeout: float = DEFAULT_TIMEOUT_S) -> CrossrefRecord:
    """Resolve `doi` through the Crossref API right now, or raise `SourceFetchError`."""
    response = client.get(f"{CROSSREF_API}{doi}", timeout=timeout)
    return parse_crossref(doi, response)


# --- the protocol -----------------------------------------------------------------------------


def _write_stored_copy(repo: Path, request: FetchRequest, response: HttpResponse) -> Path:
    directory = repo / SOURCES_DIR / request.event_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{request.source_id}.{extension_for(response.content_type)}"
    path.write_bytes(response.content)
    return path


def fetch_source(
    client: HttpClient,
    request: FetchRequest,
    ledger: ManifestLedger,
    repo: Path,
    *,
    now: datetime | None = None,
    apply: bool = False,
) -> FetchOutcome:
    """Run the fetch protocol for one document. Raises `SourceFetchError` on any dishonesty."""
    accessed = now or datetime.now(tz=UTC)
    crossref: CrossrefRecord | None = None
    warnings: list[str] = []
    if request.doi is not None:
        crossref = resolve_doi(client, request.doi, request.timeout_s)

    response = client.get(request.url, timeout=request.timeout_s)
    if response.status_code != 200:
        raise SourceFetchError(f"GET {request.url} returned HTTP {response.status_code}")
    if not response.content:
        raise SourceFetchError(f"GET {request.url} returned no bytes")
    digest = sha256_bytes(response.content)

    title = request.title or (crossref.title if crossref else None)
    if title is None:
        title = extract_title(response.content, response.content_type)
    if not title:
        raise SourceFetchError(
            f"{request.source_id}: no title could be extracted from {response.final_url}; "
            "pass --title"
        )
    authors = tuple(request.authors) or (crossref.authors if crossref else ())
    year = request.year if request.year is not None else (crossref.year if crossref else None)
    publisher = request.publisher or (crossref.publisher if crossref else None)
    if request.excerpt and request.excerpt not in response.content.decode(
        "utf-8", errors="replace"
    ):
        warnings.append(
            "excerpt text was not found verbatim in the retrieved bytes (PDF or dynamic page?)"
        )

    stored_path: Path | None = None
    stored_copy: str | None = None
    if request.store:
        stored_path = _write_stored_copy(repo, request, response)
        stored_copy = stored_path.relative_to(repo).as_posix()

    source = SourceRef(
        id=request.source_id,
        kind=request.kind,
        title=title,
        url=response.final_url,
        doi=crossref.doi if crossref else None,
        authors=list(authors),
        year=year,
        publisher=publisher,
        accessed_utc=accessed,
        sha256=digest,
        content_type=response.content_type,
        licence=request.licence,
        stored_copy=stored_copy,
        claims_supported=list(request.claims),
        excerpt=request.excerpt,
        peer_reviewed=request.kind == SourceKind.peer_reviewed,
    )
    params: dict[str, Any] = {"requested_url": request.url, "kind": request.kind.value}
    if crossref is not None:
        params["doi"] = crossref.doi
        params["crossref_url"] = crossref.url
        params["crossref_sha256"] = crossref.sha256
        if crossref.container_title:
            params["container_title"] = crossref.container_title
    entry = ManifestEntry(
        source=DataSource.source_document,
        product_id=f"{request.event_id}/{request.source_id}",
        event_id=request.event_id,
        path=stored_copy,
        url=response.final_url,
        params=params,
        sha256=digest,
        size_bytes=len(response.content),
        retrieved_at=accessed,
        licence=request.licence,
        licence_source_url=request.licence_source_url or response.final_url,
        provenance=Provenance.real,
        status=ManifestStatus.fetched if stored_copy else ManifestStatus.listed,
        adapter=ADAPTER_NAME,
        adapter_version=__version__,
        notes=request.notes or (f"title: {title}" if not request.store else None),
    )
    ledger.append(entry)

    applied_to: Path | None = None
    if apply:
        applied_to = apply_source(repo, request.event_id, source)
    return FetchOutcome(
        source=source,
        entry=entry,
        response=response,
        stored_path=stored_path,
        crossref=crossref,
        applied_to=applied_to,
        warnings=tuple(warnings),
    )


def dump_record(record: dict[str, Any]) -> str:
    """Canonical serialisation of an event record: sorted keys, two-space indent."""
    return json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def apply_source(repo: Path, event_id: str, source: SourceRef) -> Path:
    """Insert (or replace by id) `source` in `data/events/<event_id>.json` `sources[]`."""
    path = repo / EVENTS_DIR / f"{event_id}.json"
    if not path.exists():
        raise SourceFetchError(f"{path} does not exist; create the record first")
    record = json.loads(path.read_text(encoding="utf-8"))
    sources = [s for s in record.get("sources", []) if s.get("id") != source.id]
    sources.append(json.loads(source.model_dump_json(exclude_none=False)))
    record["sources"] = sources
    path.write_text(dump_record(record), encoding="utf-8")
    return path
