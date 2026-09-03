"""Run artefacts: the machine-readable JSON and the human-readable event report.

Two audiences. `reports/m2/<target>.json` is what `validate-lfh` reads, so it carries the
force history verbatim plus the diagnostics a gate needs -- config hash, Green's-function
checksums, timings, geometry. `reports/m2/<target>.md` is what a sceptical reader reads, and
it is written to make disagreement easy to find rather than easy to miss:

* a **Disagreement** section on every new-event report, quoting the public figures with
  attribution and stating the numeric relationship without arguing which is right;
* the refusal, in full, when the geometry could not support a location;
* every assumption behind the mass, verbatim from `MassEstimate.assumptions`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from serac.models.lfh.pipeline import InversionRun
from serac.models.lfh.references import LfhReferences, LfhTarget


def run_payload(run: InversionRun, *, wall_s: float, config_hash: str) -> dict[str, object]:
    grid = run.grid
    return {
        "target_id": run.target.target_id,
        "generated_at_utc": datetime.now(tz=UTC).isoformat(),
        "config_hash": config_hash,
        "config": json.loads(run.config.model_dump_json()),
        "wall_clock_s": round(wall_s, 3),
        "timings_s": {k: round(v, 3) for k, v in run.timings_s.items()},
        "force_history": json.loads(run.force_history.model_dump_json()),
        "geometry": (
            {
                "n_stations": run.geometry.n_stations,
                "n_channels": run.geometry.n_channels,
                "azimuthal_gap_deg": round(run.geometry.azimuthal_gap_deg, 2),
                "median_pre_event_snr": round(run.geometry.median_snr, 3),
                "min_distance_deg": round(run.geometry.min_distance_deg, 3),
                "max_distance_deg": round(run.geometry.max_distance_deg, 3),
                "stations": run.geometry.station_keys,
            }
            if run.geometry
            else None
        ),
        "stations": [
            {
                "key": channel.key,
                "component": channel.component,
                "latitude": channel.latitude,
                "longitude": channel.longitude,
                "distance_deg": round(channel.distance_deg, 4),
                "azimuth_deg": round(channel.azimuth_deg, 2),
                "peak_displacement_m": channel.amplitude,
                "pre_event_snr": round(channel.snr, 3),
            }
            for channel in run.channels
        ],
        "grid_search": (
            {
                "n_nodes": len(grid.nodes),
                "spacing_km": grid.grid_spacing_km,
                "stride": grid.stride,
                "lambda_value": grid.lambda_value,
                "best_variance_reduction": grid.best.variance_reduction,
                "uncertainty_radius_km": round(grid.uncertainty_radius_km(), 2),
                "surface": grid.surface(),
            }
            if grid
            else None
        ),
        "l_curve": (
            run.final.l_curve.as_dict()
            if run.final is not None and run.final.l_curve is not None
            else None
        ),
        "estimators": {
            name: (
                {
                    "name": estimate.name,
                    "method": estimate.method,
                    "mass_kg_p05": estimate.mass_kg_p05,
                    "mass_kg_p50": estimate.mass_kg_p50,
                    "mass_kg_p95": estimate.mass_kg_p95,
                    "a_eff": json.loads(estimate.a_eff.model_dump_json()),
                    "diagnostics": estimate.diagnostics,
                }
                if estimate
                else None
            )
            for name, estimate in (("a", run.estimator_a), ("b", run.estimator_b))
        },
        "terrain": (
            {
                "dem_path": run.terrain.dem_path,
                "azimuth_deg": run.terrain.azimuth_deg,
                "source_elevation_m": run.terrain.source_elevation_m,
                "profile_length_m": run.terrain.max_distance_m,
                "n_valid_samples": run.terrain.n_valid,
            }
            if run.terrain
            else None
        ),
        "greens_cache_keys": run.greens_cache_keys,
        "bootstrap_failures": run.draws.failures if run.draws else [],
        "notes": run.notes,
    }


def write_run_json(run: InversionRun, out_dir: Path, *, wall_s: float, config_hash: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run.target.target_id}.json"
    path.write_text(
        json.dumps(run_payload(run, wall_s=wall_s, config_hash=config_hash), indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _interval(value: object, unit: str = "") -> str:
    if value is None:
        return "not reported"
    p05 = getattr(value, "p05", None)
    p50 = getattr(value, "p50", None)
    p95 = getattr(value, "p95", None)
    if p05 is None:
        return "not reported"
    suffix = f" {unit}" if unit else ""
    return f"{p05:.3g} / **{p50:.3g}** / {p95:.3g}{suffix}"


def _disagreement_section(target: LfhTarget, run: InversionRun) -> list[str]:
    """Public figures quoted with attribution, and the numeric relationship, nothing more."""
    lines = ["## Disagreement", ""]
    history = run.force_history
    if not target.public_statements and target.published_mass_kg is None:
        lines.append(
            "No public mass or force figure was retrieved for this event in-session, so there "
            "is nothing to disagree with. That absence is the finding: serac's interval stands "
            "alone and has not been cross-checked against anyone else's."
        )
        lines.append("")
        return lines
    for statement in target.public_statements:
        lines.append(f"- {statement}")
    if target.published_mass_kg is not None:
        published = target.published_mass_kg
        lines.append(
            f'- {published.source_ref}: "{published.excerpt}" '
            f"({published.low:.3g}-{published.high:.3g} {published.units})"
        )
    lines.append("")
    if history.status == "computed" and history.mass is not None:
        mass = history.mass
        lines.append(
            f"serac's own interval is {mass.mass_kg_p05:.3g} to {mass.mass_kg_p95:.3g} kg "
            f"(median {mass.mass_kg_p50:.3g} kg)."
        )
        comparison = target.comparison_mass_kg()
        if comparison is not None:
            low, high, provenance = comparison
            centre = (low * high) ** 0.5
            ratio = mass.mass_kg_p50 / centre if centre > 0 else float("nan")
            overlaps = mass.mass_kg_p05 <= high and mass.mass_kg_p95 >= low
            lines.append("")
            lines.append(
                f"Against {provenance} ({low:.3g}-{high:.3g} kg): the intervals "
                f"{'overlap' if overlaps else 'do NOT overlap'}, and serac's median is "
                f"{ratio:.2f} times the geometric centre of the published interval."
            )
        lines.append("")
        lines.append(
            "This section states the numeric relationship and stops there. serac's estimate "
            "rests on assumptions listed above that the published figures do not share, and "
            "no parameter was adjusted after seeing these numbers."
        )
    else:
        lines.append(
            "serac produced no estimate for this event -- see the refusal above -- so there is "
            "no number to compare. A published figure existing does not make serac's silence "
            "wrong, and serac's silence does not make the published figure wrong."
        )
    lines.append("")
    return lines


def event_report(run: InversionRun, references: LfhReferences, *, wall_s: float) -> str:
    target = run.target
    history = run.force_history
    lines: list[str] = [
        f"# M2 force-history inversion — {target.name}",
        "",
        f"- Target id: `{target.target_id}` ({target.role.replace('_', ' ')})",
        f"- Origin: {target.origin_utc.isoformat()}",
        f"- Nominal source: {target.source_latitude:.4f}, {target.source_longitude:.4f}",
        f"- Config hash: `{run.config.config_hash()}`",
        f"- Wall clock: {wall_s:.1f} s",
        f"- Status: **{history.status}**",
        "",
    ]

    lines += ["## Station geometry", ""]
    if run.geometry is None:
        lines += ["No channels survived preparation.", ""]
    else:
        lines += [
            f"{run.geometry.describe()}.",
            "",
            "| channel | distance (deg) | azimuth (deg) | peak displacement (m) | SNR |",
            "|---|---:|---:|---:|---:|",
        ]
        for channel in sorted(run.channels, key=lambda c: c.distance_deg):
            lines.append(
                f"| `{channel.key}` | {channel.distance_deg:.2f} | "
                f"{channel.azimuth_deg:.0f} | {channel.amplitude:.2e} | {channel.snr:.2f} |"
            )
        lines.append("")

    if history.status != "computed":
        lines += [
            "## Refusal",
            "",
            history.notes,
            "",
            "serac refuses rather than guesses. A source location published from a station set "
            "this sparse would be a number with no evidence behind it, and the contract makes "
            'that impossible to emit: `status="failed"` histories may not carry a location, a '
            "mass or any force samples.",
            "",
        ]
        lines += _disagreement_section(target, run)
        return "\n".join(lines) + "\n"

    assert history.source_location is not None
    assert history.mass is not None
    location = history.source_location
    lines += [
        "## Result",
        "",
        "| quantity | p05 / **p50** / p95 |",
        "|---|---|",
        f"| Peak force | {_interval(history.peak_force_n, 'N')} |",
        f"| Impulse | {_interval(history.impulse_ns, 'N s')} |",
        f"| Duration | {_interval(history.duration_s, 's')} |",
        f"| Force azimuth | {_interval(history.force_azimuth_deg, 'deg from north')} |",
        (
            f"| **Mass** | {history.mass.mass_kg_p05:.3g} / "
            f"**{history.mass.mass_kg_p50:.3g}** / {history.mass.mass_kg_p95:.3g} kg |"
        ),
        "",
        f"- Location: {location.latitude:.4f}, {location.longitude:.4f} "
        f"(depth {location.depth_km:.1f} km, method `{location.method}`, "
        f"grid {location.grid_spacing_km:g} km, "
        f"resolution radius {location.uncertainty_radius_km:.1f} km)",
        f"- Variance reduction: {location.variance_reduction:.3f}",
        f"- Azimuthal gap: {location.azimuthal_gap_deg:.0f} deg",
        f"- Regularisation: {history.regularisation}, lambda {history.lambda_value:.4g} "
        "from the L-curve corner",
        "",
    ]

    if run.estimator_a is not None and run.estimator_b is not None:
        lines += [
            "### The two mass estimators",
            "",
            "| estimator | method | p05 (kg) | p50 (kg) | p95 (kg) | a_eff basis |",
            "|---|---|---:|---:|---:|---|",
        ]
        for estimate in (run.estimator_a, run.estimator_b):
            lines.append(
                f"| {estimate.name} | `{estimate.method}` | {estimate.mass_kg_p05:.3g} | "
                f"{estimate.mass_kg_p50:.3g} | {estimate.mass_kg_p95:.3g} | "
                f"`{estimate.a_eff.basis}` |"
            )
        ratio = history.mass.consistency_ratio
        lines += [
            "",
            f"Consistency ratio (A/B on the medians): **{ratio:.2f}**"
            + ("" if ratio is None or 1 / 3 <= ratio <= 3 else " — outside [1/3, 3]."),
            "",
            "The published interval is the **union** of the two, not their average.",
            "",
        ]

    if history.greens is not None:
        lines += [
            "### Modelled Green's functions",
            "",
            f"- Earth model: `{history.greens.earth_model}` via {history.greens.provider}",
            f"- Band: {history.greens.band_s[0]:g}-{history.greens.band_s[1]:g} s at "
            f"dt = {history.greens.dt_s:g} s",
            f"- {len(history.greens.cache_sha256)} cached sets, recorded as "
            "`provenance: derived` (ADR-0016); they are modelled physics, never observations, "
            "and are never published on the bus.",
            "",
        ]

    if history.bootstrap is not None:
        lines += [
            "### Uncertainty",
            "",
            f"{history.bootstrap.n_draws} bootstrap draws (seed {history.bootstrap.seed}) "
            f"resampling {', '.join(history.bootstrap.resampled)}.",
            "",
        ]
        if run.draws and run.draws.failures:
            lines += [f"{len(run.draws.failures)} draws failed and were excluded.", ""]

    lines += ["### Assumptions behind the mass", ""]
    lines += [f"{i + 1}. {text}" for i, text in enumerate(history.mass.assumptions)]
    lines += [""]

    if run.notes:
        lines += ["### Notes from the run", ""]
        lines += [f"- {note}" for note in run.notes]
        lines += [""]

    lines += ["## Timings", "", "| stage | seconds |", "|---|---:|"]
    for stage, value in run.timings_s.items():
        lines.append(f"| {stage.replace('_s', '')} | {value:.2f} |")
    lines += [f"| **total wall clock** | **{wall_s:.2f}** |", ""]

    lines += _disagreement_section(target, run)

    used = {
        quantity.source_ref
        for quantity in (
            target.published_mass_kg,
            target.published_peak_force_n,
            target.published_duration_s,
            target.published_volume_m3,
        )
        if quantity is not None
    }
    if target.geometry_source_ref:
        used.add(target.geometry_source_ref)
    if used:
        lines += ["## Sources", ""]
        for source_id in sorted(used):
            source = references.source(source_id)
            lines.append(
                f"- `{source_id}` — {source.citation()}; fetched {source.accessed_utc.date()}, "
                f"sha256 `{source.sha256[:16]}...`, DOI resolved via "
                f"{source.doi_resolved_via or 'not resolved'}"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def write_event_report(
    run: InversionRun, out_dir: Path, references: LfhReferences, *, wall_s: float
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run.target.target_id}.md"
    path.write_text(event_report(run, references, wall_s=wall_s), encoding="utf-8")
    return path


def force_history_csv(run: InversionRun) -> str:
    """The force history as CSV, for anyone who would rather plot it than read JSON."""
    history = run.force_history
    if history.status != "computed":
        return "status,failed\n"
    assert history.force_up_n is not None
    dt = history.sample_interval_s or 1.0
    header = (
        "time_s,up_p05_n,up_p50_n,up_p95_n,north_p05_n,north_p50_n,north_p95_n,"
        "east_p05_n,east_p50_n,east_p95_n"
    )
    columns = [
        history.force_up_p05_n,
        history.force_up_n,
        history.force_up_p95_n,
        history.force_north_p05_n,
        history.force_north_n,
        history.force_north_p95_n,
        history.force_east_p05_n,
        history.force_east_n,
        history.force_east_p95_n,
    ]
    array = np.array(columns, dtype=float)
    rows = [header]
    for index in range(array.shape[1]):
        values = ",".join(f"{v:.6e}" for v in array[:, index])
        rows.append(f"{index * dt:.1f},{values}")
    return "\n".join(rows) + "\n"
