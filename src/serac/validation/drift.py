"""Comparing a fresh run against the committed record, ignoring what a clock cannot repeat.

`validate-e2e` used to *overwrite* `reports/e2e/*` on every run. Two things followed from that,
and the second is worse than the first.

The visible one is Known gap 66: the gate dirtied the working tree that `make promote` requires
clean, so nothing could ever be promoted. The deeper one is that a gate which rewrites its own
evidence proves nothing. The committed report agreed with the code by construction, however far
the code had drifted from what the report claimed, because the report was replaced before anyone
compared them.

So the gate now runs into a scratch directory and *checks* the committed record against it. A
change in what the chain does is a finding. A change in what the clock said is not, and this
module is the line between the two: a key is volatile when it is a timestamp or a wall-clock
measurement, and volatile values are dropped from the comparison and reported separately rather
than being allowed to fail a suite. Re-recording is a deliberate act — `serac cascade e2e --event
<id>` — not a side effect of running the gate.
"""

from __future__ import annotations

import re
from typing import Any

VOLATILE_SUFFIXES: tuple[str, ...] = ("_utc", "_at")
"""A key ending in one of these holds a clock reading."""

VOLATILE_KEYS: frozenset[str] = frozenset({"compute_seconds_total", "wall_clock_s"})
"""Measured durations. Real numbers, and the reason the latency report exists -- but they move
with the machine, so they are compared as measurements rather than as facts about the code."""

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_MEASURED = re.compile(r'("(?:' + "|".join(sorted(VOLATILE_KEYS)) + r')"\s*:\s*)(-?\d+(?:\.\d+)?)')


def is_volatile(key: str) -> bool:
    """True when ``key`` holds a clock reading or a wall-clock measurement."""
    return key in VOLATILE_KEYS or key.endswith(VOLATILE_SUFFIXES)


def stable_view(payload: Any) -> Any:
    """``payload`` with every volatile key removed, recursively."""
    if isinstance(payload, dict):
        return {k: stable_view(v) for k, v in payload.items() if not is_volatile(str(k))}
    if isinstance(payload, list):
        return [stable_view(v) for v in payload]
    return payload


def stable_text(text: str) -> str:
    """``text`` with timestamps and measured durations masked, for rendered documents."""
    masked = _ISO.sub("<utc>", text)
    return _MEASURED.sub(r"\1<measured>", masked)


def drift(committed: Any, fresh: Any, *, path: str = "") -> list[str]:
    """Paths at which the stable views of two payloads differ, most specific first.

    Both sides are passed through :func:`stable_view` first, so a difference reported here is a
    difference in what the code did, never in when it was run.
    """
    return _diff(stable_view(committed), stable_view(fresh), path)


def _diff(a: Any, b: Any, path: str) -> list[str]:
    here = path or "$"
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append(f"{here}.{key}: added ({_short(b[key])})")
            elif key not in b:
                out.append(f"{here}.{key}: removed (was {_short(a[key])})")
            else:
                out.extend(_diff(a[key], b[key], f"{here}.{key}"))
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{here}: length {len(a)} -> {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out.extend(_diff(x, y, f"{here}[{i}]"))
        return out
    if a != b:
        return [f"{here}: {_short(a)} -> {_short(b)}"]
    return []


def measured_values(payload: Any, *, path: str = "") -> dict[str, float]:
    """Every volatile *numeric* value in ``payload``, keyed by path.

    The durations dropped from the comparison are still the measurements the latency report is
    for, so they are collected here and reported rather than discarded.
    """
    out: dict[str, float] = {}
    here = path or "$"
    if isinstance(payload, dict):
        for key, value in payload.items():
            child = f"{here}.{key}"
            if str(key) in VOLATILE_KEYS and isinstance(value, int | float):
                out[child] = float(value)
            else:
                out.update(measured_values(value, path=child))
    elif isinstance(payload, list):
        for i, value in enumerate(payload):
            out.update(measured_values(value, path=f"{here}[{i}]"))
    return out


def _short(value: Any, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."
