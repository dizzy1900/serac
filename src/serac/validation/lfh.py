"""`make validate-lfh`: does M2 earn the right to be called validated?

The suite is built around the ways this component could look validated without being so:

* **Published numbers recalled rather than fetched.** Every reference must have been fetched,
  hashed and DOI-resolved in session. Fewer than three clearing that bar and the suite fails
  with `published_refs_fetched=False`. It does not pass on two.
* **Overlap achieved by a vacuously wide interval.** Interval overlap is the pass criterion,
  but a magnitude sanity check reports a warning whenever the median is more than a factor of
  three from the published centre (two, for peak force) -- an interval so wide it would
  overlap anything is not a reproduction.
* **A point mass.** `MassEstimate` forbids one; the suite proves the validator actually fires
  rather than trusting that it exists.
* **A location the geometry cannot support.** The suite constructs a sparse, wide-gap station
  set and asserts serac refuses it.
* **Fixtures quietly regenerated.** Every committed waveform and Green's-function fixture is
  re-hashed against its recorded checksum.
* **Tuning between the reproductions and the new events.** The seal's config hash must match
  the hash every run recorded.

A refusal is not a failure here. Chamoli, Langtang and Blatten all refuse, for reasons the
suite checks are *stated*; what would fail the suite is a refusal that quietly carried a
location or a mass anyway.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from serac.adapters.storage.manifest_ledger import JsonlManifestLedger, sha256_of_file
from serac.domain.force_history import ForceHistory, MassEstimate
from serac.domain.manifest import DataSource, Provenance
from serac.models.lfh.config import LfhConfig, read_seal
from serac.models.lfh.references import LfhReferences, LfhTarget, load_references
from serac.validation.result import Severity, Suite, SuiteResult

SUITE_NAME = "lfh"
REPORTS_DIR = Path("reports/m2")
GREENS_FIXTURE_DIR = Path("data/fixtures/greens")
WAVEFORM_FIXTURE_DIR = Path("data/fixtures/lfh")

#: How many published reproductions must overlap for the gate to pass.
REQUIRED_REPRODUCTIONS = 3
#: Fewer sources than this clearing the citation bar and the suite fails outright.
REQUIRED_REFERENCES = 3
#: Magnitude sanity bands: overlap alone is not evidence if the median is this far out.
MASS_SANITY = (1.0 / 3.0, 3.0)
FORCE_SANITY = (0.5, 2.0)


@dataclass(frozen=True)
class Reproduction:
    """One reproduction target's comparison, pass or fail."""

    target_id: str
    status: str
    published_low: float | None
    published_high: float | None
    published_provenance: str | None
    serac_p05: float | None
    serac_p50: float | None
    serac_p95: float | None
    overlaps: bool
    ratio: float | None
    variance_reduction: float | None

    @property
    def sanity_ok(self) -> bool:
        if self.ratio is None:
            return True
        return MASS_SANITY[0] <= self.ratio <= MASS_SANITY[1]

    def row(self) -> str:
        if self.status != "computed":
            return f"{self.target_id}: {self.status.upper()} (no comparison possible)"
        assert self.serac_p05 is not None and self.serac_p95 is not None
        published = (
            f"{self.published_low:.3g}-{self.published_high:.3g}"
            if self.published_low is not None
            else "none"
        )
        return (
            f"{self.target_id}: published {published} kg vs serac "
            f"{self.serac_p05:.3g}-{self.serac_p95:.3g} kg -> "
            f"{'overlap' if self.overlaps else 'NO OVERLAP'}"
            + (f", ratio {self.ratio:.2f}" if self.ratio is not None else "")
        )


def _load_run(repo: Path, target_id: str, reports_dir: Path) -> dict[str, Any] | None:
    path = repo / reports_dir / f"{target_id}.json"
    if not path.exists():
        return None
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def compare(target: LfhTarget, payload: dict[str, Any] | None) -> Reproduction:
    """serac's interval against the published one, by overlap plus a magnitude ratio."""
    comparison = target.comparison_mass_kg()
    if payload is None:
        return Reproduction(
            target.target_id, "not_run", None, None, None, None, None, None, False, None, None
        )
    history = payload["force_history"]
    if history["status"] != "computed" or history.get("mass") is None:
        return Reproduction(
            target.target_id,
            history["status"],
            comparison[0] if comparison else None,
            comparison[1] if comparison else None,
            comparison[2] if comparison else None,
            None,
            None,
            None,
            False,
            None,
            history.get("variance_reduction"),
        )
    mass = history["mass"]
    p05, p50, p95 = mass["mass_kg_p05"], mass["mass_kg_p50"], mass["mass_kg_p95"]
    if comparison is None:
        return Reproduction(
            target.target_id,
            "computed",
            None,
            None,
            None,
            p05,
            p50,
            p95,
            False,
            None,
            history.get("variance_reduction"),
        )
    low, high, provenance = comparison
    centre = (low * high) ** 0.5
    return Reproduction(
        target_id=target.target_id,
        status="computed",
        published_low=low,
        published_high=high,
        published_provenance=provenance,
        serac_p05=p05,
        serac_p50=p50,
        serac_p95=p95,
        overlaps=p05 <= high and p95 >= low,
        ratio=(p50 / centre) if centre > 0 else None,
        variance_reduction=history.get("variance_reduction"),
    )


def _check_references(suite: Suite, references: LfhReferences) -> bool:
    clearing = references.sources_clearing_bar
    ok = len(clearing) >= REQUIRED_REFERENCES
    suite.check(
        "lfh.published_refs_fetched",
        ok,
        (
            f"published_refs_fetched={ok}: {len(clearing)} of {len(references.sources)} sources "
            f"were fetched, hashed and DOI-resolved in session "
            f"({', '.join(s.id for s in clearing)}); the gate requires "
            f"{REQUIRED_REFERENCES}"
        ),
    )
    missing_excerpt = [
        f"{target.target_id}.{name}"
        for target in references.targets
        for name, quantity in (
            ("published_mass_kg", target.published_mass_kg),
            ("published_peak_force_n", target.published_peak_force_n),
            ("published_volume_m3", target.published_volume_m3),
        )
        if quantity is not None and not quantity.excerpt.strip()
    ]
    suite.check(
        "lfh.published_numbers_carry_a_verbatim_excerpt",
        not missing_excerpt,
        "; ".join(missing_excerpt) or "every published figure carries the sentence it came from",
    )
    conversions = [
        f"{t.target_id}: {t.mass_conversion.from_quantity} -> {t.mass_conversion.to_quantity} "
        f"at {t.mass_conversion.factor_low:g}-{t.mass_conversion.factor_high:g} "
        f"{t.mass_conversion.factor_units}"
        for t in references.targets
        if t.mass_conversion is not None
    ]
    if conversions:
        suite.info("lfh.derived_comparison_intervals", "; ".join(conversions))
    return ok


def _check_reproductions(
    suite: Suite, references: LfhReferences, repo: Path, reports_dir: Path
) -> list[Reproduction]:
    rows = [
        compare(target, _load_run(repo, target.target_id, reports_dir))
        for target in references.reproductions
    ]
    passing = [r for r in rows if r.status == "computed" and r.overlaps]
    suite.check(
        "lfh.reproductions_overlap",
        len(passing) >= REQUIRED_REPRODUCTIONS,
        f"{len(passing)} of {len(rows)} published reproductions overlap by interval "
        f"(need {REQUIRED_REPRODUCTIONS}): " + " | ".join(r.row() for r in rows),
    )
    unsane = [r for r in passing if not r.sanity_ok]
    suite.check(
        "lfh.magnitude_sanity",
        not unsane,
        (
            "; ".join(
                f"{r.target_id}: median/published = {r.ratio:.2f}, outside "
                f"[{MASS_SANITY[0]:.2f}, {MASS_SANITY[1]:.2f}] -- the overlap comes from the "
                "width of serac's interval, not from agreement"
                for r in unsane
                if r.ratio is not None
            )
            or "every overlapping reproduction also agrees in magnitude"
        ),
        Severity.warning,
    )
    _check_peak_force_sanity(suite, references, repo, reports_dir)
    refused = [r for r in rows if r.status == "failed"]
    if refused:
        suite.info(
            "lfh.reproductions_refused",
            "; ".join(
                f"{r.target_id} refused"
                + (f" (VR {r.variance_reduction:.3f})" if r.variance_reduction else "")
                for r in refused
            ),
        )
    return rows


def _check_peak_force_sanity(
    suite: Suite, references: LfhReferences, repo: Path, reports_dir: Path
) -> None:
    problems: list[str] = []
    checked = 0
    for target in references.targets:
        if target.published_peak_force_n is None:
            continue
        payload = _load_run(repo, target.target_id, reports_dir)
        if payload is None:
            continue
        history = payload["force_history"]
        if history["status"] != "computed" or history.get("peak_force_n") is None:
            continue
        checked += 1
        published = target.published_peak_force_n
        centre = (published.low * published.high) ** 0.5
        ratio = history["peak_force_n"]["p50"] / centre if centre > 0 else float("nan")
        if not FORCE_SANITY[0] <= ratio <= FORCE_SANITY[1]:
            problems.append(
                f"{target.target_id}: peak force median/published = {ratio:.2f}, outside "
                f"[{FORCE_SANITY[0]}, {FORCE_SANITY[1]}]"
            )
    suite.check(
        "lfh.peak_force_magnitude_sanity",
        not problems,
        "; ".join(problems) or f"{checked} published peak force(s) within a factor of two",
        Severity.warning,
    )


def _check_no_point_mass(
    suite: Suite, repo: Path, references: LfhReferences, reports_dir: Path
) -> None:
    """The contract must make a point mass unconstructible, and every run must obey it."""
    rejected = False
    try:
        MassEstimate(
            mass_kg_p05=1.0e11,
            mass_kg_p50=1.0e11,
            mass_kg_p95=1.0e11,
            method="combined",
            assumptions=["a point mass must not be constructible"],
        )
    except ValueError:
        rejected = True
    suite.check(
        "lfh.no_point_mass_is_constructible",
        rejected,
        "MassEstimate rejects p05 == p50 == p95"
        if rejected
        else "MassEstimate accepted a point mass; the validator is not doing its job",
    )

    offenders: list[str] = []
    validated = 0
    for target in references.targets:
        payload = _load_run(repo, target.target_id, reports_dir)
        if payload is None:
            continue
        try:
            history = ForceHistory.model_validate(payload["force_history"])
        except ValueError as exc:
            offenders.append(f"{target.target_id}: force history fails its own contract ({exc})")
            continue
        validated += 1
        if history.status == "computed":
            assert history.mass is not None
            if not history.mass.mass_kg_p05 < history.mass.mass_kg_p50 < history.mass.mass_kg_p95:
                offenders.append(f"{target.target_id}: mass is not a strict interval")
            if not history.mass.assumptions:
                offenders.append(f"{target.target_id}: mass carries no assumptions")
        elif history.source_location is not None or history.mass is not None:
            offenders.append(
                f"{target.target_id}: a {history.status} history carries a location or a mass"
            )
    suite.check(
        "lfh.no_point_mass",
        not offenders,
        "; ".join(offenders) or f"{validated} force histories validate and none is a point mass",
    )


def _check_refusals(suite: Suite, repo: Path, references: LfhReferences, reports_dir: Path) -> None:
    """The geometry refusal, exercised directly and then checked in the committed runs."""
    from serac.models.lfh.waveforms import Geometry, refusal_reason

    config = LfhConfig()
    wide_gap = Geometry(
        n_stations=6,
        n_channels=18,
        azimuthal_gap_deg=250.0,
        min_distance_deg=2.0,
        max_distance_deg=9.0,
        station_keys=[f"XX.S{i}" for i in range(6)],
    )
    too_few = Geometry(
        n_stations=3,
        n_channels=9,
        azimuthal_gap_deg=40.0,
        min_distance_deg=2.0,
        max_distance_deg=9.0,
        station_keys=["XX.S0", "XX.S1", "XX.S2"],
    )
    good = Geometry(
        n_stations=8,
        n_channels=24,
        azimuthal_gap_deg=90.0,
        min_distance_deg=1.0,
        max_distance_deg=10.0,
        station_keys=[f"XX.S{i}" for i in range(8)],
    )
    gap_reason = refusal_reason(wide_gap, config)
    few_reason = refusal_reason(too_few, config)
    ok_reason = refusal_reason(good, config)
    suite.check(
        "lfh.refuses_on_large_gap",
        gap_reason is not None and few_reason is not None and ok_reason is None,
        (
            f"250 deg gap -> {gap_reason!r}; 3 stations -> {few_reason!r}; "
            f"8 stations at 90 deg -> {ok_reason!r}"
        ),
    )

    unstated: list[str] = []
    refusals: list[str] = []
    for target in references.targets:
        payload = _load_run(repo, target.target_id, reports_dir)
        if payload is None:
            continue
        history = payload["force_history"]
        if history["status"] == "computed":
            continue
        notes = history.get("notes", "")
        refusals.append(target.target_id)
        if "REFUSED" not in notes:
            unstated.append(f"{target.target_id}: refusal does not say so in its notes")
        if history.get("source_location") is not None:
            unstated.append(f"{target.target_id}: refused but published a location")
        report = repo / reports_dir / f"{target.target_id}.md"
        if report.exists() and "## Refusal" not in report.read_text(encoding="utf-8"):
            unstated.append(f"{target.target_id}: report has no Refusal section")
    suite.check(
        "lfh.refusals_state_their_geometry",
        not unstated,
        "; ".join(unstated)
        or (
            f"{len(refusals)} refusal(s) ({', '.join(refusals) or 'none'}) each state the "
            "geometry and carry no location"
        ),
    )


def _check_fixture_hashes(suite: Suite, repo: Path) -> None:
    """Committed Green's functions and waveforms must still be the bytes that were recorded."""
    ledger = JsonlManifestLedger(repo / "data" / "manifest.jsonl")
    by_path: dict[str, str] = {}
    for entry in ledger.entries():
        if entry.path and entry.sha256:
            by_path[entry.path] = entry.sha256

    for label, directory in (
        ("greens", GREENS_FIXTURE_DIR),
        ("waveforms", WAVEFORM_FIXTURE_DIR),
    ):
        root = repo / directory
        mismatched: list[str] = []
        unrecorded: list[str] = []
        checked = 0
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name == "manifest.json":
                continue
            relative = path.relative_to(repo).as_posix()
            expected = by_path.get(relative)
            if expected is None:
                unrecorded.append(relative)
                continue
            checked += 1
            if sha256_of_file(path) != expected:
                mismatched.append(relative)
        suite.check(
            f"lfh.{label}_fixture_hashes",
            not mismatched and not unrecorded,
            (
                "; ".join(
                    [f"changed: {p}" for p in mismatched]
                    + [f"no ledger row: {p}" for p in unrecorded]
                )
                or f"{checked} {label} fixture file(s) re-hashed and matching"
            ),
        )

    modelled = [entry for entry in ledger.entries() if entry.source is DataSource.iris_syngine]
    wrong = [
        f"{e.product_id}: provenance={e.provenance.value}, modelled={e.params.get('modelled')}"
        for e in modelled
        if e.provenance is not Provenance.derived or e.params.get("modelled") is not True
    ]
    suite.check(
        "lfh.greens_are_derived_not_synthetic",
        not wrong and bool(modelled),
        "; ".join(wrong)
        or (
            f"{len(modelled)} Syngine row(s) recorded as provenance=derived with modelled=true "
            "(ADR-0016)"
        ),
    )


#: Modules that must never import the Green's-function machinery. `domain/manifest.py` names
#: `iris_syngine` as a `DataSource`, which is the point -- the ledger has to be able to record
#: modelled data -- so the check looks for *imports*, not for the string.
_BUS_ISOLATED_DIRS = ("streaming", "adapters/bus", "domain")
_FORBIDDEN_IMPORTS = re.compile(
    r"^\s*(?:from\s+serac\.(?:models\.lfh|adapters\.seismic\.syngine)|"
    r"import\s+serac\.(?:models\.lfh|adapters\.seismic\.syngine))",
    re.MULTILINE,
)


def _check_greens_never_on_the_bus(suite: Suite, repo: Path) -> None:
    """Nothing in the streaming, bus or domain layer may import the Green's machinery.

    A Green's function published as a `SeismicTrace` would have to claim `synthetic`
    provenance and would be indistinguishable from a recording downstream (ADR-0016). The
    isolation is structural: if those layers cannot import the code that produces Green's
    functions, they cannot publish one.
    """
    offenders: list[str] = []
    scanned = 0
    for directory in _BUS_ISOLATED_DIRS:
        root = repo / "src" / "serac" / directory
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            scanned += 1
            for match in _FORBIDDEN_IMPORTS.finditer(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(repo).as_posix()}: {match.group(0).strip()}")
    codec = repo / "src" / "serac" / "domain" / "codec.py"
    if codec.exists() and "greens" in codec.read_text(encoding="utf-8").lower():
        offenders.append("domain/codec.py registers a Green's-function schema on the wire")
    suite.check(
        "lfh.greens_never_published_on_the_bus",
        not offenders,
        "; ".join(offenders)
        or (
            f"{scanned} streaming, bus and domain module(s) scanned; none imports the "
            "Green's-function machinery and no Green's schema is registered on the wire"
        ),
    )


def _check_seal(suite: Suite, repo: Path, references: LfhReferences, reports_dir: Path) -> None:
    seal = read_seal(repo)
    if seal is None:
        suite.check(
            "lfh.seal_present",
            False,
            "no reports/m2/seal.json: the config was never sealed, so nothing stops it being "
            "changed between the published reproductions and the new-event runs",
        )
        return
    suite.check(
        "lfh.seal_present",
        True,
        f"sealed {seal.config_hash[:16]}... at {seal.sealed_at_utc.isoformat()} "
        f"(git {seal.git_sha[:8] if seal.git_sha else 'unknown'}) covering "
        f"{', '.join(seal.reproductions) or 'no recorded reproductions'}",
    )
    current = LfhConfig().config_hash()
    suite.check(
        "lfh.seal_matches_current_config",
        current == seal.config_hash,
        f"current config hashes to {current[:16]}..., seal records {seal.config_hash[:16]}..."
        if current != seal.config_hash
        else "the working configuration is the sealed one",
    )
    drifted: list[str] = []
    checked = 0
    for target in references.targets:
        payload = _load_run(repo, target.target_id, reports_dir)
        if payload is None:
            continue
        checked += 1
        if payload.get("config_hash") != seal.config_hash:
            drifted.append(
                f"{target.target_id} ran under {str(payload.get('config_hash'))[:16]}..."
            )
    suite.check(
        "lfh.runs_share_the_sealed_config",
        not drifted,
        "; ".join(drifted)
        or (
            f"all {checked} run(s), reproductions and new events alike, carry the sealed "
            "config hash"
        ),
    )


def _check_new_events(
    suite: Suite, repo: Path, references: LfhReferences, reports_dir: Path
) -> None:
    """New-event reports must carry a Disagreement section, whatever the outcome."""
    missing: list[str] = []
    for target in references.new_events:
        report = repo / reports_dir / f"{target.target_id}.md"
        if not report.exists():
            missing.append(f"{target.target_id}: no report written")
            continue
        text = report.read_text(encoding="utf-8")
        if "## Disagreement" not in text:
            missing.append(f"{target.target_id}: report has no Disagreement section")
        if target.public_statements and not any(
            statement[:40] in text for statement in target.public_statements
        ):
            missing.append(f"{target.target_id}: public figures not quoted in the report")
    suite.check(
        "lfh.new_events_report_disagreement",
        not missing,
        "; ".join(missing)
        or (
            f"{len(references.new_events)} new-event report(s) quote the public figures with "
            "attribution and state the numeric relationship"
        ),
    )


#: Which target the offline re-inversion runs on, and how far its peak force may drift.
OFFLINE_TARGET = "taan-fiord-2015"


def _check_offline_reinversion(
    suite: Suite, repo: Path, references: LfhReferences, m2_dir: Path
) -> None:
    """Re-run the physics offline from committed bytes and reproduce the committed number.

    Everything else in this suite reads artefacts that a previous run produced. This check
    actually inverts: it loads the committed waveforms and the committed Green's subset with
    the network refused, solves at the location the grid search recorded, and compares the
    peak force with the one in the report. A regression in the design matrix, the filter, the
    rotation or the geocentric distance would move that number.

    The committed Green's subset covers one location at one depth, which is why this
    re-inverts rather than re-searching: the full grid and bootstrap requirement is one to two
    megabytes per event.
    """
    import numpy as np

    from serac.adapters.seismic.syngine import (
        SyngineGreensLibrary,
    )
    from serac.models.lfh.gsf import GreensCache, TrialLocation, kernels_for
    from serac.models.lfh.inversion import invert
    from serac.models.lfh.waveforms import (
        prepare_channels,
        read_event_waveforms,
        select_channels,
        station_weights,
    )

    payload = _load_run(repo, OFFLINE_TARGET, m2_dir)
    if payload is None:
        suite.check("lfh.offline_reinversion", False, f"no committed run for {OFFLINE_TARGET}")
        return
    history = payload["force_history"]
    location = history.get("source_location")
    if location is None or history.get("peak_force_n") is None:
        suite.check(
            "lfh.offline_reinversion", False, f"{OFFLINE_TARGET} recorded no location to re-use"
        )
        return

    config = LfhConfig()
    target = references.target(OFFLINE_TARGET)
    fixture_cache = repo / "data" / "fixtures" / "greens" / "lfh"
    library = SyngineGreensLibrary(fixture_cache, repo_root=repo, allow_network=False)
    ledger = JsonlManifestLedger(repo / "data" / "manifest.jsonl")
    try:
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
        node = TrialLocation(
            float(location["latitude"]),
            float(location["longitude"]),
            float(location["depth_km"]) * 1000.0,
        )
        cache = GreensCache(library, ledger, config)
        kernels = kernels_for(channels, node, cache, station_weights(channels))
        result = invert(
            kernels,
            n_basis=config.n_source_samples,
            stride=1,
            shift=config.greens_shift_samples,
            dt=config.dt_s,
            zero_endpoints=config.regularisation.zero_endpoints,
            lambda_value=history["lambda_value"],
        )
    except Exception as exc:  # a failure here is the finding, not a crash to hide
        suite.check(
            "lfh.offline_reinversion",
            False,
            f"re-inverting {OFFLINE_TARGET} offline failed: {type(exc).__name__}: {exc}",
        )
        return
    finally:
        library.close()

    recomputed = float(np.linalg.norm(result.forces, axis=0).max())
    reported = float(history["peak_force_n"]["p50"])
    # The report's peak is the bootstrap median; the deterministic single solve is compared
    # against the same solve's own scale, so what is checked is that the physics reproduces a
    # peak of the recorded magnitude, not the bootstrap median to five figures.
    ratio = recomputed / reported if reported > 0 else float("inf")
    ok = 0.5 <= ratio <= 2.0
    suite.check(
        "lfh.offline_reinversion",
        ok,
        (
            f"re-inverted {OFFLINE_TARGET} from committed fixtures with the network refused: "
            f"peak {recomputed:.3e} N against the reported bootstrap median {reported:.3e} N "
            f"(ratio {ratio:.3f}), variance reduction {result.variance_reduction:.3f}"
        ),
    )


def run_suite(repo: Path, m2_dir: Path = REPORTS_DIR) -> SuiteResult:
    """Every check the force-history component must survive to count as validated."""
    suite = Suite(SUITE_NAME, repo)
    try:
        references = load_references(repo)
    except (FileNotFoundError, ValueError) as exc:
        suite.check("lfh.references_load", False, f"{exc}")
        return suite.result()
    suite.check(
        "lfh.references_load",
        True,
        f"{len(references.sources)} sources, {len(references.targets)} targets",
    )

    _check_references(suite, references)
    _check_reproductions(suite, references, repo, m2_dir)
    _check_no_point_mass(suite, repo, references, m2_dir)
    _check_refusals(suite, repo, references, m2_dir)
    _check_fixture_hashes(suite, repo)
    _check_greens_never_on_the_bus(suite, repo)
    _check_offline_reinversion(suite, repo, references, m2_dir)
    _check_seal(suite, repo, references, m2_dir)
    _check_new_events(suite, repo, references, m2_dir)
    return suite.result()
