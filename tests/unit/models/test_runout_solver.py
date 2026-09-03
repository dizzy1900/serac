"""Verification of `serac-swe-voellmy` against cases with known answers.

These are the tests that decide whether the solver may be trusted to generate an ensemble:
mass conservation on a closed domain, a lake at rest staying at rest (well-balancedness),
the Ritter dam break against its analytic solution, Voellmy-Salm terminal velocity on a
uniform slope, and entrainment conserving mass while decelerating the flow.
"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from serac.models.runout.params import GRAVITY, SolverSettings, VoellmyParameters
from serac.models.runout.solver import (
    VoellmySolver,
    ritter_solution,
    terminal_velocity,
)


def make_parameters(**overrides: float | tuple[float, float]) -> VoellmyParameters:
    base: dict[str, object] = {
        "release_volume_m3": 1.0e7,
        "ice_fraction": 0.4,
        "release_elevation_band_m": (4000.0, 4500.0),
        "entrainment_coefficient": 0.0,
        "mu": 0.15,
        "xi_m_s2": 1000.0,
        "critical_shear_pa": 0.0,
    }
    base.update(overrides)
    return VoellmyParameters.model_validate(base)


def flat_solver(
    shape: tuple[int, int] = (16, 64),
    *,
    dx: float = 10.0,
    bed: np.ndarray | None = None,
    parameters: VoellmyParameters | None = None,
    erodible: np.ndarray | None = None,
    **settings: float | bool,
) -> VoellmySolver:
    ground = np.zeros(shape) if bed is None else bed
    mask = np.ones(shape, dtype=bool)
    opts: dict[str, object] = {"resolution_m": dx, "cfl": 0.45, "max_time_s": 10.0}
    opts.update(settings)
    return VoellmySolver(
        bed=ground,
        domain_mask=mask,
        outflow_mask=np.zeros(shape, dtype=bool),
        erodible_depth=np.zeros(shape) if erodible is None else erodible,
        parameters=parameters or make_parameters(),
        settings=SolverSettings.model_validate(opts),
    )


# -- mass conservation ---------------------------------------------------------------------


def test_mass_is_conserved_on_a_closed_domain() -> None:
    """No outflow, no entrainment: the volume at the end equals the volume at the start."""
    shape = (24, 48)
    rng = np.random.default_rng(11)
    bed = 0.02 * np.arange(shape[1])[None, :] * np.ones((shape[0], 1))
    solver = flat_solver(shape, dx=10.0, bed=bed, max_time_s=40.0, stop_when_dry=False)
    h0 = np.zeros(shape)
    h0[8:16, 4:12] = 3.0 + rng.uniform(0.0, 0.5, size=(8, 8))
    expected = float(h0.sum() * solver.cell_area)

    result = solver.run(h0)

    assert result.outflow_volume_m3 == 0.0
    assert result.entrained_volume_m3 == 0.0
    assert result.final_volume_m3 == pytest.approx(expected, rel=1e-12)
    assert abs(result.mass_balance["relative_error"]) < 1e-12


# -- well-balancedness ---------------------------------------------------------------------


def test_lake_at_rest_is_steady() -> None:
    """A flat free surface over irregular topography must not move (Audusse well-balancing)."""
    shape = (20, 40)
    rng = np.random.default_rng(3)
    bed = rng.uniform(0.0, 4.0, size=shape)
    level = 6.0
    h0 = np.maximum(level - bed, 0.0)
    solver = flat_solver(shape, dx=25.0, bed=bed, max_time_s=200.0, stop_when_dry=False)

    result = solver.run(h0)

    assert result.steps > 20, "the run must actually take steps to be evidence of anything"
    surface = result.final_depth + bed
    wet = result.final_depth > 1e-9
    assert np.abs(surface[wet] - level).max() < 1e-10
    assert result.max_speed.max() < 1e-10


def test_lake_at_rest_with_dry_banks_stays_dry() -> None:
    """Cells whose bed is above the water line must not become wet."""
    shape = (12, 30)
    bed = np.linspace(0.0, 10.0, shape[1])[None, :] * np.ones((shape[0], 1))
    level = 4.0
    h0 = np.maximum(level - bed, 0.0)
    solver = flat_solver(shape, dx=20.0, bed=bed, max_time_s=100.0, stop_when_dry=False)

    result = solver.run(h0)

    dry_initially = h0 <= 0.0
    assert result.final_depth[dry_initially].max() < 1e-10
    assert np.abs((result.final_depth + bed)[h0 > 0] - level).max() < 1e-10


# -- Ritter dam break ----------------------------------------------------------------------


@pytest.mark.parametrize(("n_cells", "tolerance"), [(200, 0.10), (400, 0.07)])
def test_ritter_dam_break_matches_the_analytic_solution(n_cells: int, tolerance: float) -> None:
    """Frictionless dry-bed dam break against Ritter (1892)."""
    length = 1000.0
    dx = length / n_cells
    shape = (3, n_cells)
    h0_depth = 10.0
    t_end = 12.0

    solver = VoellmySolver(
        bed=np.zeros(shape),
        domain_mask=np.ones(shape, dtype=bool),
        outflow_mask=np.zeros(shape, dtype=bool),
        erodible_depth=np.zeros(shape),
        parameters=make_parameters(mu=1e-9, xi_m_s2=1e12),
        settings=SolverSettings(
            resolution_m=dx,
            cfl=0.45,
            max_time_s=t_end,
            dry_depth_m=1e-4,
            stop_when_dry=False,
            stop_kinetic_fraction=0.0,
        ),
    )
    centres = (np.arange(n_cells) + 0.5) * dx - length / 2.0
    h0 = np.where(centres < 0.0, h0_depth, 0.0)[None, :] * np.ones((shape[0], 1))

    result = solver.run(h0)

    analytic, _ = ritter_solution(centres, result.time_s, h0_depth)
    numeric = result.final_depth[1]
    # compare inside the wave fan only; the reservoir end is a wall, not part of Ritter
    c0 = math.sqrt(GRAVITY * h0_depth)
    inside = (centres > -0.9 * c0 * result.time_s) & (centres < 2.2 * c0 * result.time_s)
    l1 = float(np.abs(numeric[inside] - analytic[inside]).sum() * dx)
    scale = float(np.abs(analytic[inside]).sum() * dx)
    relative = l1 / scale
    # first order with a Rusanov viscosity, so the tolerance is set from the measured error
    # and the convergence test below is what proves the scheme is not merely diffusive
    assert relative < tolerance, f"Ritter L1 relative error {relative:.4f} at n={n_cells}"


def test_ritter_error_falls_under_refinement() -> None:
    """First order or not, the scheme must converge; a flat error curve means a bug."""
    errors: list[float] = []
    for n_cells in (100, 200, 400):
        length, h0_depth, t_end = 1000.0, 10.0, 12.0
        dx = length / n_cells
        shape = (3, n_cells)
        solver = VoellmySolver(
            bed=np.zeros(shape),
            domain_mask=np.ones(shape, dtype=bool),
            outflow_mask=np.zeros(shape, dtype=bool),
            erodible_depth=np.zeros(shape),
            parameters=make_parameters(mu=1e-9, xi_m_s2=1e12),
            settings=SolverSettings(
                resolution_m=dx,
                cfl=0.45,
                max_time_s=t_end,
                dry_depth_m=1e-4,
                stop_when_dry=False,
                stop_kinetic_fraction=0.0,
            ),
        )
        centres = (np.arange(n_cells) + 0.5) * dx - length / 2.0
        h0 = np.where(centres < 0.0, h0_depth, 0.0)[None, :] * np.ones((shape[0], 1))
        result = solver.run(h0)
        analytic, _ = ritter_solution(centres, result.time_s, h0_depth)
        c0 = math.sqrt(GRAVITY * h0_depth)
        inside = (centres > -0.9 * c0 * result.time_s) & (centres < 2.2 * c0 * result.time_s)
        num = result.final_depth[1]
        errors.append(float(np.abs(num[inside] - analytic[inside]).sum() * dx))
    assert errors[1] < errors[0], f"no convergence 100 -> 200: {errors}"
    assert errors[2] < errors[1], f"no convergence 200 -> 400: {errors}"


# -- Voellmy-Salm terminal velocity ---------------------------------------------------------


def _uniform_slope_solver(
    slope_deg: float,
    mu: float,
    xi: float,
    n_cells: int = 400,
    dx: float = 20.0,
    max_time_s: float = 400.0,
) -> tuple[VoellmySolver, tuple[int, int]]:
    slope = math.radians(slope_deg)
    shape = (3, n_cells)
    x = np.arange(n_cells) * dx
    bed = ((x.max() - x) * math.tan(slope))[None, :] * np.ones((shape[0], 1))
    solver = VoellmySolver(
        bed=bed,
        domain_mask=np.ones(shape, dtype=bool),
        outflow_mask=np.zeros(shape, dtype=bool),
        erodible_depth=np.zeros(shape),
        parameters=make_parameters(mu=mu, xi_m_s2=xi),
        settings=SolverSettings(
            resolution_m=dx,
            cfl=0.4,
            max_time_s=max_time_s,
            stop_when_dry=False,
            stop_kinetic_fraction=0.0,
        ),
    )
    return solver, shape


def _terminal_velocity_error(cfl: float) -> float:
    """Relative error of the settled centre speed against the analytic Voellmy value."""
    slope_deg, depth, mu, xi = 20.0, 2.0, 0.2, 800.0
    slope = math.radians(slope_deg)
    expected = terminal_velocity(depth, slope, mu, xi)
    n_cells, dx = 400, 20.0
    shape = (3, n_cells)
    x = np.arange(n_cells) * dx
    bed = ((x.max() - x) * math.tan(slope))[None, :] * np.ones((shape[0], 1))
    solver = VoellmySolver(
        bed=bed,
        domain_mask=np.ones(shape, dtype=bool),
        outflow_mask=np.zeros(shape, dtype=bool),
        erodible_depth=np.zeros(shape),
        parameters=make_parameters(mu=mu, xi_m_s2=xi),
        settings=SolverSettings(
            resolution_m=dx,
            cfl=cfl,
            max_time_s=30.0,
            stop_when_dry=False,
            stop_kinetic_fraction=0.0,
        ),
    )
    centre = n_cells // 2
    samples: list[float] = []

    def observe(t: float, dt: float, h: np.ndarray, u: np.ndarray, v: np.ndarray) -> None:
        samples.append(float(np.hypot(u, v)[1, centre]))

    # launched exactly at the analytic terminal velocity, so this measures drift, not spin-up
    solver.run(
        np.full(shape, depth), initial_hu=np.full(shape, depth * expected), observers=[observe]
    )
    settled = np.array(samples[len(samples) // 4 :])
    return float(abs(settled.mean() - expected) / expected)


def test_voellmy_terminal_velocity_converges_first_order_in_dt() -> None:
    """A sheet launched at the analytic terminal velocity holds it, to first order in `dt`.

    Gravity and friction are applied as separate operators inside a step, so the balance is
    only recovered to O(dt): at the production CFL of 0.45 the settled speed sits **7.6% below**
    the analytic value, and the error halves every time the step does. That is a real bias in
    the ensemble's arrival times, not a rounding detail, and it is recorded in the model card
    and in `reports/runout/verification.json` rather than tuned away.
    """
    errors = {cfl: _terminal_velocity_error(cfl) for cfl in (0.4, 0.2, 0.1, 0.05)}

    ordered = [errors[c] for c in (0.4, 0.2, 0.1, 0.05)]
    for coarse, fine in itertools.pairwise(ordered):
        assert fine < coarse, f"terminal velocity error did not fall with dt: {errors}"
        ratio = coarse / max(fine, 1e-12)
        assert 1.6 < ratio < 2.6, f"expected first order, got ratio {ratio:.2f}: {errors}"
    assert errors[0.05] < 0.01, f"error at CFL 0.05 is {errors[0.05]:.3%}, expected under 1%"


def test_uniform_flow_spins_up_towards_the_terminal_velocity() -> None:
    """From rest, a uniform sheet must accelerate monotonically towards the Voellmy speed."""
    slope_deg, depth, mu, xi = 20.0, 2.0, 0.2, 800.0
    solver, shape = _uniform_slope_solver(slope_deg, mu, xi)
    expected = terminal_velocity(depth, math.radians(slope_deg), mu, xi)
    centre = shape[1] // 2
    trace: list[tuple[float, float]] = []

    def observe(t: float, dt: float, h: np.ndarray, u: np.ndarray, v: np.ndarray) -> None:
        trace.append((t, float(np.hypot(u, v)[1, centre])))

    solver.run(np.full(shape, depth), observers=[observe])

    early = [s for t, s in trace if t <= 60.0]
    assert early[0] < early[-1], "the sheet never accelerated"
    peak = max(early)
    # the sheet is finite, so the free surface adjusts as it spreads and the centre reaches a
    # little under the analytic value before end effects arrive; 15% is the honest bound
    assert peak == pytest.approx(expected, rel=0.15), (
        f"spin-up peak {peak:.3f} m/s vs analytic {expected:.3f} m/s"
    )


def test_terminal_velocity_is_zero_when_friction_exceeds_gravity() -> None:
    """`mu > tan(theta)` means the flow cannot be sustained; the formula must say so."""
    assert terminal_velocity(2.0, math.radians(10.0), mu=0.5, xi=1000.0) == 0.0


# -- entrainment ----------------------------------------------------------------------------


def test_entrainment_conserves_mass() -> None:
    """Everything the flow picks up must leave the bed: no volume is created or destroyed."""
    shape = (12, 60)
    dx = 20.0
    slope = math.radians(18.0)
    x = np.arange(shape[1]) * dx
    bed = ((x.max() - x) * math.tan(slope))[None, :] * np.ones((shape[0], 1))
    erodible = np.full(shape, 2.0)
    solver = VoellmySolver(
        bed=bed,
        domain_mask=np.ones(shape, dtype=bool),
        outflow_mask=np.zeros(shape, dtype=bool),
        erodible_depth=erodible,
        parameters=make_parameters(entrainment_coefficient=0.02, critical_shear_pa=0.0),
        settings=SolverSettings(resolution_m=dx, cfl=0.4, max_time_s=120.0, stop_when_dry=False),
    )
    h0 = np.zeros(shape)
    h0[4:8, 2:8] = 5.0
    initial = float(h0.sum() * solver.cell_area)
    bed_initial = float(erodible.sum() * solver.cell_area)

    result = solver.run(h0)

    assert result.entrained_volume_m3 > 0.0, "the test must actually entrain something"
    # flow volume gained exactly what the bed lost
    assert result.final_volume_m3 == pytest.approx(initial + result.entrained_volume_m3, rel=1e-10)
    assert result.entrained_volume_m3 <= bed_initial + 1e-6


def test_entrainment_cannot_over_draw_the_bed() -> None:
    """A thin mantle under a fast flow bounds the entrained volume by what was there."""
    shape = (8, 40)
    dx = 20.0
    slope = math.radians(25.0)
    x = np.arange(shape[1]) * dx
    bed = ((x.max() - x) * math.tan(slope))[None, :] * np.ones((shape[0], 1))
    erodible = np.full(shape, 0.05)
    available = float(erodible.sum() * dx * dx)
    solver = VoellmySolver(
        bed=bed,
        domain_mask=np.ones(shape, dtype=bool),
        outflow_mask=np.zeros(shape, dtype=bool),
        erodible_depth=erodible,
        parameters=make_parameters(entrainment_coefficient=0.9, critical_shear_pa=0.0),
        settings=SolverSettings(resolution_m=dx, cfl=0.4, max_time_s=200.0, stop_when_dry=False),
    )
    h0 = np.zeros(shape)
    h0[2:6, 1:6] = 8.0

    result = solver.run(h0)

    assert result.entrained_volume_m3 <= available + 1e-6


def test_entrainment_decelerates_the_flow() -> None:
    """Bed material arrives at rest, so picking it up costs velocity at constant momentum.

    Stated on the operator rather than on a whole run, because over a whole run entrainment
    also *deepens* the flow, and a Voellmy terminal velocity rises with depth -- so a bulking
    flow legitimately ends up faster overall. The momentum cost is the local claim, and this is
    the local test.
    """
    shape = (4, 8)
    dx = 20.0
    solver = VoellmySolver(
        bed=np.zeros(shape),
        domain_mask=np.ones(shape, dtype=bool),
        outflow_mask=np.zeros(shape, dtype=bool),
        erodible_depth=np.full(shape, 3.0),
        parameters=make_parameters(entrainment_coefficient=0.05, critical_shear_pa=0.0),
        settings=SolverSettings(resolution_m=dx, cfl=0.4, max_time_s=10.0),
    )
    h = np.full(shape, 4.0)
    u = np.full(shape, 12.0)
    v = np.zeros(shape)
    momentum = h * u

    de = solver.entrainment_depth(h, u, v, np.full(shape, 3.0), dt=0.5)

    assert de.min() > 0.0, "the test must actually entrain something"
    u_after = momentum / (h + de)
    assert np.all(u_after < u), "entrainment must not accelerate the flow"


def test_entrainment_stops_when_the_shear_is_below_critical() -> None:
    """`tau_c` above the basal shear switches entrainment off entirely."""
    shape = (4, 8)
    solver = VoellmySolver(
        bed=np.zeros(shape),
        domain_mask=np.ones(shape, dtype=bool),
        outflow_mask=np.zeros(shape, dtype=bool),
        erodible_depth=np.full(shape, 3.0),
        parameters=make_parameters(entrainment_coefficient=0.05, critical_shear_pa=1.0e9),
        settings=SolverSettings(resolution_m=20.0, cfl=0.4, max_time_s=10.0),
    )
    de = solver.entrainment_depth(
        np.full(shape, 4.0), np.full(shape, 12.0), np.zeros(shape), np.full(shape, 3.0), dt=0.5
    )
    assert de.max() == 0.0


# -- mask and outflow ------------------------------------------------------------------------


def test_mask_walls_do_not_leak() -> None:
    """A closed mask is a wall: nothing crosses it however hard the flow pushes."""
    shape = (20, 40)
    mask = np.zeros(shape, dtype=bool)
    mask[:, :20] = True
    bed = np.zeros(shape)
    solver = VoellmySolver(
        bed=bed,
        domain_mask=mask,
        outflow_mask=np.zeros(shape, dtype=bool),
        erodible_depth=np.zeros(shape),
        parameters=make_parameters(mu=1e-9, xi_m_s2=1e12),
        settings=SolverSettings(resolution_m=10.0, cfl=0.45, max_time_s=60.0, stop_when_dry=False),
    )
    h0 = np.zeros(shape)
    h0[:, :10] = 5.0
    h0 *= mask
    expected = float(h0.sum() * solver.cell_area)

    result = solver.run(h0)

    assert result.final_depth[~mask].max() == 0.0
    assert result.final_volume_m3 == pytest.approx(expected, rel=1e-12)


def test_outflow_removes_mass_and_the_balance_still_closes() -> None:
    shape = (10, 60)
    outflow = np.zeros(shape, dtype=bool)
    outflow[:, -1] = True
    # 1.2 km at 12 deg. An earlier version used a 1.4 deg slope, where tan(theta) = 0.025 is
    # below mu = 0.05, so the flow could not start and the test was vacuous.
    bed = (np.arange(shape[1])[::-1] * 20.0 * math.tan(math.radians(12.0)))[None, :] * np.ones(
        (shape[0], 1)
    )
    solver = VoellmySolver(
        bed=bed,
        domain_mask=np.ones(shape, dtype=bool),
        outflow_mask=outflow,
        erodible_depth=np.zeros(shape),
        parameters=make_parameters(mu=0.05, xi_m_s2=4000.0),
        settings=SolverSettings(resolution_m=20.0, cfl=0.4, max_time_s=300.0, stop_when_dry=True),
    )
    h0 = np.zeros(shape)
    h0[3:7, 1:6] = 6.0
    initial = float(h0.sum() * solver.cell_area)

    result = solver.run(h0)

    assert result.outflow_volume_m3 > 0.0
    assert abs(result.mass_balance["relative_error"]) < 1e-10
    assert result.initial_volume_m3 == pytest.approx(initial, rel=1e-12)
