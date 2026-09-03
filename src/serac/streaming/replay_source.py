"""Where replay chunks come from: a committed fixture directory or the in-code synthetic lane.

`FixtureReplaySource` reads `data/fixtures/seismic/<event>/manifest.json`, re-hashes every
listed file (a drifted fixture is refused, not replayed), and slices the MiniSEED into
`chunk_seconds` pieces in stream-time order. `SyntheticReplaySource` generates the
`synthetic-lp-burst` lane in code with `provenance=synthetic`; it is never written under
`data/` and its fixture reference is a `synthetic://` locator plus the hash of the generated
payloads.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from obspy import Stream, read

from serac.adapters.seismic.obspy_codec import slice_stream
from serac.adapters.storage.manifest_ledger import sha256_of_file
from serac.domain.replay import FixtureManifest, FixtureRef, TimeWindow
from serac.domain.seismic import SeismicTrace, TraceProvenance, TraceSource
from serac.errors import SeracError
from serac.streaming.synthetic import synthetic_lp_burst

SYNTHETIC_EVENT_ID = "synthetic-lp-burst"
SYNTHETIC_START_UTC = datetime(2000, 1, 1, 0, 0, tzinfo=UTC)
SYNTHETIC_BURST_START_S = 40.0
SYNTHETIC_N_CHUNKS = 24
SYNTHETIC_SEED = 7


class FixtureNotFetchedError(SeracError):
    """No committed fixture exists for the event (and `--online` was not given)."""


class FixtureIntegrityError(SeracError):
    """A fixture file does not match the sha256 in its manifest."""


@dataclass(frozen=True)
class StationInfo:
    """Channel coordinates read from the fixture's StationXML (or None when absent)."""

    sncl: str
    latitude: float | None
    longitude: float | None
    elevation_m: float | None


class ReplaySource(ABC):
    """A finite, ordered sequence of chunks plus what the report needs to describe them."""

    event_id: str
    contains_synthetic: bool

    @abstractmethod
    def chunks(self, *, chunk_seconds: float) -> Iterator[SeismicTrace]: ...

    @abstractmethod
    def fixture_refs(self) -> list[FixtureRef]: ...

    @abstractmethod
    def window(self) -> TimeWindow | None: ...

    @abstractmethod
    def stations(self) -> list[StationInfo]: ...

    def caveats(self) -> list[str]:
        return []


def fixture_dir_for(repo_root: Path, event_id: str) -> Path:
    return repo_root / "data" / "fixtures" / "seismic" / event_id


def load_fixture_manifest(fixture_dir: Path) -> FixtureManifest:
    path = fixture_dir / "manifest.json"
    if not path.exists():
        raise FixtureNotFetchedError(
            f"no replay fixture at {fixture_dir} (missing manifest.json); pass --online to "
            "fetch from FDSN, or commit a fixture"
        )
    manifest = FixtureManifest.model_validate_json(path.read_text(encoding="utf-8"))
    if manifest.status == "not_fetched":
        raise FixtureNotFetchedError(f"{path}: status is not_fetched")
    return manifest


def verify_fixture(fixture_dir: Path, manifest: FixtureManifest) -> list[FixtureRef]:
    """Re-hash every listed file; raise on any drift."""
    refs: list[FixtureRef] = []
    for file in manifest.files:
        path = fixture_dir / file.path
        if not path.exists():
            raise FixtureIntegrityError(f"{path} listed in manifest.json but missing")
        digest = sha256_of_file(path)
        if digest != file.sha256:
            raise FixtureIntegrityError(f"{path}: sha256 {digest} != manifest {file.sha256}")
        refs.append(FixtureRef(path=path.as_posix(), sha256=digest, provenance="real"))
    return refs


class FixtureReplaySource(ReplaySource):
    """Chunks from a committed, hash-verified fixture directory."""

    contains_synthetic = False

    def __init__(self, fixture_dir: Path, *, repo_root: Path | None = None) -> None:
        self.fixture_dir = fixture_dir
        self.repo_root = repo_root
        self.manifest = load_fixture_manifest(fixture_dir)
        self.event_id = self.manifest.event_id
        self._refs = verify_fixture(fixture_dir, self.manifest)

    def _rel(self, path: str) -> str:
        if self.repo_root is not None:
            try:
                return Path(path).resolve().relative_to(self.repo_root.resolve()).as_posix()
            except ValueError:
                pass
        return path

    def fixture_refs(self) -> list[FixtureRef]:
        return [r.model_copy(update={"path": self._rel(r.path)}) for r in self._refs]

    def window(self) -> TimeWindow | None:
        return self.manifest.window

    def _provenance(self, file_path: str) -> TraceProvenance:
        return TraceProvenance(
            source=TraceSource.fixture,
            server=self.manifest.request.base_url,
            retrieved_at=self.manifest.retrieved_at_utc,
            licence=self.manifest.licence,
            licence_source_url=self.manifest.licence_source_url,
            fixture_path=self._rel((self.fixture_dir / file_path).as_posix()),
        )

    def stream(self) -> Stream:
        stream = Stream()
        for file in self.manifest.files:
            if file.kind == "miniseed":
                stream += read(str(self.fixture_dir / file.path), format="MSEED")
        return stream

    def chunks(self, *, chunk_seconds: float) -> Iterator[SeismicTrace]:
        # Provenance carries the directory-level licence; the per-file path is recorded on
        # each chunk so a consumer can trace bytes back to the manifest row.
        by_key = {f.sncl: f.path for f in self.manifest.files if f.kind == "miniseed" and f.sncl}
        pieces: list[SeismicTrace] = []
        for file_path in sorted(set(by_key.values())):
            stream = read(str(self.fixture_dir / file_path), format="MSEED")
            pieces.extend(
                slice_stream(
                    stream, chunk_seconds=chunk_seconds, provenance=self._provenance(file_path)
                )
            )
        pieces.sort(key=lambda c: (c.start_time_utc, c.sncl.key, c.sequence))
        yield from pieces

    def stations(self) -> list[StationInfo]:
        xml = [f for f in self.manifest.files if f.kind == "stationxml"]
        keys = [f.sncl for f in self.manifest.files if f.kind == "miniseed" and f.sncl]
        if not xml:
            return [StationInfo(k, None, None, None) for k in keys]
        from serac.adapters.seismic.fdsn import load_inventory, stations_from_inventory

        inventory = load_inventory(self.fixture_dir / xml[0].path)
        refs = {
            s.sncl.key: s
            for s in stations_from_inventory(inventory, data_centre=self.manifest.request.base_url)
        }
        out: list[StationInfo] = []
        for key in keys:
            ref = refs.get(key)
            out.append(
                StationInfo(key, ref.latitude, ref.longitude, ref.elevation_m)
                if ref
                else StationInfo(key, None, None, None)
            )
        return out

    def caveats(self) -> list[str]:
        return [
            f"Waveforms are the committed fixture {self.fixture_dir.name} (status "
            f"{self.manifest.status}); licence {self.manifest.licence!r}, see "
            f"{self.manifest.licence_source_url}."
        ]


class SyntheticReplaySource(ReplaySource):
    """The in-code `synthetic-lp-burst` lane: labelled synthetic, never stored under data/."""

    event_id = SYNTHETIC_EVENT_ID
    contains_synthetic = True

    def __init__(
        self,
        *,
        start_utc: datetime = SYNTHETIC_START_UTC,
        n_chunks: int = SYNTHETIC_N_CHUNKS,
        burst_start_s: float = SYNTHETIC_BURST_START_S,
        seed: int = SYNTHETIC_SEED,
    ) -> None:
        self.start_utc = start_utc
        self.n_chunks = n_chunks
        self.burst_start_s = burst_start_s
        self.seed = seed
        self._chunks: list[SeismicTrace] | None = None
        self._chunk_seconds: float | None = None

    @property
    def origin_time_utc(self) -> datetime:
        """The burst onset is known by construction; it plays the role of an origin time."""
        return self.start_utc + timedelta(seconds=self.burst_start_s)

    def _generate(self, chunk_seconds: float) -> list[SeismicTrace]:
        if self._chunks is None or self._chunk_seconds != chunk_seconds:
            self._chunks = list(
                synthetic_lp_burst(
                    start_utc=self.start_utc,
                    n_chunks=self.n_chunks,
                    chunk_seconds=chunk_seconds,
                    burst_start_s=self.burst_start_s,
                    seed=self.seed,
                )
            )
            self._chunk_seconds = chunk_seconds
        return self._chunks

    def chunks(self, *, chunk_seconds: float) -> Iterator[SeismicTrace]:
        yield from self._generate(chunk_seconds)

    def fixture_refs(self) -> list[FixtureRef]:
        chunks = self._generate(self._chunk_seconds or 5.0)
        digest = hashlib.sha256(b"".join(c.data for c in chunks)).hexdigest()
        locator = (
            f"synthetic://serac.streaming.synthetic.synthetic_lp_burst?seed={self.seed}"
            f"&n_chunks={self.n_chunks}&burst_start_s={self.burst_start_s}"
        )
        return [FixtureRef(path=locator, sha256=digest, provenance="synthetic")]

    def window(self) -> TimeWindow | None:
        chunks = self._generate(self._chunk_seconds or 5.0)
        return TimeWindow(start_utc=chunks[0].start_time_utc, end_utc=chunks[-1].end_time_utc)

    def stations(self) -> list[StationInfo]:
        chunks = self._generate(self._chunk_seconds or 5.0)
        return [StationInfo(chunks[0].sncl.key, None, None, None)]

    def caveats(self) -> list[str]:
        return [
            "SYNTHETIC lane: the waveform is Gaussian noise plus a Hann-windowed 20 s-period "
            "sinusoid generated in code (provenance=synthetic). It is not an observation and "
            "proves plumbing only; the origin time is the burst onset parameter."
        ]
