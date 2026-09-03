"""`serac sources fetch` protocol against a fake HTTP client. No network, nothing real."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.domain.common import SourceKind
from serac.domain.manifest import DataSource, ManifestStatus
from serac.pipelines.sources import (
    FetchRequest,
    HttpResponse,
    SourceFetchError,
    apply_source,
    dump_record,
    extension_for,
    extract_title,
    fetch_source,
    media_type,
    parse_crossref,
    sha256_bytes,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
FAKE_DOI = "10.99999/fictional.1"
FAKE_URL = "https://example.invalid/paper"
HTML = b"<html><head><meta name='citation_title' content='A fictional &amp; test paper'>"
HTML += b"<title>site title</title></head><body>volume was 42 units</body></html>"
CROSSREF = {
    "status": "ok",
    "message": {
        "DOI": FAKE_DOI,
        "title": ["A fictional & test paper"],
        "author": [{"family": "Doe", "given": "J."}, {"name": "Consortium"}],
        "published-online": {"date-parts": [[2020, 1, 1]]},
        "publisher": "Fictional Press",
        "container-title": ["Journal of Nothing"],
    },
}


class FakeHttp:
    """Serves canned responses by URL and records what was requested."""

    def __init__(self, responses: dict[str, HttpResponse]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def get(self, url: str, *, timeout: float) -> HttpResponse:
        self.requests.append(url)
        try:
            return self.responses[url]
        except KeyError:
            return HttpResponse(404, url, "text/plain", b"not found")


def _html(final: str = FAKE_URL, body: bytes = HTML) -> HttpResponse:
    return HttpResponse(200, final, "text/html; charset=utf-8", body)


def _crossref_response(doc: object = CROSSREF) -> HttpResponse:
    return HttpResponse(
        200,
        f"https://api.crossref.org/works/{FAKE_DOI}",
        "application/json",
        json.dumps(doc).encode(),
    )


def _request(**overrides: object) -> FetchRequest:
    data: dict[str, object] = {
        "url": FAKE_URL,
        "event_id": "test-event-1",
        "source_id": "test-src-1",
        "kind": SourceKind.peer_reviewed,
        "licence": "CC-BY-4.0",
        "claims": ("fall_height_m",),
    }
    data.update(overrides)
    return FetchRequest(**data)  # type: ignore[arg-type]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "data" / "events").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def ledger(repo: Path) -> JsonlManifestLedger:
    return JsonlManifestLedger(repo / "data" / "manifest.jsonl")


def test_helpers() -> None:
    assert media_type("text/html; charset=utf-8") == "text/html"
    assert media_type("") == "application/octet-stream"
    assert extension_for("application/pdf") == "pdf"
    assert extension_for("application/x-unknown") == "bin"
    assert extract_title(HTML, "text/html") == "A fictional & test paper"
    assert extract_title(b"<title> only \n title </title>", "text/html") == "only title"
    assert extract_title(b"no title here", "text/html") is None
    assert extract_title(json.dumps(CROSSREF).encode(), "application/json") == (
        "A fictional & test paper"
    )
    assert extract_title(b"%PDF-1.4", "application/pdf") is None
    assert sha256_bytes(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_listed_fetch_without_store(repo: Path, ledger: JsonlManifestLedger) -> None:
    http = FakeHttp({FAKE_URL: _html(final="https://example.invalid/paper/landing")})
    outcome = fetch_source(http, _request(excerpt="volume was 42 units"), ledger, repo, now=NOW)
    src = outcome.source
    assert src.id == "test-src-1"
    assert src.title == "A fictional & test paper"
    assert src.url == "https://example.invalid/paper/landing", "final URL after redirects"
    assert src.sha256 == sha256_bytes(HTML)
    assert src.content_type == "text/html; charset=utf-8"
    assert src.accessed_utc == NOW
    assert src.doi is None
    assert src.stored_copy is None
    assert src.peer_reviewed is True
    assert outcome.warnings == ()
    assert outcome.stored_path is None
    rows = list(ledger.entries())
    assert len(rows) == 1
    row = rows[0]
    assert row.source == DataSource.source_document
    assert row.status == ManifestStatus.listed
    assert row.event_id == "test-event-1"
    assert row.product_id == "test-event-1/test-src-1"
    assert row.sha256 == src.sha256
    assert row.size_bytes == len(HTML)
    assert row.path is None
    assert row.adapter == "serac sources fetch"
    assert row.licence_source_url == src.url
    assert (
        not list((repo / "data" / "raw").glob("**/*")) if (repo / "data" / "raw").exists() else True
    )


def test_store_writes_copy_and_fetched_row(repo: Path, ledger: JsonlManifestLedger) -> None:
    http = FakeHttp({FAKE_URL: _html()})
    outcome = fetch_source(http, _request(store=True), ledger, repo, now=NOW)
    assert outcome.source.stored_copy == "data/raw/sources/test-event-1/test-src-1.html"
    assert outcome.stored_path is not None
    assert outcome.stored_path.read_bytes() == HTML
    row = next(iter(ledger.entries()))
    assert row.status == ManifestStatus.fetched
    assert row.path == outcome.source.stored_copy


def test_doi_is_resolved_via_crossref_first(repo: Path, ledger: JsonlManifestLedger) -> None:
    http = FakeHttp(
        {
            FAKE_URL: _html(body=b"<html><body>no title</body></html>"),
            f"https://api.crossref.org/works/{FAKE_DOI}": _crossref_response(),
        }
    )
    outcome = fetch_source(http, _request(doi=FAKE_DOI), ledger, repo, now=NOW)
    assert http.requests[0].startswith("https://api.crossref.org/works/")
    assert outcome.crossref is not None
    assert outcome.source.doi == FAKE_DOI
    assert outcome.source.title == "A fictional & test paper", "title taken from Crossref"
    assert outcome.source.authors == ["Doe, J.", "Consortium"]
    assert outcome.source.year == 2020
    assert outcome.source.publisher == "Fictional Press"
    row = next(iter(ledger.entries()))
    assert row.params["doi"] == FAKE_DOI
    assert row.params["crossref_sha256"] == outcome.crossref.sha256
    assert row.params["container_title"] == "Journal of Nothing"


def test_unresolved_doi_is_refused_and_nothing_is_ledgered(
    repo: Path, ledger: JsonlManifestLedger
) -> None:
    http = FakeHttp({FAKE_URL: _html()})
    with pytest.raises(SourceFetchError, match="did not resolve"):
        fetch_source(http, _request(doi=FAKE_DOI), ledger, repo, now=NOW)
    assert list(ledger.entries()) == []
    assert http.requests == [f"https://api.crossref.org/works/{FAKE_DOI}"]


def test_crossref_must_return_the_same_doi() -> None:
    other = json.loads(json.dumps(CROSSREF))
    other["message"]["DOI"] = "10.99999/other"
    with pytest.raises(SourceFetchError, match="different DOI"):
        parse_crossref(FAKE_DOI, _crossref_response(other))
    with pytest.raises(SourceFetchError, match="no work record"):
        parse_crossref(FAKE_DOI, _crossref_response({"status": "error"}))
    with pytest.raises(SourceFetchError, match="non-JSON"):
        parse_crossref(FAKE_DOI, HttpResponse(200, "u", "application/json", b"{"))


def test_http_error_and_empty_body_are_refused(repo: Path, ledger: JsonlManifestLedger) -> None:
    http = FakeHttp({})
    with pytest.raises(SourceFetchError, match="HTTP 404"):
        fetch_source(http, _request(), ledger, repo, now=NOW)
    http = FakeHttp({FAKE_URL: HttpResponse(200, FAKE_URL, "text/html", b"")})
    with pytest.raises(SourceFetchError, match="no bytes"):
        fetch_source(http, _request(), ledger, repo, now=NOW)
    assert list(ledger.entries()) == []


def test_missing_title_needs_override(repo: Path, ledger: JsonlManifestLedger) -> None:
    http = FakeHttp({FAKE_URL: HttpResponse(200, FAKE_URL, "application/pdf", b"%PDF-1.4")})
    with pytest.raises(SourceFetchError, match="pass --title"):
        fetch_source(http, _request(), ledger, repo, now=NOW)
    outcome = fetch_source(http, _request(title="Given title"), ledger, repo, now=NOW)
    assert outcome.source.title == "Given title"
    assert outcome.source.content_type == "application/pdf"


def test_excerpt_not_in_bytes_is_a_warning_not_an_error(
    repo: Path, ledger: JsonlManifestLedger
) -> None:
    http = FakeHttp({FAKE_URL: _html()})
    outcome = fetch_source(http, _request(excerpt="text that is not there"), ledger, repo, now=NOW)
    assert outcome.warnings and "not found verbatim" in outcome.warnings[0]


def test_apply_inserts_and_replaces_by_id(repo: Path, ledger: JsonlManifestLedger) -> None:
    record_path = repo / "data" / "events" / "test-event-1.json"
    record_path.write_text(dump_record({"event_id": "test-event-1", "sources": []}))
    http = FakeHttp({FAKE_URL: _html()})
    outcome = fetch_source(http, _request(), ledger, repo, now=NOW, apply=True)
    assert outcome.applied_to == record_path
    doc = json.loads(record_path.read_text())
    assert [s["id"] for s in doc["sources"]] == ["test-src-1"]
    assert doc["sources"][0]["sha256"] == outcome.source.sha256
    # A second fetch with the same id replaces, not duplicates.
    apply_source(repo, "test-event-1", outcome.source)
    doc = json.loads(record_path.read_text())
    assert [s["id"] for s in doc["sources"]] == ["test-src-1"]
    text = record_path.read_text()
    assert text == dump_record(json.loads(text)), "canonical form: sorted keys, 2-space indent"
    with pytest.raises(SourceFetchError, match="does not exist"):
        apply_source(repo, "nope", outcome.source)


def test_press_report_is_not_peer_reviewed(repo: Path, ledger: JsonlManifestLedger) -> None:
    http = FakeHttp({FAKE_URL: _html()})
    outcome = fetch_source(
        http,
        _request(kind=SourceKind.press_report, licence="all-rights-reserved; cited-only"),
        ledger,
        repo,
        now=NOW,
    )
    assert outcome.source.peer_reviewed is False
    assert outcome.source.kind == SourceKind.press_report
