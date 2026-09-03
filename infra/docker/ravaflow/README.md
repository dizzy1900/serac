# r.avaflow — acquisition record (FAILED)

This directory exists to record, with dates and URLs, that **r.avaflow could not be obtained**,
and to say what serac does instead. There is no Dockerfile here because there is nothing to
build: no image can be produced without the source.

## What was attempted, 2026-09-03

| Route | URL | Outcome |
|---|---|---|
| Official GRASS GIS addon | `https://grass.osgeo.org/grass83/manuals/addons/r.avaflow.html` | **404**. `r.avaflow` is not in the official `grass-addons` collection, so `g.extension extension=r.avaflow` cannot resolve it. |
| Canonical public source repository | searched GitHub / GitLab for an upstream-maintained `r.avaflow` | **none found**. Third-party mirrors and forks exist but none is the canonical distribution, and building a hazard model from an unattributed mirror would defeat the point of the exercise. |
| Project site download | `https://www.avaflow.org/` | **registration wall**. The download is gated behind a form requesting name, institution and email. That registration was not performed. |
| Container base | GRASS GIS official images | **amd64 only**. This machine is macOS arm64; Docker buildx can emulate amd64 but slowly, and it is moot without the addon source. |

The founder's decision, recorded in the Prompt 2 plan: implement a documented depth-averaged
solver substitute in-repo and label it **NOT r.avaflow** everywhere.

## What serac uses instead

`serac-swe-voellmy` v0 — `src/serac/models/runout/solver.py`. A single-phase depth-averaged
Voellmy-Salm shallow-water solver with entrainment, verified against mass conservation, a
still-water lake, the analytic Ritter dam break and the analytic Voellmy terminal velocity. Its
governing equations, numerical scheme, CFL condition and boundary handling are documented in the
module docstring and in `reports/MODEL_CARD_runout.md`.

## What it cannot do that r.avaflow can

r.avaflow implements the Pudasaini multi-phase mass-flow model. `serac-swe-voellmy` does not:

* **No two- or three-phase physics.** r.avaflow solves separate solid, fine-solid and fluid
  phases with interfacial momentum transfer. serac solves one depth-averaged phase.
* **No phase separation.** r.avaflow can develop a fluid-rich front running ahead of a
  solid-rich body — the mechanism behind the long, fast runout of real rock-ice cascades.
  serac's ice fraction only sets a mixture density; it cannot separate.
* **No phase-dependent entrainment or deposition**, no pore-pressure evolution, no explicit
  ice melt.

Consequence for this corridor, measured and not hypothetical: 92% of the Lhende–Bhote Koshi–
Trishuli thalweg is below 6.8 degrees, so a Coulomb friction coefficient above roughly 0.08
cannot sustain motion there at all. A single-phase Voellmy rheology therefore stops far short of
the ~100 km the 26 August 2026 cascade is reported to have travelled. See
`reports/runout/langtang_sanity.md`.

## Outstanding

**Cross-validation against r.avaflow is outstanding.** Until it is done there is no independent
simulator against which to separate serac's structural biases from implementation error. Doing
it requires either obtaining r.avaflow through the registration route or finding an
authoritative mirror; whoever does it should record the outcome by editing this file.
