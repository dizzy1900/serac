"""`serac lfh`: build the Green's library, invert an event, seal the config, write reports.

Wire into `serac.cli` with:

    app.add_typer(cli_lfh.app, name="lfh", help="Landslide force-history inversion (M2).")

The order the commands are meant to be run in is the order the anti-tuning rule needs:
`greens build` (or rely on the committed fixture subset), then `invert` on the published
reproductions, then `seal`, and only then `invert` on Langtang and Blatten. `validate lfh`
checks that the seal was respected.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import typer

from serac.adapters.seismic.syngine import (
    LICENCE as GREENS_LICENCE,
)
from serac.adapters.seismic.syngine import (
    LICENCE_NOTE as GREENS_LICENCE_NOTE,
)
from serac.adapters.seismic.syngine import (
    LICENCE_SOURCE_URL as GREENS_LICENCE_URL,
)
from serac.adapters.seismic.syngine import (
    PROVIDER_URL,
    SyngineGreensLibrary,
    distance_library,
    nearest_request,
)
from serac.adapters.storage.manifest_ledger import JsonlManifestLedger, sha256_of_file
from serac.domain.manifest import DataSource, ManifestEntry, ManifestStatus, Provenance
from serac.models.lfh.config import LfhConfig, read_seal, seal_config, write_seal
from serac.models.lfh.gsf import build_grid
from serac.models.lfh.pipeline import invert_event
from serac.models.lfh.references import LfhTarget, load_references
from serac.models.lfh.report import write_event_report, write_run_json
from serac.ports.greens import GreensRequest
from serac.validation.result import git_sha

app = typer.Typer(name="lfh", help="Landslide force-history inversion (M2).", no_args_is_help=True)
greens_app = typer.Typer(name="greens", help="Modelled Green's functions.", no_args_is_help=True)
app.add_typer(greens_app, name="greens")

REPO_OPTION = typer.Option(Path("."), "--repo", help="Repository root.")
CACHE_OPTION = typer.Option(
    None, "--cache", help="Green's cache root; defaults to data/interim/greens."
)
OFFLINE_OPTION = typer.Option(
    False, "--offline", help="Refuse to fetch; fail if a Green's function is not cached."
)
REPORTS_OPTION = typer.Option(Path("reports/m2"), "--reports-dir", help="Where reports go.")
REPORT_FLAG = typer.Option(True, "--report/--no-report")
WARM_FLAG = typer.Option(
    False, "--warm", help="Reuse cached response removal (measures warm, not cold, latency)."
)
TARGET_OPTION = typer.Option(None, "--target", help="Restrict to one target id.")
WORKERS_OPTION = typer.Option(4, "--workers", min=1, max=8)
FULL_OPTION = typer.Option(False, "--full", help="Plan the whole 0.5-15 deg distance library.")
REPRODUCTION_OPTION = typer.Option([], "--reproduction", help="Target ids validated here.")
FORCE_OPTION = typer.Option(False, "--force", help="Overwrite an existing seal.")
ROLE_OPTION = typer.Option("all", "--role", help="reproduction | new_event | all")
TARGET_ARGUMENT = typer.Argument(..., help="Target id from data/references/lfh_published.json")
FIXTURE_CACHE = Path("data/fixtures/greens/lfh")
INTERIM_CACHE = Path("data/interim/greens")
PREPARED_CACHE = Path("data/interim/lfh/prepared")
OUT_OPTION = typer.Option(FIXTURE_CACHE, "--out")


def _cache_root(repo: Path, cache: Path | None) -> Path:
    if cache is not None:
        return cache
    fixtures = repo / FIXTURE_CACHE
    return fixtures if fixtures.exists() else repo / INTERIM_CACHE


def _library(repo: Path, cache: Path | None, *, offline: bool) -> SyngineGreensLibrary:
    return SyngineGreensLibrary(_cache_root(repo, cache), repo_root=repo, allow_network=not offline)


def _requests_for_target(target: LfhTarget, config: LfhConfig, repo: Path) -> list[GreensRequest]:
    """Every (distance, depth) the inversion of one target will ask for.

    Enumerated from the station geometry and the trial grid rather than guessed, so
    `greens build` fetches exactly what `invert` needs and `--offline` then works.
    """
    from serac.adapters.seismic.syngine import geocentric_distance_azimuth
    from serac.models.lfh.waveforms import prepare_channels, read_event_waveforms, select_channels

    stream, inventory = read_event_waveforms(repo / target.fixture_dir)
    prepared, _ = prepare_channels(
        stream,
        inventory,
        origin_utc=target.origin_utc,
        source_lat=target.source_latitude,
        source_lon=target.source_longitude,
        config=config,
    )
    channels, _ = select_channels(prepared, config)
    depths = sorted({*config.grid.depths_m, *config.bootstrap.depths_m})
    nodes = build_grid(target.source_latitude, target.source_longitude, config)
    seen: dict[str, GreensRequest] = {}
    for channel in channels:
        for node in nodes:
            distance, _ = geocentric_distance_azimuth(
                node.latitude, node.longitude, channel.latitude, channel.longitude
            )
            for depth in depths:
                request = nearest_request(
                    distance,
                    model=config.earth_model,
                    source_depth_m=depth,
                    dt_s=config.dt_s,
                    duration_s=config.greens_duration_s,
                    step_deg=config.greens_step_deg,
                    min_deg=config.stations.min_distance_deg,
                    max_deg=config.stations.max_distance_deg,
                )
                seen.setdefault(request.cache_key(), request)
    return list(seen.values())


@greens_app.command("plan")
def greens_plan(
    repo: Path = REPO_OPTION,
    cache: Path | None = CACHE_OPTION,
    target: str | None = TARGET_OPTION,
    full: bool = FULL_OPTION,
) -> None:
    """Say what would be fetched. Writes nothing -- not the cache, not the ledger."""
    config = LfhConfig()
    library = _library(repo, cache, offline=True)
    requests: list[GreensRequest]
    if full:
        requests = distance_library(
            min_deg=config.stations.min_distance_deg,
            max_deg=config.stations.max_distance_deg,
            step_deg=config.greens_step_deg,
            model=config.earth_model,
            dt_s=config.dt_s,
            duration_s=config.greens_duration_s,
        )
    else:
        references = load_references(repo)
        targets = [references.target(target)] if target else references.targets
        requests = []
        for item in targets:
            requests.extend(_requests_for_target(item, config, repo))
    plan = library.plan(requests)
    typer.echo(
        f"{len(plan.requests)} distinct (distance, depth) pairs: "
        f"{plan.cached} cached, {plan.to_fetch} to fetch"
    )
    typer.echo(f"estimated {plan.estimated_bytes / 1024:.0f} kB from {plan.provider_url}")
    typer.echo(f"basis: {plan.estimate_basis}")
    typer.echo(f"requests: {2 * plan.to_fetch} Syngine calls (two per pair)")


@greens_app.command("build")
def greens_build(
    repo: Path = REPO_OPTION,
    cache: Path | None = CACHE_OPTION,
    target: str | None = TARGET_OPTION,
    workers: int = WORKERS_OPTION,
) -> None:
    """Fetch and cache everything the configured targets need."""
    config = LfhConfig()
    references = load_references(repo)
    targets = [references.target(target)] if target else references.targets
    library = _library(repo, cache, offline=False)
    ledger = JsonlManifestLedger(repo / "data" / "manifest.jsonl")

    requests: list[GreensRequest] = []
    seen: set[str] = set()
    for item in targets:
        for request in _requests_for_target(item, config, repo):
            key = request.cache_key()
            if key not in seen:
                seen.add(key)
                requests.append(request)
    plan = library.plan(requests)
    typer.echo(f"{plan.cached} cached, {plan.to_fetch} to fetch ({2 * plan.to_fetch} calls)")
    if plan.to_fetch == 0:
        return

    started = time.perf_counter()
    done = 0
    lock_ledger = JsonlManifestLedger(repo / "data" / "manifest.jsonl")

    def _fetch(request: GreensRequest) -> str:
        library.get(request, lock_ledger)
        return request.cache_key()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(_fetch, requests):
            done += 1
            if done % 20 == 0:
                elapsed = time.perf_counter() - started
                typer.echo(f"  {done}/{len(requests)} in {elapsed:.0f} s")
    typer.echo(
        f"built {len(requests)} sets in {time.perf_counter() - started:.0f} s "
        f"into {_cache_root(repo, cache)}"
    )
    _ = ledger


def run_inversion(
    target_id: str,
    *,
    repo: Path,
    cache: Path | None = None,
    offline: bool = False,
    reports_dir: Path = Path("reports/m2"),
    write_report: bool = True,
    warm: bool = False,
) -> Path:
    """Invert one event, write its artefacts and return the run JSON path.

    Kept separate from the typer command so other commands can call it: invoking a typer
    command as a plain function passes `OptionInfo` sentinels rather than defaults.
    """
    config = LfhConfig()
    references = load_references(repo)
    target = references.target(target_id)
    library = _library(repo, cache, offline=offline)
    ledger = JsonlManifestLedger(repo / "data" / "manifest.jsonl")

    started = time.perf_counter()
    run = invert_event(
        target,
        repo=repo,
        library=library,
        ledger=ledger,
        config=config,
        prepared_cache_dir=(repo / PREPARED_CACHE) if warm else None,
    )
    wall_s = time.perf_counter() - started
    library.close()

    path = write_run_json(run, repo / reports_dir, wall_s=wall_s, config_hash=config.config_hash())
    typer.echo(f"status: {run.force_history.status}")
    if run.refused:
        typer.echo(run.force_history.notes[:400])
    else:
        history = run.force_history
        assert history.peak_force_n is not None and history.mass is not None
        assert history.variance_reduction is not None and history.azimuthal_gap_deg is not None
        typer.echo(
            f"  peak force {history.peak_force_n.p05:.2e} / {history.peak_force_n.p50:.2e} / "
            f"{history.peak_force_n.p95:.2e} N"
        )
        ratio = history.mass.consistency_ratio
        typer.echo(
            f"  mass       {history.mass.mass_kg_p05:.2e} / {history.mass.mass_kg_p50:.2e} / "
            f"{history.mass.mass_kg_p95:.2e} kg"
            + (f" (consistency {ratio:.2f})" if ratio is not None else "")
        )
        typer.echo(
            f"  VR {history.variance_reduction:.3f}, gap {history.azimuthal_gap_deg:.0f} deg"
        )
    typer.echo(f"  wall {wall_s:.1f} s -> {path}")
    if write_report:
        report = write_event_report(run, repo / reports_dir, references, wall_s=wall_s)
        typer.echo(f"  report -> {report}")
    return path


@app.command("invert")
def invert(
    target_id: str = TARGET_ARGUMENT,
    repo: Path = REPO_OPTION,
    cache: Path | None = CACHE_OPTION,
    offline: bool = OFFLINE_OPTION,
    reports_dir: Path = REPORTS_OPTION,
    write_report: bool = REPORT_FLAG,
    warm: bool = WARM_FLAG,
) -> None:
    """Invert one event and write the force history plus a report."""
    run_inversion(
        target_id,
        repo=repo,
        cache=cache,
        offline=offline,
        reports_dir=reports_dir,
        write_report=write_report,
        warm=warm,
    )


@app.command("seal")
def seal(
    repo: Path = REPO_OPTION,
    reproductions: list[str] = REPRODUCTION_OPTION,
    force: bool = FORCE_OPTION,
) -> None:
    """Record the config hash and git sha the reproductions were validated under."""
    existing = read_seal(repo)
    if existing is not None and not force:
        typer.echo(f"already sealed at {existing.config_hash} ({existing.sealed_at_utc})")
        raise typer.Exit(code=1)
    config = LfhConfig()
    record = seal_config(
        config, git_sha=git_sha(repo), reproductions=list(reproductions) or _passing(repo)
    )
    path = write_seal(record, repo)
    typer.echo(f"sealed {record.config_hash} at {path}")
    typer.echo(f"  reproductions: {', '.join(record.reproductions) or 'none recorded'}")


def _passing(repo: Path) -> list[str]:
    """Reproduction targets whose run JSON exists, so `seal` can default sensibly."""
    out: list[str] = []
    references = load_references(repo)
    for target in references.reproductions:
        path = repo / "reports" / "m2" / f"{target.target_id}.json"
        if path.exists():
            out.append(target.target_id)
    return out


@app.command("targets")
def targets(repo: Path = REPO_OPTION) -> None:
    """List the events and what was published for each."""
    references = load_references(repo)
    typer.echo(
        f"{len(references.sources)} sources, "
        f"{len(references.sources_clearing_bar)} clearing the citation bar"
    )
    for target in references.targets:
        comparison = target.comparison_mass_kg()
        mass = f"{comparison[0]:.2e}-{comparison[1]:.2e} kg" if comparison else "no published mass"
        typer.echo(f"  {target.role:13s} {target.target_id:26s} {mass}")


def best_node_requests(
    target: LfhTarget, config: LfhConfig, repo: Path, reports_dir: Path
) -> list[GreensRequest]:
    """The Green's sets needed to re-invert this target at its *recorded* best location.

    The full grid-plus-bootstrap requirement is one to two megabytes per event, far too much
    to commit. Re-inverting at the location the grid search already found needs one distance
    per station at one depth -- a couple of hundred kilobytes -- and that is enough to prove
    the physics path runs offline from committed bytes and still returns the number in the
    committed report.
    """
    from serac.adapters.seismic.syngine import geocentric_distance_azimuth
    from serac.models.lfh.waveforms import prepare_channels, read_event_waveforms, select_channels

    payload = json.loads(
        (repo / reports_dir / f"{target.target_id}.json").read_text(encoding="utf-8")
    )
    location = payload["force_history"].get("source_location")
    if location is None:
        return []
    stream, inventory = read_event_waveforms(repo / target.fixture_dir)
    prepared, _ = prepare_channels(
        stream,
        inventory,
        origin_utc=target.origin_utc,
        source_lat=target.source_latitude,
        source_lon=target.source_longitude,
        config=config,
    )
    channels, _ = select_channels(prepared, config)
    depth_m = float(location["depth_km"]) * 1000.0
    seen: dict[str, GreensRequest] = {}
    for channel in channels:
        distance, _ = geocentric_distance_azimuth(
            float(location["latitude"]),
            float(location["longitude"]),
            channel.latitude,
            channel.longitude,
        )
        request = nearest_request(
            distance,
            model=config.earth_model,
            source_depth_m=depth_m,
            dt_s=config.dt_s,
            duration_s=config.greens_duration_s,
            step_deg=config.greens_step_deg,
            min_deg=config.stations.min_distance_deg,
            max_deg=config.stations.max_distance_deg,
        )
        seen.setdefault(request.cache_key(), request)
    return list(seen.values())


@app.command("fixtures")
def fixtures(
    repo: Path = REPO_OPTION,
    cache: Path | None = CACHE_OPTION,
    out: Path = OUT_OPTION,
    target: str | None = TARGET_OPTION,
    reports_dir: Path = REPORTS_OPTION,
) -> None:
    """Copy the Green's sets needed to re-invert committed events at their recorded location."""
    import shutil

    config = LfhConfig()
    references = load_references(repo)
    targets = [references.target(target)] if target else references.reproductions
    source_root = cache or (repo / INTERIM_CACHE)
    library = SyngineGreensLibrary(source_root, repo_root=repo, allow_network=False)
    ledger = JsonlManifestLedger(repo / "data" / "manifest.jsonl")
    recorded = {e.path for e in ledger.entries() if e.path}
    copied = 0
    missing: list[str] = []
    for item in targets:
        for request in best_node_requests(item, config, repo, reports_dir):
            path = library.cache_path(request)
            if not path.exists():
                missing.append(request.cache_key())
                continue
            destination = repo / out / path.parent.name / path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            copied += 1
            relative = destination.resolve().relative_to(repo.resolve()).as_posix()
            if relative in recorded:
                continue
            # A copy into the committed fixture tree is a new stored artefact and needs its
            # own ledger row: nothing may live under data/ without one, and validate-lfh
            # re-hashes each of these against the checksum recorded here.
            source_entry = library.read_index(config.earth_model).get(request.cache_key(), {})
            ledger.append(
                ManifestEntry(
                    source=DataSource.iris_syngine,
                    product_id=f"greens/fixture/{item.target_id}/{request.cache_key()}",
                    product_level="greens_function",
                    event_id=item.event_id or item.target_id,
                    path=relative,
                    url=PROVIDER_URL,
                    params={
                        "modelled": True,
                        "earth_model": config.earth_model.value,
                        "distance_deg": request.distance_deg,
                        "source_depth_m": request.source_depth_m,
                        "copied_from": str(source_entry.get("path", library.cache_path(request))),
                        "purpose": (
                            "offline re-inversion of "
                            f"{item.target_id} at its recorded best-fitting location"
                        ),
                    },
                    sha256=sha256_of_file(destination),
                    size_bytes=destination.stat().st_size,
                    retrieved_at=datetime.now(tz=UTC),
                    licence=GREENS_LICENCE,
                    licence_source_url=GREENS_LICENCE_URL,
                    provenance=Provenance.derived,
                    status=ManifestStatus.fetched,
                    adapter="serac lfh fixtures",
                    adapter_version="0.1.0",
                    notes=GREENS_LICENCE_NOTE,
                )
            )
            recorded.add(relative)
    total = sum(p.stat().st_size for p in (repo / out).rglob("*") if p.is_file())
    typer.echo(f"copied {copied} Green's sets ({total / 1024:.0f} kB) into {out}")
    if missing:
        typer.echo(f"missing {len(missing)} from the cache; run `serac lfh greens build` first")
        raise typer.Exit(code=1)


@app.command("summary")
def summary(repo: Path = REPO_OPTION, reports_dir: Path = REPORTS_OPTION) -> None:
    """Print the reproduction table from the run JSONs already written."""
    references = load_references(repo)
    rows: list[tuple[str, str, str, str, str]] = []
    for target in references.targets:
        path = repo / reports_dir / f"{target.target_id}.json"
        if not path.exists():
            rows.append((target.target_id, "-", "not run", "-", "-"))
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        history = payload["force_history"]
        if history["status"] != "computed":
            rows.append((target.target_id, "-", "REFUSED", "-", "-"))
            continue
        mass = history["mass"]
        comparison = target.comparison_mass_kg()
        published = f"{comparison[0]:.2e}-{comparison[1]:.2e}" if comparison else "-"
        serac = f"{mass['mass_kg_p05']:.2e}-{mass['mass_kg_p95']:.2e}"
        overlap = "-"
        if comparison:
            overlap = (
                "yes"
                if mass["mass_kg_p05"] <= comparison[1] and mass["mass_kg_p95"] >= comparison[0]
                else "NO"
            )
        ratio = "-"
        if comparison and comparison[0] > 0:
            centre = (comparison[0] * comparison[1]) ** 0.5
            ratio = f"{mass['mass_kg_p50'] / centre:.2f}"
        rows.append((target.target_id, published, serac, overlap, ratio))
    typer.echo(f"{'target':28s} {'published kg':24s} {'serac kg':24s} {'overlap':8s} ratio")
    for row in rows:
        typer.echo(f"{row[0]:28s} {row[1]:24s} {row[2]:24s} {row[3]:8s} {row[4]}")


@app.command("run-all")
def run_all(
    repo: Path = REPO_OPTION,
    cache: Path | None = CACHE_OPTION,
    offline: bool = OFFLINE_OPTION,
    role: str = ROLE_OPTION,
) -> None:
    """Invert every target of a role, in order."""
    references = load_references(repo)
    chosen = (
        references.targets if role == "all" else [t for t in references.targets if t.role == role]
    )
    for target in chosen:
        typer.echo(f"=== {target.target_id}")
        run_inversion(target.target_id, repo=repo, cache=cache, offline=offline)
