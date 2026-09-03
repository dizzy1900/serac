"""Keep the OpenMP runtimes from colliding when torch and lightgbm share a process.

M4's tests import torch; M1's import lightgbm. On macOS arm64 each ships its own OpenMP
runtime, and a process that initialises both **segfaults** — reproducibly, with
`pytest tests/unit/discriminator tests/unit/models`, and intermittently under `pytest -n auto`
where xdist decides which worker gets which module. Capping OpenMP to one thread avoids it.

This belongs in the root `tests/conftest.py`, which is orchestrator-owned; setting it here is
only reliable while pytest imports this file before either runtime initialises. The orchestrator
has been asked to hoist it.
"""

from __future__ import annotations

import os

for _var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_var, "1")
