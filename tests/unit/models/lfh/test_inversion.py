"""The inversion, proved by recovering a force history it was never told about.

The test that matters here is the synthetic round trip: take a known three-component force,
push it through the same forward operator the inversion uses, add noise, invert, and check the
answer comes back. It is the only test that catches the whole class of bugs where the physics
is right but the bookkeeping is not -- and one of those bugs was real.

**The timing offset.** `greens_shift_samples` aligns the data window with the force series. It
was first written as `-(window_before + source_lead)` rather than
`-(window_before - source_lead)`, a 120-sample error. Nothing crashed. The inversion still
returned a smooth, plausible force history with a well-formed L-curve and an honest-looking
envelope -- and a peak force two orders of magnitude too large, because amplitude is what a
least-squares fit reaches for when it cannot match phase. Variance reduction fell to 0.11,
which is the only place it showed. `test_shift_recovers_known_force` and
`test_wrong_shift_destroys_the_fit` pin both the correct value and the symptom of the wrong
one.
"""

from __future__ import annotations

import numpy as np
import pytest

from serac.models.lfh.config import LfhConfig
from serac.models.lfh.inversion import (
    TraceKernel,
    accumulate,
    boxcar_kernels,
    design_matrix,
    free_index,
    invert,
    l_curve_corner,
    second_difference_operator,
    solve_normal,
)


def _kernels(rng: np.random.Generator, length: int) -> np.ndarray:
    """A smooth, causal, band-limited stand-in for a Green's function triple."""
    t = np.arange(length)
    out = np.zeros((3, length))
    for k in range(3):
        arrival = 60 + 25 * k
        envelope = np.exp(-((t - arrival) ** 2) / (2 * 40.0**2))
        out[k] = envelope * np.sin(2 * np.pi * (t - arrival) / (45.0 + 8 * k))
        out[k] *= (1.0 + 0.3 * k) * 1e-18
    out += 1e-21 * rng.normal(size=out.shape)
    out[:, :20] = 0.0  # causality: nothing before the wave arrives
    return out


def _known_force(n: int) -> np.ndarray:
    """A single acceleration-then-deceleration pulse, zero at both ends."""
    t = np.arange(n)
    shape = np.exp(-((t - n * 0.35) ** 2) / (2 * (n * 0.12) ** 2)) - 0.7 * np.exp(
        -((t - n * 0.62) ** 2) / (2 * (n * 0.15) ** 2)
    )
    shape[0] = shape[-1] = 0.0
    return np.vstack([-0.45 * shape, 1.0 * shape, 0.35 * shape]) * 1.4e11


def _synthetic_case(
    *, shift: int, n_time: int = 500, n_basis: int = 201, n_traces: int = 9, noise: float = 0.02
) -> tuple[list[TraceKernel], np.ndarray]:
    rng = np.random.default_rng(7)
    force = _known_force(n_basis)
    traces: list[TraceKernel] = []
    for index in range(n_traces):
        kernels = _kernels(np.random.default_rng(100 + index), n_time + n_basis)
        block = design_matrix(
            kernels, n_time=n_time, n_basis=n_basis, stride=1, shift=shift, dt=1.0
        )
        clean = block @ force.reshape(-1)
        scale = float(np.abs(clean).max())
        data = clean + noise * scale * rng.normal(size=clean.size)
        traces.append(
            TraceKernel(
                key=f"XX.S{index:02d}..LHZ",
                component="Z",
                data=data,
                kernels=kernels,
                weight=1.0 / max(float(np.sqrt(np.mean(data**2))), 1e-30),
            )
        )
    return traces, force


# --- the round trip -------------------------------------------------------------------------


def test_shift_recovers_known_force() -> None:
    """With the right alignment the inversion recovers the force it was never shown.

    Two solutions are checked because they say different things. Lightly regularised, the
    operator is unbiased: the peak comes back at about 1.09x and the shape correlates at
    r = 0.81, which is what proves the forward and inverse bookkeeping agree. At the L-curve
    corner the same data give 0.36x and r = 0.41 -- the corner criterion over-smooths on a
    well-conditioned, white-noise problem where the residual barely rises with lambda.

    That bias is kept and reported rather than tuned away. The brief specifies the L-curve
    corner; changing the criterion after measuring how it behaves is the kind of adjustment
    this repository exists to avoid, and the honest response is to state the number in the
    model card and let the lambda jitter in the bootstrap carry part of it into the interval.
    """
    shift = -60
    traces, force = _synthetic_case(shift=shift)
    true_peak = float(np.linalg.norm(force, axis=0).max())

    light = invert(traces, n_basis=201, stride=1, shift=shift, dt=1.0, lambda_value=0.3)
    light_peak = float(np.linalg.norm(light.forces, axis=0).max())
    light_correlation = float(np.corrcoef(light.forces.reshape(-1), force.reshape(-1))[0, 1])
    assert light.variance_reduction > 0.95
    assert 0.8 < light_peak / true_peak < 1.3, (
        f"the operator itself must be unbiased; peak off by {light_peak / true_peak:.2f}x"
    )
    assert light_correlation > 0.75, f"recovered shape r={light_correlation:.3f}"

    corner = invert(traces, n_basis=201, stride=1, shift=shift, dt=1.0)
    corner_peak = float(np.linalg.norm(corner.forces, axis=0).max())
    assert corner.variance_reduction > 0.95
    assert 0.2 < corner_peak / true_peak < 0.7, (
        "the L-curve corner is expected to damp the peak on this problem; if this ratio moves "
        f"the model card's stated bias is stale (measured {corner_peak / true_peak:.2f}x)"
    )
    assert corner_peak < light_peak, "the corner must be the more heavily damped of the two"


def test_wrong_shift_destroys_the_fit() -> None:
    """The 120-sample misalignment that shipped once: no crash, a wrecked fit, a wrong force.

    This is the diagnostic signature to recognise. Variance reduction collapses from 1.00 to
    0.38 while the returned force stays smooth, plausibly shaped and roughly the right size,
    so nothing about the output announces the error. On real records it was worse: variance
    reduction fell to 0.11 and the peak force came back two orders of magnitude too large,
    because least squares compensates for phase it cannot match by reaching for amplitude.
    """
    traces, force = _synthetic_case(shift=-60)
    misaligned = invert(traces, n_basis=201, stride=1, shift=-180, dt=1.0)
    aligned = invert(traces, n_basis=201, stride=1, shift=-60, dt=1.0)

    assert misaligned.variance_reduction < 0.6 < aligned.variance_reduction, (
        "the only place a misalignment shows is the variance reduction"
    )
    misaligned_correlation = float(
        np.corrcoef(misaligned.forces.reshape(-1), force.reshape(-1))[0, 1]
    )
    assert misaligned_correlation < 0.4, (
        f"a misaligned solution should not track the true force; r={misaligned_correlation:.3f}"
    )
    assert float(np.linalg.norm(misaligned.forces, axis=0).max()) > 0, (
        "and it still returns a perfectly presentable force history, which is the danger"
    )


def test_config_shift_is_the_lead_difference() -> None:
    """`greens_shift_samples` is `-(window_before - source_lead)`, not their sum."""
    config = LfhConfig()
    assert config.greens_shift_samples == -round(
        (config.window_before_s - config.source_lead_s) / config.dt_s
    )
    assert config.greens_shift_samples == -60
    widened = config.model_copy(update={"window_before_s": 200.0, "source_lead_s": 50.0})
    assert widened.greens_shift_samples == -150


def test_coarse_basis_recovers_the_same_force() -> None:
    """The gSF search runs on a coarse basis; it must not change the answer's scale."""
    shift = -60
    traces, force = _synthetic_case(shift=shift)
    fine = invert(traces, n_basis=201, stride=1, shift=shift, dt=1.0)
    coarse = invert(traces, n_basis=41, stride=5, shift=shift, dt=1.0)
    fine_peak = float(np.linalg.norm(fine.forces, axis=0).max())
    coarse_peak = float(np.linalg.norm(coarse.forces, axis=0).max())
    assert 0.5 < coarse_peak / fine_peak < 2.0
    assert coarse.forces.shape[1] == 41 * 5
    assert float(np.linalg.norm(force, axis=0).max()) > 0


# --- the pieces -----------------------------------------------------------------------------


def test_zero_endpoints_are_exactly_zero() -> None:
    """A mass movement starts and ends at rest, so the first and last samples are pinned."""
    shift = -60
    traces, _ = _synthetic_case(shift=shift)
    result = invert(traces, n_basis=201, stride=1, shift=shift, dt=1.0, zero_endpoints=True)
    np.testing.assert_allclose(result.forces[:, 0], 0.0, atol=0.0)
    np.testing.assert_allclose(result.forces[:, -1], 0.0, atol=0.0)


def test_free_index_drops_only_the_endpoints() -> None:
    free = free_index(10, zero_endpoints=True)
    assert free.size == 3 * 8
    assert set(free.tolist()).isdisjoint({0, 9, 10, 19, 20, 29})


def test_second_difference_operator_annihilates_a_straight_line() -> None:
    """The penalty must be blind to a ramp and awake to curvature."""
    operator = second_difference_operator(12, zero_endpoints=False)
    line = np.arange(12, dtype=float)
    np.testing.assert_allclose(operator @ line, 0.0, atol=1e-12)
    curve = line**2
    assert float(np.abs(operator @ curve).max()) > 0.5


def test_boxcar_kernels_sum_consecutive_lags() -> None:
    kernels = np.zeros((3, 8))
    kernels[:, 0] = 1.0
    summed = boxcar_kernels(kernels, 3)
    np.testing.assert_allclose(summed[0, :4], [1.0, 1.0, 1.0, 0.0])
    np.testing.assert_allclose(boxcar_kernels(kernels, 1), kernels)


def test_design_matrix_is_causal() -> None:
    """Green's indices before zero contribute nothing: the ground has not moved yet."""
    kernels = np.ones((3, 50))
    block = design_matrix(kernels, n_time=20, n_basis=10, stride=1, shift=-15, dt=1.0)
    # Force sample 0 acts 15 samples after the window starts, so the first 15 rows are empty.
    np.testing.assert_allclose(block[:15, 0], 0.0)
    assert float(np.abs(block[15:, 0]).max()) > 0


def test_residual_norm_matches_an_explicit_computation() -> None:
    """`NormalEquations.residual_norm_sq` must equal `||d - A x||^2` computed the long way."""
    shift = -60
    traces, _ = _synthetic_case(shift=shift, n_time=200, n_basis=61, n_traces=3)
    normal = accumulate(traces, n_basis=61, stride=1, shift=shift, dt=1.0)
    rng = np.random.default_rng(5)
    x = rng.normal(size=3 * 61) * 1e10
    explicit = 0.0
    for trace in traces:
        block = design_matrix(
            trace.kernels, n_time=trace.data.size, n_basis=61, stride=1, shift=shift, dt=1.0
        )
        residual = (trace.data - block @ x) * trace.weight
        explicit += float(residual @ residual)
    assert normal.residual_norm_sq(x) == pytest.approx(explicit, rel=1e-8)


def test_variance_reduction_is_one_for_a_perfect_fit() -> None:
    shift = -60
    traces, force = _synthetic_case(shift=shift, n_time=200, n_basis=61, n_traces=3, noise=0.0)
    normal = accumulate(traces, n_basis=61, stride=1, shift=shift, dt=1.0)
    assert normal.variance_reduction(force.reshape(-1)) == pytest.approx(1.0, abs=1e-9)


def test_l_curve_corner_finds_the_knee() -> None:
    """A synthetic L shape: the corner is the interior point of maximum curvature."""
    lambdas = np.geomspace(1e-3, 1e3, 41)
    residual = np.sqrt(1.0 + lambdas**4)
    solution = 1.0 / np.sqrt(1.0 + lambdas**4)
    index, curvature = l_curve_corner(lambdas, residual, solution)
    assert 0 < index < lambdas.size - 1
    assert curvature.shape == lambdas.shape


def test_lambda_is_recorded_and_the_curve_is_kept() -> None:
    shift = -60
    traces, _ = _synthetic_case(shift=shift, n_time=200, n_basis=61, n_traces=4)
    result = invert(traces, n_basis=61, stride=1, shift=shift, dt=1.0, n_lambda=15)
    assert result.l_curve is not None
    assert result.l_curve.lambdas.size == 15
    assert result.lambda_value == pytest.approx(result.l_curve.corner_lambda)
    fixed = solve_normal(
        accumulate(traces, n_basis=61, stride=1, shift=shift, dt=1.0),
        stride=1,
        lambda_value=result.lambda_value,
    )
    assert fixed.l_curve is None
    np.testing.assert_allclose(fixed.forces, result.forces, rtol=1e-9)


def test_more_regularisation_smooths_and_shrinks() -> None:
    shift = -60
    traces, _ = _synthetic_case(shift=shift, n_time=200, n_basis=61, n_traces=4)
    normal = accumulate(traces, n_basis=61, stride=1, shift=shift, dt=1.0)
    light = solve_normal(normal, stride=1, lambda_value=1e-3)
    heavy = solve_normal(normal, stride=1, lambda_value=1e2)
    assert heavy.solution_norm < light.solution_norm
    assert heavy.residual_norm > light.residual_norm
    assert heavy.variance_reduction <= light.variance_reduction
