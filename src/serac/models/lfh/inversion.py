"""Least-squares inversion for a three-component force history.

The forward problem is a convolution. A mass movement exerts a time-varying single force on
the Earth; each station records that force convolved with the Green's function for its
distance and azimuth. Written discretely, for station-component `i` at data sample `n`:

    s_i[n] = dt * sum_k sum_j  g_ik[n - j + shift] * f_k[j]

with `k` running over (up, north, east), `j` over the force samples, and `shift` accounting
for the data window starting before the origin while the force starts a little after it. That
is linear in `f`, so the whole thing is one matrix `A` and one vector `d`.

Three choices make the result something other than a curve fit:

**Zero endpoints.** A mass movement starts at rest and ends at rest, so the net force it
exerts is zero at both ends of the window. Dropping the first and last basis coefficients
imposes that exactly, rather than leaving the inversion free to add a ramp that fits noise.

**Second-difference Tikhonov.** The unregularised problem is ill-conditioned: nearby force
samples trade off against each other almost freely. Penalising the second difference asks for
the smoothest force history consistent with the data, which is the right prior for a
decelerating mass and the wrong one for a spike.

**Lambda from the L-curve corner, recorded.** The regularisation weight is not tuned to make
an answer look right. It is chosen by the standard maximum-curvature criterion on the
(log residual norm, log solution norm) trade-off curve, and the chosen value plus the whole
curve are reported so a reader can see how sharp the corner was.

The gSF grid search calls this many times, so the force is parameterised on a coarse
piecewise-constant basis during the search (`stride > 1`) and on the full sample grid for the
final inversion at the chosen node.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class TraceKernel:
    """One station-component: the observed trace and its three Green's-function columns.

    `kernels` is `(3, L)` -- the displacement this channel would record per newton of force
    along (up, north, east), already rotated to the channel's own component and band-passed
    with the same filter as the data, so filtering and convolution commute.
    """

    key: str
    component: str
    data: np.ndarray
    kernels: np.ndarray
    weight: float = 1.0
    distance_deg: float = 0.0
    azimuth_deg: float = 0.0

    def __post_init__(self) -> None:
        if self.kernels.ndim != 2 or self.kernels.shape[0] != 3:
            raise ValueError(f"kernels must be (3, L); got {self.kernels.shape}")
        if self.data.ndim != 1:
            raise ValueError("data must be one-dimensional")


@dataclass
class NormalEquations:
    """`A^T A`, `A^T d` and `d^T d`, accumulated trace by trace.

    Kept rather than `A` itself because the grid search solves the same system at 121 nodes
    and only ever needs the residual norm, which falls straight out of these three.
    """

    ata: np.ndarray
    atd: np.ndarray
    dtd: float
    n_data: int
    n_basis: int

    def residual_norm_sq(self, x: np.ndarray) -> float:
        """`||d - A x||^2` without forming `A x`."""
        value = self.dtd - 2.0 * float(self.atd @ x) + float(x @ (self.ata @ x))
        return max(value, 0.0)

    def variance_reduction(self, x: np.ndarray) -> float:
        if self.dtd <= 0:
            return 0.0
        return float(np.clip(1.0 - self.residual_norm_sq(x) / self.dtd, 0.0, 1.0))


def boxcar_kernels(kernels: np.ndarray, stride: int) -> np.ndarray:
    """Sum `stride` consecutive lags, so one coefficient drives a piecewise-constant force.

    A force held constant over `stride` samples contributes the sum of `stride` successively
    lagged Green's functions. Precomputing that sum turns the coarse basis into plain shifts
    of a single kernel, which is what `design_matrix` needs.
    """
    if stride == 1:
        return kernels
    out = np.zeros_like(kernels)
    for p in range(stride):
        if p == 0:
            out += kernels
        else:
            out[:, p:] += kernels[:, :-p]
    return out


def design_matrix(
    kernels: np.ndarray, *, n_time: int, n_basis: int, stride: int, shift: int, dt: float
) -> np.ndarray:
    """`(n_time, 3 * n_basis)` design block for one trace.

    Column `k * n_basis + j` is the response to a unit force on component `k` held over force
    samples `[j*stride, (j+1)*stride)`. Green's-function indices outside `[0, L)` are zero,
    which is causality: the ground has not moved before the force acted.
    """
    summed = boxcar_kernels(kernels, stride)
    length = summed.shape[1]
    block = np.zeros((n_time, 3 * n_basis), dtype=float)
    times = np.arange(n_time)
    for j in range(n_basis):
        index = times - j * stride + shift
        valid = (index >= 0) & (index < length)
        if not valid.any():
            continue
        rows = np.nonzero(valid)[0]
        cols = index[valid]
        for k in range(3):
            block[rows, k * n_basis + j] = summed[k, cols] * dt
    return block


def accumulate(
    traces: list[TraceKernel], *, n_basis: int, stride: int, shift: int, dt: float
) -> NormalEquations:
    """Build the normal equations from every trace, each scaled by its weight."""
    size = 3 * n_basis
    ata = np.zeros((size, size), dtype=float)
    atd = np.zeros(size, dtype=float)
    dtd = 0.0
    n_data = 0
    for trace in traces:
        block = design_matrix(
            trace.kernels,
            n_time=trace.data.size,
            n_basis=n_basis,
            stride=stride,
            shift=shift,
            dt=dt,
        )
        weighted = block * trace.weight
        data = trace.data * trace.weight
        ata += weighted.T @ weighted
        atd += weighted.T @ data
        dtd += float(data @ data)
        n_data += trace.data.size
    return NormalEquations(ata=ata, atd=atd, dtd=dtd, n_data=n_data, n_basis=n_basis)


def second_difference_operator(n_basis: int, *, zero_endpoints: bool) -> np.ndarray:
    """Second-difference matrix over the free coefficients of one force component.

    With `zero_endpoints`, the coefficient vector is `[0, x_1, ..., x_{n-2}, 0]`: the operator
    still spans the full series -- so the smoothness constraint reaches the ends -- but its
    columns are restricted to the free coefficients.
    """
    rows = []
    for i in range(1, n_basis - 1):
        row = np.zeros(n_basis)
        row[i - 1], row[i], row[i + 1] = 1.0, -2.0, 1.0
        rows.append(row)
    full = np.vstack(rows) if rows else np.zeros((0, n_basis))
    if zero_endpoints:
        return full[:, 1 : n_basis - 1]
    return full


def free_index(n_basis: int, *, zero_endpoints: bool) -> np.ndarray:
    """Indices into the `3 * n_basis` coefficient vector that the inversion actually solves for."""
    per = (
        np.arange(1, n_basis - 1)
        if zero_endpoints
        else np.arange(n_basis)  # pragma: no cover - configuration keeps zero_endpoints on
    )
    return np.concatenate([per + k * n_basis for k in range(3)])


@dataclass
class LCurve:
    """The regularisation trade-off, kept so the choice of lambda can be inspected."""

    lambdas: np.ndarray
    residual_norms: np.ndarray
    solution_norms: np.ndarray
    curvature: np.ndarray
    corner_index: int

    @property
    def corner_lambda(self) -> float:
        return float(self.lambdas[self.corner_index])

    def as_dict(self) -> dict[str, list[float]]:
        return {
            "lambda": self.lambdas.tolist(),
            "residual_norm": self.residual_norms.tolist(),
            "solution_norm": self.solution_norms.tolist(),
            "curvature": self.curvature.tolist(),
        }


@dataclass
class InversionResult:
    """A solved force history on the basis grid."""

    coefficients: np.ndarray
    forces: np.ndarray
    variance_reduction: float
    lambda_value: float
    residual_norm: float
    solution_norm: float
    l_curve: LCurve | None = None
    diagnostics: dict[str, float] = field(default_factory=dict)


class _Solver:
    """Holds the reduced normal equations and the scaled smoothing operator."""

    def __init__(self, normal: NormalEquations, *, zero_endpoints: bool) -> None:
        self.normal = normal
        self.free = free_index(normal.n_basis, zero_endpoints=zero_endpoints)
        self.ata = normal.ata[np.ix_(self.free, self.free)]
        self.atd = normal.atd[self.free]
        block = second_difference_operator(normal.n_basis, zero_endpoints=zero_endpoints)
        n_free_per = block.shape[1]
        ltl_block = block.T @ block
        size = 3 * n_free_per
        ltl = np.zeros((size, size))
        for k in range(3):
            lo = k * n_free_per
            ltl[lo : lo + n_free_per, lo : lo + n_free_per] = ltl_block
        # Scale so lambda means the same thing whatever the data amplitude and window length.
        trace_a = float(np.trace(self.ata))
        trace_l = float(np.trace(ltl))
        self.scale = (trace_a / trace_l) if trace_l > 0 and trace_a > 0 else 1.0
        self.ltl = ltl * self.scale
        self.ridge = 1e-12 * (trace_a / max(size, 1) or 1.0)

    def solve(self, lam: float) -> np.ndarray:
        matrix = self.ata + (lam**2) * self.ltl
        matrix = matrix + self.ridge * np.eye(matrix.shape[0])
        try:
            solution: np.ndarray = np.linalg.solve(matrix, self.atd)
        except np.linalg.LinAlgError:  # pragma: no cover - singular only on degenerate input
            solution = np.linalg.lstsq(matrix, self.atd, rcond=None)[0]
        return solution

    def expand(self, reduced: np.ndarray) -> np.ndarray:
        full = np.zeros(3 * self.normal.n_basis)
        full[self.free] = reduced
        return full

    def norms(self, reduced: np.ndarray) -> tuple[float, float]:
        full = self.expand(reduced)
        residual = float(np.sqrt(self.normal.residual_norm_sq(full)))
        smooth = float(np.sqrt(max(reduced @ (self.ltl @ reduced), 0.0)))
        return residual, smooth


def l_curve_corner(
    lambdas: np.ndarray, residual_norms: np.ndarray, solution_norms: np.ndarray
) -> tuple[int, np.ndarray]:
    """Maximum-curvature point of the log-log L-curve, and the curvature itself.

    Curvature of the parametric curve `(log rho, log eta)` by finite differences. Endpoints
    are excluded because a one-sided derivative there is meaningless, so a corner at the very
    edge of the lambda range reports as the nearest interior point -- and a corner pinned to
    the edge is itself a signal that the range was too narrow, which the caller records.
    """
    x = np.log(np.maximum(residual_norms, 1e-300))
    y = np.log(np.maximum(solution_norms, 1e-300))
    dx, dy = np.gradient(x), np.gradient(y)
    ddx, ddy = np.gradient(dx), np.gradient(dy)
    denominator = np.power(dx * dx + dy * dy, 1.5)
    curvature = np.zeros_like(x)
    good = denominator > 0
    curvature[good] = (dx[good] * ddy[good] - dy[good] * ddx[good]) / denominator[good]
    interior = np.zeros_like(curvature, dtype=bool)
    interior[1:-1] = True
    if not interior.any():  # pragma: no cover - n_lambda >= 5 by configuration
        return int(np.argmax(curvature)), curvature
    masked = np.where(interior, curvature, -np.inf)
    return int(np.argmax(masked)), curvature


def invert(
    traces: list[TraceKernel],
    *,
    n_basis: int,
    stride: int,
    shift: int,
    dt: float,
    zero_endpoints: bool = True,
    lambda_value: float | None = None,
    lambda_min: float = 1e-4,
    lambda_max: float = 1e4,
    n_lambda: int = 40,
) -> InversionResult:
    """Invert for the force history; pick lambda from the L-curve unless one is given.

    `forces` comes back as `(3, n_basis * stride)` in newtons on the *fine* sample grid, so a
    coarse-basis result and a full-resolution one are directly comparable.
    """
    normal = accumulate(traces, n_basis=n_basis, stride=stride, shift=shift, dt=dt)
    return solve_normal(
        normal,
        stride=stride,
        zero_endpoints=zero_endpoints,
        lambda_value=lambda_value,
        lambda_min=lambda_min,
        lambda_max=lambda_max,
        n_lambda=n_lambda,
    )


def solve_normal(
    normal: NormalEquations,
    *,
    stride: int,
    zero_endpoints: bool = True,
    lambda_value: float | None = None,
    lambda_min: float = 1e-4,
    lambda_max: float = 1e4,
    n_lambda: int = 40,
) -> InversionResult:
    """Solve pre-accumulated normal equations. The grid search reuses this at every node."""
    solver = _Solver(normal, zero_endpoints=zero_endpoints)
    curve: LCurve | None = None
    if lambda_value is None:
        lambdas = np.geomspace(lambda_min, lambda_max, n_lambda)
        residuals = np.empty(n_lambda)
        solutions = np.empty(n_lambda)
        for i, lam in enumerate(lambdas):
            residuals[i], solutions[i] = solver.norms(solver.solve(float(lam)))
        index, curvature = l_curve_corner(lambdas, residuals, solutions)
        curve = LCurve(
            lambdas=lambdas,
            residual_norms=residuals,
            solution_norms=solutions,
            curvature=curvature,
            corner_index=index,
        )
        lambda_value = curve.corner_lambda

    reduced = solver.solve(lambda_value)
    full = solver.expand(reduced)
    residual, smooth = solver.norms(reduced)
    coarse = full.reshape(3, normal.n_basis)
    forces = np.repeat(coarse, stride, axis=1) if stride > 1 else coarse
    return InversionResult(
        coefficients=full,
        forces=forces,
        variance_reduction=normal.variance_reduction(full),
        lambda_value=float(lambda_value),
        residual_norm=residual,
        solution_norm=smooth,
        l_curve=curve,
        diagnostics={
            "operator_scale": solver.scale,
            "n_free": float(solver.free.size),
            "n_data": float(normal.n_data),
        },
    )
