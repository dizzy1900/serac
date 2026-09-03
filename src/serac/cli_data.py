"""`serac data ...` — dataset assembly for the model components.

`build-discriminator-set` is M1's training-set build. `--dry-run` writes nothing at all, not
even a ledger line: it fetches only the catalogue metadata it needs to count windows and
choose stations, then prints the class/region/decade tables and a byte estimate with the
assumption the estimate rests on. Above 5 GB it refuses without `--yes`, per CLAUDE.md rule 7.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger
from serac.models.discriminator.catalog import ClassLabel, DiscriminatorCatalog, assemble
from serac.models.discriminator.windows import StationChoice
from serac.pipelines.discriminator_build import (
    CONFIRM_ABOVE_BYTES,
    BuildPlan,
    _Cache,
    _client,
    build_dataset,
    fetch_stations,
    fetch_tectonic,
    plan_build,
)
from serac.ports.seismic import CatalogEvent

app = typer.Typer(help="Assemble model training sets.", no_args_is_help=True)


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:,.1f} {unit}"
        value /= 1000
    return f"{value:,.1f} TB"


def _prepare(
    repo: Path, *, workers: int, echo: bool = True
) -> tuple[DiscriminatorCatalog, dict[str, list[StationChoice]]]:
    """Phases 1-2: tectonic candidates and station selection. Network, but no bulk download."""
    from concurrent.futures import ThreadPoolExecutor

    from serac.models.discriminator.catalog import build_positives

    cache = _Cache(repo)
    cache.ensure()
    positives, _, _ = build_positives(repo)
    if echo:
        typer.echo(f"positives after dedupe: {len(positives)}")
    with _client() as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            tectonic: dict[str, Sequence[CatalogEvent]] = dict(
                zip(
                    (p.entry_id for p in positives),
                    (r[1] for r in pool.map(lambda p: fetch_tectonic(p, cache, client), positives)),
                    strict=True,
                )
            )
        if echo:
            typer.echo(f"tectonic candidates: {sum(len(v) for v in tectonic.values()):,}")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            selections = list(pool.map(lambda p: fetch_stations(p, cache, client), positives))
    stations_by_group = {
        p.event_group: choice for p, (_, choice) in zip(positives, selections, strict=True)
    }
    catalogue = assemble(repo, tectonic_by_positive=tectonic)
    if echo:
        typer.echo(f"windows: {len(catalogue.entries):,} over {len(catalogue.groups):,} groups")
    return catalogue, stations_by_group


def _print_plan(plan: BuildPlan) -> None:
    typer.echo("")
    typer.echo(
        f"positives {plan.n_positives:,} | negatives {plan.n_negatives:,} | "
        f"noise {plan.n_noise:,} | windows {plan.n_windows:,} | groups {plan.n_groups:,}"
    )
    typer.echo(f"unique stations: {plan.n_unique_stations:,}")
    typer.echo("")
    typer.echo("class x region")
    for region, row in plan.class_by_region.items():
        counts = "  ".join(f"{k}={v}" for k, v in row.items())
        typer.echo(f"  {region:30s} {counts}")
    typer.echo("class x decade")
    for decade, row in plan.class_by_decade.items():
        counts = "  ".join(f"{k}={v}" for k, v in row.items())
        typer.echo(f"  {decade:30s} {counts}")
    typer.echo("")
    typer.echo(
        f"estimated bytes: waveforms {_human(plan.estimated_waveform_bytes)} + responses "
        f"{_human(plan.estimated_response_bytes)} + zarr {_human(plan.estimated_zarr_bytes)} "
        f"= {_human(plan.estimated_total_bytes)}"
    )
    typer.echo(f"estimate basis: {plan.estimate_basis}")
    for warning in plan.warnings:
        typer.echo(f"  warning: {warning}")


@app.command("build-discriminator-set")
def build_discriminator_set(
    repo: Annotated[Path, typer.Option(help="Repository root.")] = Path(),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print counts and a byte estimate; write nothing.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help=f"Confirm a build above {_human(CONFIRM_ABOVE_BYTES)}.")
    ] = False,
    resume: Annotated[
        bool, typer.Option("--resume", help="Reuse anything already on disk (the default).")
    ] = True,
    workers: Annotated[int, typer.Option(help="Concurrent FDSN requests.")] = 8,
) -> None:
    """Assemble the M1 discriminator training set into Zarr with a hashed chunk index."""
    catalogue, stations_by_group = _prepare(repo, workers=workers, echo=not dry_run)
    plan = plan_build(catalogue, stations_by_group)
    _print_plan(plan)

    if dry_run:
        typer.echo("")
        typer.echo("--dry-run: nothing written, no ledger rows appended.")
        raise typer.Exit(0)

    if plan.needs_confirmation and not yes:
        typer.echo("")
        typer.secho(
            f"refusing: estimated {_human(plan.estimated_total_bytes)} exceeds the "
            f"{_human(CONFIRM_ABOVE_BYTES)} ask-first threshold. Re-run with --yes.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    if not resume:
        typer.secho(
            "note: --no-resume does not delete caches; remove data/raw/discriminator to refetch.",
            fg=typer.colors.YELLOW,
        )

    ledger = JsonlManifestLedger(repo / "data" / "manifest.jsonl")
    report = build_dataset(
        repo,
        catalogue,
        stations_by_group,
        ledger,
        workers=workers,
        progress=lambda message: typer.echo(f"  {message}"),
    )
    reports_dir = repo / "reports" / "m1"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "build.json").write_text(report.model_dump_json(indent=2) + "\n", "utf-8")
    (reports_dir / "plan.json").write_text(plan.model_dump_json(indent=2) + "\n", "utf-8")

    typer.echo("")
    typer.echo(
        f"windows written {report.n_windows_written:,} / {report.n_windows_requested:,}; "
        f"positives {report.positives_written:,} / {report.positives_requested:,}"
    )
    typer.echo(f"not fetched (recorded, not substituted): {report.n_windows_not_fetched:,}")
    typer.echo(f"bytes fetched: {_human(report.bytes_fetched)}")
    typer.echo(f"chunk index sha256: {report.chunk_index_sha256}")


@app.command("describe-discriminator-set")
def describe_discriminator_set(
    repo: Annotated[Path, typer.Option(help="Repository root.")] = Path(),
) -> None:
    """Print the built set's class, region and decade tables from its committed index."""
    from collections import Counter

    from serac.models.discriminator.dataset import load_index

    index = load_index(repo / "data" / "features" / "discriminator")
    typer.echo(f"{index.n_windows:,} windows over {len(index.groups()):,} groups")
    for name, key in (("class", "class_label"), ("region", "region_id"), ("decade", "decade")):
        counts = Counter(str(getattr(w, key)) for w in index.windows)
        typer.echo(f"{name}: " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    positives = [w for w in index.windows if w.class_label is ClassLabel.mass_movement]
    typer.echo(f"positives: {len(positives):,}")
    table: dict[tuple[str, str], int] = {}
    for window in positives:
        table[(window.region_id, window.decade)] = (
            table.get((window.region_id, window.decade), 0) + 1
        )
    typer.echo(json.dumps({f"{r}|{d}": n for (r, d), n in sorted(table.items())}, indent=1))
