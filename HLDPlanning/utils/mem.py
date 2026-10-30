# -*- coding: utf-8 -*-
"""Hand freed memory back to the OS while a QGIS stage is still running.

``qgis_process`` shares a 512 MB container with the engine, and the growth pass
is its largest allocation: the Voronoi partition of every building is built,
dissolved and converted to QGIS geometry once per defragmentation round, up to
eleven times in one stage.  Python drops each round's objects as soon as the
last reference goes, but glibc keeps the arenas, so RSS only ever climbed —
run e394dd0c sat at 501 MB of the 512 MB cap inside "defragmentation round 2/6"
until the stall guard killed it.

``gc.collect()`` is needed as well as ``malloc_trim``: the PyQGIS/GEOS wrappers
form reference cycles that reference counting alone never frees, which is why
the partitions piled up instead of being reused.  Both calls are best effort
(Windows has no ``malloc_trim``), so nothing here can fail a run.
"""

from __future__ import annotations

import ctypes
import gc
from pathlib import Path
from typing import Any, Dict, Optional

_TRIM = None
_TRIED = False


def _trim() -> Optional[bool]:
    """Return free malloc arenas to the OS (glibc only; None elsewhere)."""
    global _TRIM, _TRIED
    if not _TRIED:
        _TRIED = True
        try:
            _TRIM = ctypes.CDLL("libc.so.6").malloc_trim
        except Exception:  # noqa: BLE001 - absent off glibc (Windows, musl)
            _TRIM = None
    if _TRIM is None:
        return None
    try:
        return bool(_TRIM(0))
    except Exception:  # noqa: BLE001 - never let housekeeping fail a run
        return None


def rss_mb() -> Optional[float]:
    """This process's resident size in MB (None where /proc is unavailable)."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024.0, 1)
    except OSError:
        return None
    return None


def release_memory(reason: str = "") -> Dict[str, Any]:
    """Collect reference cycles, trim the arenas, report what is held now.

    ``objects`` is the count ``gc.collect()`` reclaimed: a number near zero
    across repeated calls means the stage is genuinely holding memory rather
    than leaking it, which is the distinction the stage log needs.
    """
    objects = gc.collect()
    trimmed = _trim()
    return {"reason": reason, "objects": objects, "trimmed": trimmed,
            "rss_mb": rss_mb()}
