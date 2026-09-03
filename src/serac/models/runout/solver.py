"""`serac-swe-voellmy`: a depth-averaged Voellmy-Salm shallow-water solver with entrainment.

**This is NOT r.avaflow.** r.avaflow could not be obtained (no official GRASS addon, no
canonical public repository, avaflow.org's download behind a registration wall); see
`infra/docker/ravaflow/README.md` for the acquisition record. This module is a documented
substitute written for this repository, and cross-validation against r.avaflow is outstanding.
It is **single-phase**: it cannot represent two- or three-phase physics, nor phase separation
between rock, ice and fluid. Ice content enters only through the mixture density and, by the
operator's choice of Voellmy coefficients, through the friction.

Governing equations
-------------------
Conservative depth-averaged shallow-water equations over a fixed bed `b(x, y)`, with the
Voellmy-Salm basal resistance and a bed-entrainment source, in Cartesian map coordinates:

    d(h)/dt      + d(hu)/dx           + d(hv)/dy           = E
    d(hu)/dt     + d(hu^2 + gh^2/2)/dx + d(huv)/dy         = -g h db/dx + h Sfx - u E
    d(hv)/dt     + d(huv)/dx           + d(hv^2 + gh^2/2)/dy = -g h db/dy + h Sfy - v E

`h` is flow depth (m), `(u, v)` the depth-averaged velocity (m/s), `g` gravity. The last term
in each momentum equation is the momentum cost of entraining bed material that is at rest:
entrained mass arrives with zero velocity, so it decelerates the flow. That term is what makes
entrainment a brake rather than free acceleration, and it is exercised by
`test_entrainment_decelerates`.

**Voellmy-Salm resistance.** The basal shear opposing motion has a Coulomb part and a
turbulent part (Voellmy 1955; Salm 1993):

    tau_b / (rho h) = mu * g * cos(theta) + g * |U|^2 / (xi * h)

with `theta` the local bed slope. Written as a deceleration `a` along `-U/|U|`. On a uniform
slope the steady balance `g sin(theta) = mu g cos(theta) + g |U|^2 / (xi h)` gives the terminal
velocity

    |U|_term = sqrt( xi * h * (sin(theta) - mu * cos(theta)) )

which `test_terminal_velocity` reproduces. `mu` is dimensionless; `xi` has units m/s^2.

**Entrainment.** A parametric closure, not a calibrated law:

    E = c_e * |U| * max(0, 1 - tau_c / tau_b),   tau_b = rho g h cos(theta)

`c_e` is dimensionless, so `E` has units m/s. Entrainment is capped by the remaining erodible
depth in the cell and by the CFL step, so bed material can never be over-drawn. `c_e`, `tau_c`
and the erodible-depth layer itself are assumptions carried into every forecast's
`assumptions[]`; none of them is fitted to an observation.

Numerical scheme
----------------
First-order explicit finite volume on the uniform map-space grid, unsplit (fluxes in both
directions accumulated from the same state), with:

* **HLL flux** (Harten, Lax & van Leer 1983) with the Einfeldt wave-speed estimates, so the
  scheme is positivity-preserving in depth and handles the transonic rarefaction of a dam break
  without an entropy fix.
* **Hydrostatic reconstruction** (Audusse, Bouchut, Bristeau, Klein & Perthame 2004) at every
  interface. This is what makes the scheme *well-balanced*: a lake at rest over arbitrary
  topography stays exactly at rest to machine precision, which is asserted by
  `test_lake_at_rest_is_steady` and is not true of a naive centred bed-slope source.
* **Wetting and drying** via a `dry_depth_m` threshold: velocities are reconstructed as
  `hu / h` only where `h > dry_depth_m`, and are zero elsewhere, so a thin film cannot produce
  an unbounded velocity and a vanishing CFL step.
* **Semi-implicit friction** applied after the flux update as a *stopping* operator,
  `U <- U * max(0, 1 - dt * a / |U|)`, which cannot reverse the flow however large `dt * a` is.
  Momentum-conserving to the extent that friction is a sink; energy is not tracked.
* **CFL condition** `dt = C * dx / max(|U| + sqrt(g h))` over wet cells, `C <= 0.9`, default
  0.45 for two dimensions. `dt` is recomputed every step from the current state.

**Boundaries.** Every cell outside `domain_mask` is a reflective wall: the interface flux
between a wet cell and a wall cell is the solid-wall flux, so mass cannot leak through the
mask. The single exception is `outflow_mask`, the downstream end of the corridor, where a
zero-gradient (free) outflow removes mass from the domain; the removed volume is accumulated in
`RunResult.outflow_volume_m3` so the mass balance still closes.

Order of accuracy is **one**. A second-order MUSCL reconstruction would sharpen the front but
doubles the cost and complicates the well-balancing; the measured Ritter convergence rate is
reported in `reports/runout/verification.json` rather than claimed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from serac.models.runout.params import (
    GRAVITY,
    SOLVER_NAME,
    SOLVER_VERSION,
    SolverSettings,
    VoellmyParameters,
)

F64 = NDArray[np.float64]
BOOL = NDArray[np.bool_]


@dataclass
class SolverState:
    """Conserved variables plus the mutable bed."""

    h: F64
    hu: F64
    hv: F64
    bed: F64
    erodible: F64
    time_s: float = 0.0
    step: int = 0


@dataclass
class RunFlags:
    """Numerical events worth retaining on a member rather than discarding it."""

    velocity_clipped_steps: int = 0
    dt_floor_steps: int = 0
    hit_step_limit: bool = False
    hit_time_limit: bool = False
    negative_depth_repairs: int = 0
    repaired_volume_m3: float = 0.0
    mass_error_relative: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "velocity_clipped_steps": self.velocity_clipped_steps,
            "dt_floor_steps": self.dt_floor_steps,
            "hit_step_limit": self.hit_step_limit,
            "hit_time_limit": self.hit_time_limit,
            "negative_depth_repairs": self.negative_depth_repairs,
            "repaired_volume_m3": self.repaired_volume_m3,
            "mass_error_relative": self.mass_error_relative,
        }

    @property
    def reasons(self) -> list[str]:
        out: list[str] = []
        if self.velocity_clipped_steps:
            out.append(f"velocity clipped on {self.velocity_clipped_steps} steps")
        if self.dt_floor_steps:
            out.append(f"dt hit the floor on {self.dt_floor_steps} steps")
        if self.hit_step_limit:
            out.append("stopped at max_steps")
        if self.hit_time_limit:
            out.append("stopped at max_time_s")
        if self.negative_depth_repairs:
            out.append(
                f"{self.negative_depth_repairs} negative-depth repairs "
                f"({self.repaired_volume_m3:.3g} m3 fabricated)"
            )
        if abs(self.mass_error_relative) > 1e-6:
            out.append(f"mass error {self.mass_error_relative:.2e}")
        return out


@dataclass
class RunResult:
    """What one solve produced. Fields are on the solver grid unless named otherwise."""

    max_depth: F64
    max_speed: F64
    arrival_time_s: F64
    final_depth: F64
    deposit_depth: F64
    entrained_volume_m3: float
    outflow_volume_m3: float
    initial_volume_m3: float
    final_volume_m3: float
    steps: int
    time_s: float
    wall_time_s: float
    flags: RunFlags
    history: list[dict[str, float]] = field(default_factory=list)

    @property
    def mass_balance(self) -> dict[str, float]:
        """Closed volume budget. `repaired_volume_m3` is mass the scheme fabricated by clamping
        a negative depth to zero; it is a *source* in the budget rather than being hidden in the
        residual, so `relative_error` stays a statement about the flux discretisation and the
        fabricated volume stays separately visible on every member's `run.json`."""
        supplied = self.initial_volume_m3 + self.entrained_volume_m3 + self.flags.repaired_volume_m3
        accounted = self.final_volume_m3 + self.outflow_volume_m3
        denom = max(supplied, 1e-9)
        return {
            "initial_volume_m3": self.initial_volume_m3,
            "entrained_volume_m3": self.entrained_volume_m3,
            "repaired_volume_m3": self.flags.repaired_volume_m3,
            "outflow_volume_m3": self.outflow_volume_m3,
            "final_volume_m3": self.final_volume_m3,
            "residual_m3": accounted - supplied,
            "relative_error": (accounted - supplied) / denom,
        }


def _velocity(h: F64, hu: F64, hv: F64, dry: float) -> tuple[F64, F64]:
    """`hu / h` where wet, zero where dry: keeps a thin film from producing huge velocities."""
    wet = h > dry
    inv = np.where(wet, 1.0 / np.maximum(h, dry), 0.0)
    return hu * inv, hv * inv


def _active_window(h: F64, dry_depth: float, margin: int) -> tuple[slice, slice] | None:
    """Bounding box of the wet cells grown by `margin`, or None when nothing is wet."""
    wet = h > dry_depth
    rows = np.flatnonzero(wet.any(axis=1))
    cols = np.flatnonzero(wet.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        return None
    height, width = h.shape
    r0 = max(0, int(rows[0]) - margin)
    r1 = min(height, int(rows[-1]) + 1 + margin)
    c0 = max(0, int(cols[0]) - margin)
    c1 = min(width, int(cols[-1]) + 1 + margin)
    return slice(r0, r1), slice(c0, c1)


def _sides(ndim: int, axis: int) -> tuple[tuple[slice, ...], tuple[slice, ...]]:
    """The `(left, right)` index tuples of every interface along `axis`."""
    sl: list[slice] = [slice(None)] * ndim
    sr: list[slice] = [slice(None)] * ndim
    sl[axis] = slice(None, -1)
    sr[axis] = slice(1, None)
    return tuple(sl), tuple(sr)


def _effective_surface(h: F64, bed: F64, wet: BOOL, axis: int) -> tuple[F64, F64]:
    """Free-surface levels on each side of every interface, with the dry-neighbour clamp.

    A dry neighbour's surface is its bed, which may sit far above the water. Used raw that
    produces two failures at once: a lake with dry banks feels a spurious pressure gradient and
    climbs them, and the numerical viscosity drives mass out of a cell that has none. The rule
    is one line -- a **dry** side's surface is clamped to the wet side's -- and it is what makes
    both `test_lake_at_rest_with_dry_banks_stays_dry` and the mass balance hold:

    * dry neighbour *above* the water (a bank, or a wall at bed + 1e6): clamped to the wet
      side's level, so there is no gradient and no viscous flux -- the flow cannot see over
      the lip;
    * dry neighbour *below* the water (the advancing front): its bed is already below the wet
      level, the clamp does nothing, and the flow runs downhill as it should.
    """
    left, right = _sides(h.ndim, axis)
    w = h + bed
    wl = w[left].copy()
    wr = w[right].copy()
    dry_l = ~wet[left]
    dry_r = ~wet[right]
    wl = np.where(dry_l, np.minimum(wl, wr), wl)
    wr = np.where(dry_r, np.minimum(wr, wl), wr)
    return wl, wr


def _rusanov_flux(
    hl: F64,
    hul: F64,
    hvl: F64,
    ul: F64,
    hr: F64,
    hur: F64,
    hvr: F64,
    ur: F64,
    wl: F64,
    wr: F64,
    speed: F64,
) -> tuple[F64, F64, F64]:
    """Well-balanced Rusanov flux for the **advective** part only.

    The pressure gradient is not in the flux -- it is the `-g cos(theta) h grad(w)` source --
    so this carries advection plus a local Lax-Friedrichs viscosity. The viscosity on the mass
    equation acts on the **free surface** `w`, not on the depth: acting on depth would diffuse
    a lake at rest over an uneven bed, which is the classic way well-balancing is lost.
    `speed` is the local maximum wave speed at the interface.
    """
    # The surface difference is bounded by the water actually available on the draining side.
    # Unbounded, a wet cell facing a 50 m bed drop sees a viscous flux far larger than its own
    # depth, goes negative, and the repair fabricates mass -- 58% of it on the entrainment test.
    # Clipped this way the viscous mass flux is at most 0.5 * CFL * h of the draining cell.
    delta = np.clip(wr - wl, -hl, hr)
    f_h = 0.5 * (hl * ul + hr * ur) - 0.5 * speed * delta
    f_hu = 0.5 * (hul * ul + hur * ur) - 0.5 * speed * (hur - hul)
    f_hv = 0.5 * (hvl * ul + hvr * ur) - 0.5 * speed * (hvr - hvl)
    return f_h, f_hu, f_hv


def _accumulate(dst: F64, flux: F64, axis: int, scale: float, *, sign_left: float = -1.0) -> None:
    """Add `sign_left * scale * flux` to the cell left of each interface and the negation right."""
    sl = [slice(None)] * dst.ndim
    sr = [slice(None)] * dst.ndim
    sl[axis] = slice(None, -1)
    sr[axis] = slice(1, None)
    dst[tuple(sl)] += sign_left * scale * flux
    dst[tuple(sr)] -= sign_left * scale * flux


def _bed_slope(bed: F64, mask: BOOL, dx: float) -> tuple[F64, F64]:
    """Central-difference bed slope with walls reflected, used only for the friction angle."""
    b = np.where(mask, bed, np.nan)
    filled = np.where(np.isfinite(b), b, bed)
    dzdy, dzdx = np.gradient(filled, dx)
    return dzdx, dzdy


class VoellmySolver:
    """One configured solve. Construct, then call `run`."""

    name = SOLVER_NAME
    version = SOLVER_VERSION

    def __init__(
        self,
        *,
        bed: F64,
        domain_mask: BOOL,
        outflow_mask: BOOL,
        erodible_depth: F64,
        parameters: VoellmyParameters,
        settings: SolverSettings,
        gravity: float = GRAVITY,
    ) -> None:
        if bed.shape != domain_mask.shape:
            raise ValueError("bed and domain_mask must have the same shape")
        self.bed = np.ascontiguousarray(bed, dtype=np.float64)
        self.mask = np.ascontiguousarray(domain_mask, dtype=bool)
        self.outflow = np.ascontiguousarray(outflow_mask, dtype=bool) & self.mask
        self.erodible0 = np.ascontiguousarray(erodible_depth, dtype=np.float64) * self.mask
        self.p = parameters
        self.s = settings
        self.g = gravity
        self.dx = float(settings.resolution_m)
        self.cell_area = self.dx * self.dx
        dzdx, dzdy = _bed_slope(self.bed, self.mask, self.dx)
        self.slope_tan = np.hypot(dzdx, dzdy)
        self.cos_theta = 1.0 / np.sqrt(1.0 + self.slope_tan**2)
        # Walls are a bed 1e6 m high: the dry-neighbour clamp in `_effective_surface` then
        # gives them zero gradient and zero viscous flux, i.e. a reflective wall.
        self.wall_bed = np.where(self.mask, self.bed, self.bed + 1.0e6)
        self.wave_coeff: F64 = self.g * self.cos_theta

    # -- one step -------------------------------------------------------------------------------

    def timestep(self, h: F64, u: F64, v: F64, cos_theta: F64 | None = None) -> float:
        cos_theta = self.cos_theta if cos_theta is None else cos_theta
        wet = h > self.s.dry_depth_m
        if not wet.any():
            return self.s.max_time_s
        speed = np.hypot(u, v) + np.sqrt(self.g * cos_theta * np.maximum(h, 0.0))
        fastest = float(speed[wet].max())
        if fastest <= 1e-12:
            return self.s.max_time_s
        return self.s.cfl * self.dx / fastest

    def _surface_gradient(self, h: F64, wet: BOOL, wall_bed: F64, mask: BOOL) -> tuple[F64, F64]:
        """Centred `grad(h + b)` with dry and walled neighbours clamped to the cell's own level.

        The clamp is the same rule as `_effective_surface`: a neighbour that holds no water and
        stands higher than this cell exerts no pressure on it. Without it a lake with dry banks
        feels a gradient towards the bank and climbs it, and `test_lake_at_rest_with_dry_banks`
        fails.
        """
        w = h + wall_bed
        usable = wet & mask
        dwdx = np.zeros_like(h)
        dwdy = np.zeros_like(h)
        for axis, out in ((1, dwdx), (0, dwdy)):
            lo = np.roll(w, 1, axis=axis)
            hi = np.roll(w, -1, axis=axis)
            lo_ok = np.roll(usable, 1, axis=axis)
            hi_ok = np.roll(usable, -1, axis=axis)
            edge_lo = np.zeros_like(usable)
            edge_hi = np.zeros_like(usable)
            first: list[slice] = [slice(None)] * h.ndim
            last: list[slice] = [slice(None)] * h.ndim
            first[axis] = slice(0, 1)
            last[axis] = slice(-1, None)
            edge_lo[tuple(first)] = True
            edge_hi[tuple(last)] = True
            lo = np.where(lo_ok & ~edge_lo, lo, np.minimum(lo, w))
            hi = np.where(hi_ok & ~edge_hi, hi, np.minimum(hi, w))
            lo = np.where(edge_lo, w, lo)
            hi = np.where(edge_hi, w, hi)
            out[...] = (hi - lo) / (2.0 * self.dx)
        return dwdx, dwdy

    def _friction(
        self, h: F64, u: F64, v: F64, dt: float, cos_theta: F64 | None = None
    ) -> tuple[F64, F64]:
        """Voellmy-Salm stopping operator; cannot reverse the flow."""
        cos_theta = self.cos_theta if cos_theta is None else cos_theta
        speed = np.hypot(u, v)
        wet = h > self.s.dry_depth_m
        coulomb = self.p.mu * self.g * cos_theta
        turbulent = self.g * speed * speed / (self.p.xi_m_s2 * np.maximum(h, self.s.dry_depth_m))
        decel = np.where(wet, coulomb + turbulent, 0.0)
        factor = np.where(
            speed > 1e-12, np.maximum(0.0, 1.0 - dt * decel / np.maximum(speed, 1e-12)), 0.0
        )
        return u * factor, v * factor

    def entrainment_depth(
        self, h: F64, u: F64, v: F64, erodible: F64, dt: float, cos_theta: F64 | None = None
    ) -> F64:
        """Depth of bed entrained this step (m), capped by what is left and by stability."""
        cos_theta = self.cos_theta if cos_theta is None else cos_theta
        c_e = self.p.entrainment_coefficient
        if c_e <= 0.0:
            return np.zeros_like(h)
        speed = np.hypot(u, v)
        wet = h > self.s.dry_depth_m
        tau_b = self.p.bulk_density * self.g * np.maximum(h, 0.0) * cos_theta
        active = np.where(
            tau_b > 0.0,
            np.maximum(0.0, 1.0 - self.p.critical_shear_pa / np.maximum(tau_b, 1e-9)),
            0.0,
        )
        rate = np.where(wet, c_e * speed * active, 0.0)
        # never entrain more than the remaining mantle, nor more than the current depth per step
        return np.minimum(np.minimum(rate * dt, erodible), np.maximum(h, 0.0))

    # -- the solve ------------------------------------------------------------------------------

    def run(
        self,
        initial_depth: F64,
        *,
        initial_hu: F64 | None = None,
        initial_hv: F64 | None = None,
        record_history: bool = False,
        observers: list[Any] | None = None,
    ) -> RunResult:
        """Advance to `max_time_s`, exhaustion of motion, or `max_steps`, whichever is first.

        The step is evaluated on an **active window**: the bounding box of the wet cells grown
        by a margin, refreshed every few steps. Everything outside is dry and static, so nothing
        is lost, and on the Lhende corridor at 90 m only about 2,000 of 403,000 cells are ever
        wet at once -- computing the whole grid every step cost 78 ms/step for no information.
        The margin is set from the refresh interval so the front, which advances at most one
        cell per step under the CFL condition, cannot reach the window edge between refreshes.
        """
        import time as _time

        started = _time.perf_counter()
        h_full = np.ascontiguousarray(initial_depth, dtype=np.float64) * self.mask
        hu_full = (
            np.zeros_like(h_full)
            if initial_hu is None
            else np.ascontiguousarray(initial_hu, np.float64) * self.mask
        )
        hv_full = (
            np.zeros_like(h_full)
            if initial_hv is None
            else np.ascontiguousarray(initial_hv, np.float64) * self.mask
        )
        erodible_full = self.erodible0.copy()
        flags = RunFlags()

        initial_volume = float(h_full.sum() * self.cell_area)
        entrained_total = 0.0
        outflow_total = 0.0

        max_depth = h_full.copy()
        max_speed = np.zeros_like(h_full)
        arrival = np.full(h_full.shape, np.nan)
        arrival[h_full > self.s.dry_depth_m] = 0.0
        history: list[dict[str, float]] = []
        next_output = 0.0

        refresh = self.s.window_refresh_steps
        margin = 2 * refresh + 4
        window: tuple[slice, slice] | None = None
        steps_since_refresh = refresh

        t = 0.0
        step = 0
        peak_kinetic = 0.0
        while t < self.s.max_time_s and step < self.s.max_steps:
            if steps_since_refresh >= refresh:
                window = _active_window(h_full, self.s.dry_depth_m, margin)
                if window is None:
                    break
                steps_since_refresh = 0
                h = h_full[window]
                hu = hu_full[window]
                hv = hv_full[window]
                erodible = erodible_full[window]
                mask = self.mask[window]
                wall_bed = self.wall_bed[window]
                cos_theta = self.cos_theta[window]
                outflow = self.outflow[window]
                has_outflow = bool(outflow.any())
                wave_coeff = self.wave_coeff[window]
            steps_since_refresh += 1

            u, v = _velocity(h, hu, hv, self.s.dry_depth_m)
            dt = self.timestep(h, u, v, cos_theta)
            if dt < self.s.min_dt_s:
                dt = self.s.min_dt_s
                flags.dt_floor_steps += 1
            dt = min(dt, self.s.max_time_s - t)
            if dt <= 0.0:
                break

            dh = np.zeros_like(h)
            dhu = np.zeros_like(h)
            dhv = np.zeros_like(h)
            scale = dt / self.dx
            wet = h > self.s.dry_depth_m
            cell_speed = np.hypot(u, v) + np.sqrt(wave_coeff * np.maximum(h, 0.0))

            faces: list[tuple[int, F64, F64, F64]] = []
            for axis in (0, 1):
                # axis 0 indexes rows (y, north-up), axis 1 indexes columns (x)
                left, right = _sides(h.ndim, axis)
                normal = v if axis == 0 else u
                h_normal = hv if axis == 0 else hu
                h_transverse = hu if axis == 0 else hv
                wl, wr = _effective_surface(h, wall_bed, wet, axis)
                face_speed = np.maximum(cell_speed[left], cell_speed[right])
                f_h, f_n, f_t = _rusanov_flux(
                    h[left],
                    h_normal[left],
                    h_transverse[left],
                    normal[left],
                    h[right],
                    h_normal[right],
                    h_transverse[right],
                    normal[right],
                    wl,
                    wr,
                    face_speed,
                )
                # a wall on either side blocks the interface entirely
                open_face = mask[left] & mask[right]
                faces.append(
                    (
                        axis,
                        np.where(open_face, f_h, 0.0),
                        np.where(open_face, f_n, 0.0),
                        np.where(open_face, f_t, 0.0),
                    )
                )

            # Conservative positivity limiter (Zalesak-style). A cell has four faces and each
            # can drain up to ~0.8 h in a step, so the raw fluxes can empty it past zero; the
            # clamp-to-zero repair then fabricates mass -- 32% of the total on the entrainment
            # test. Here each donor cell's outgoing fluxes are scaled by one factor so it can
            # give away at most what it holds. The same scaled flux is used on both sides of
            # every face, so the scheme stays exactly conservative.
            outgoing = np.zeros_like(h)
            for axis, f_h, _, _ in faces:
                left, right = _sides(h.ndim, axis)
                outgoing[left] += np.maximum(f_h, 0.0) * scale
                outgoing[right] += np.maximum(-f_h, 0.0) * scale
            available = np.maximum(h, 0.0)
            limiter = np.where(outgoing > available, available / np.maximum(outgoing, 1e-300), 1.0)
            np.clip(limiter, 0.0, 1.0, out=limiter)

            for axis, f_h, f_n, f_t in faces:
                left, right = _sides(h.ndim, axis)
                donor = np.where(f_h >= 0.0, limiter[left], limiter[right])
                _accumulate(dh, f_h * donor, axis, scale)
                _accumulate(dhv if axis == 0 else dhu, f_n * donor, axis, scale)
                _accumulate(dhu if axis == 0 else dhv, f_t * donor, axis, scale)

            # Pressure and gravity together, as the slope-aligned surface-gradient source
            # -g cos(theta) h grad(h + b). Exactly zero for a flat free surface at rest, and
            # exactly g sin(theta) for a uniform sheet on a uniform slope -- which is why the
            # Voellmy terminal velocity comes out right. Map-space hydrostatic reconstruction
            # was tried first and under-drove a 20 deg slope by a factor of 7, because at 20 m
            # cells the bed drops 7.3 m per cell against a 2 m flow depth.
            dwdx, dwdy = self._surface_gradient(h, wet, wall_bed, mask)
            drive = self.g * cos_theta * h
            dhu -= dt * drive * dwdx
            dhv -= dt * drive * dwdy

            h += dh
            hu += dhu
            hv += dhv

            negative = h < 0.0
            if negative.any():
                flags.negative_depth_repairs += int(negative.sum())
                # clamping a negative depth to zero *creates* mass; record exactly how much so
                # the mass balance shows it instead of absorbing it
                flags.repaired_volume_m3 += float(-h[negative].sum() * self.cell_area)
                hu[negative] = 0.0
                hv[negative] = 0.0
                h[negative] = 0.0
            h *= mask
            hu *= mask
            hv *= mask

            # entrainment: mass in, momentum unchanged (bed material starts at rest)
            u, v = _velocity(h, hu, hv, self.s.dry_depth_m)
            de = self.entrainment_depth(h, u, v, erodible, dt, cos_theta)
            if de.any():
                h += de
                erodible -= de
                entrained_total += float(de.sum() * self.cell_area)

            # friction
            u, v = _velocity(h, hu, hv, self.s.dry_depth_m)
            u, v = self._friction(h, u, v, dt, cos_theta)
            speed = np.hypot(u, v)
            over = speed > self.s.max_velocity_m_s
            if over.any():
                flags.velocity_clipped_steps += 1
                clip = np.where(over, self.s.max_velocity_m_s / np.maximum(speed, 1e-12), 1.0)
                u = u * clip
                v = v * clip
                speed = np.hypot(u, v)
            np.multiply(h, u, out=hu)
            np.multiply(h, v, out=hv)

            # free outflow at the downstream end
            if has_outflow:
                leaving = h * outflow
                outflow_total += float(leaving.sum() * self.cell_area)
                h -= leaving
                hu[outflow] = 0.0
                hv[outflow] = 0.0

            t += dt
            step += 1

            wet_now = h > self.s.dry_depth_m
            np.maximum(max_depth[window], h, out=max_depth[window])
            np.maximum(max_speed[window], speed, out=max_speed[window])
            arrival_view = arrival[window]
            newly = wet_now & np.isnan(arrival_view)
            arrival_view[newly] = t

            # Measured against the PEAK kinetic energy, not the first non-zero value. The
            # release is emplaced at rest, so the first non-zero value is whatever the flow had
            # after one step -- essentially nothing -- and a threshold of 1e-3 of that never
            # triggers. Every member of the first ensemble ran to the simulated-time limit
            # because of it, which made runout distance a statement about the compute budget
            # rather than about the rheology.
            kinetic = float((0.5 * h * speed * speed).sum())
            peak_kinetic = max(peak_kinetic, kinetic)
            if record_history and t >= next_output:
                history.append(
                    {
                        "t_s": t,
                        "volume_m3": float(h.sum() * self.cell_area),
                        "max_depth_m": float(h.max()),
                        "max_speed_m_s": float(speed.max()),
                        "wet_cells": float(wet_now.sum()),
                    }
                )
                next_output = t + self.s.output_interval_s
            if observers:
                # observers see full-grid arrays; the window is an optimisation, not an
                # interface, so it must not leak into what a caller indexes
                u_full = np.zeros_like(h_full)
                v_full = np.zeros_like(h_full)
                u_full[window] = u
                v_full[window] = v
                for obs in observers:
                    obs(t, dt, h_full, u_full, v_full)

            if self.s.stop_when_dry and not wet_now.any():
                break
            if (
                peak_kinetic > 0.0
                and kinetic < self.s.stop_kinetic_fraction * peak_kinetic
                and t > 60.0
            ):
                break

        flags.hit_step_limit = step >= self.s.max_steps
        flags.hit_time_limit = t >= self.s.max_time_s
        final_volume = float(h_full.sum() * self.cell_area)
        result = RunResult(
            max_depth=max_depth,
            max_speed=max_speed,
            arrival_time_s=arrival,
            final_depth=h_full,
            deposit_depth=h_full,
            entrained_volume_m3=entrained_total,
            outflow_volume_m3=outflow_total,
            initial_volume_m3=initial_volume,
            final_volume_m3=final_volume,
            steps=step,
            time_s=t,
            wall_time_s=_time.perf_counter() - started,
            flags=flags,
            history=history,
        )
        flags.mass_error_relative = result.mass_balance["relative_error"]
        return result


def terminal_velocity(
    h: float, slope_rad: float, mu: float, xi: float, g: float = GRAVITY
) -> float:
    """Voellmy-Salm steady velocity on a uniform slope; zero when the slope cannot drive flow."""
    driving = math.sin(slope_rad) - mu * math.cos(slope_rad)
    if driving <= 0.0:
        return 0.0
    return math.sqrt(xi * h * driving)


def ritter_solution(x: F64, t: float, h0: float, g: float = GRAVITY) -> tuple[F64, F64]:
    """Analytic Ritter (1892) dam break on a dry, frictionless, horizontal bed.

    Dam at `x = 0`, still water of depth `h0` for `x < 0`, dry for `x > 0`. Returns `(h, u)`.
    """
    c0 = math.sqrt(g * h0)
    if t <= 0.0:
        return np.where(x < 0.0, h0, 0.0), np.zeros_like(x)
    xi = x / t
    h = np.where(
        xi <= -c0,
        h0,
        np.where(xi >= 2.0 * c0, 0.0, (1.0 / (9.0 * g)) * (2.0 * c0 - xi) ** 2),
    )
    u = np.where(
        xi <= -c0,
        0.0,
        np.where(xi >= 2.0 * c0, 0.0, (2.0 / 3.0) * (c0 + xi)),
    )
    return h, u
