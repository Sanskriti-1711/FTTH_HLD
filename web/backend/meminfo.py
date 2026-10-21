"""Process memory, read from procfs.

The engine shares a 512 MB container with ``qgis_process`` and has been killed
for exceeding it, which leaves nothing behind: no traceback, no output files,
just a Render ``oomKilled`` event and a run that "never wrote any output".  A
run's real memory cost therefore has to be recorded while it runs, and these
helpers are deliberately dependency-free and read-only so they can be called
from a request handler, the OSM fetch thread, or around a child process without
allocating anything themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

_PROC = Path("/proc")
# cgroup v2 first: the Render container exposes v2.  v1 is kept for the local
# Docker setup, where the limit lives at the older path.
_CGROUP_LIMITS = (
    "/sys/fs/cgroup/memory.max",
    "/sys/fs/cgroup/memory/memory.limit_in_bytes",
)


def _status_field(pid: Optional[int], field: str) -> Optional[float]:
    """One ``Vm*`` field of /proc/<pid>/status in MB, or None off Linux."""
    try:
        lines = (_PROC / str(pid if pid is not None else "self") / "status").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith(field):
            try:
                return round(int(line.split()[1]) / 1024.0, 1)
            except (IndexError, ValueError):
                return None
    return None


def rss_mb(pid: Optional[int] = None) -> Optional[float]:
    """Resident set size of a process in MB (this process when ``pid`` is None)."""
    return _status_field(pid, "VmRSS:")


def peak_rss_mb(pid: Optional[int] = None) -> Optional[float]:
    """Highest RSS the process has reached, in MB."""
    return _status_field(pid, "VmHWM:")


def limit_mb() -> Optional[float]:
    """The container's memory limit in MB, or None when it is not capped."""
    for path in _CGROUP_LIMITS:
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if not raw.isdigit():
            continue
        value = int(raw)
        # cgroup v1 spells "no limit" as a page-rounded number near 2^63.
        if value < (1 << 50):
            return round(value / 1048576.0, 1)
    return None


def available_mb(pid: Optional[int] = None) -> Optional[float]:
    """Room left before the container's cap, in MB (None when not capped)."""
    limit = limit_mb()
    used = rss_mb(pid)
    if limit is None or used is None:
        return None
    return round(max(0.0, limit - used), 1)


def tight(fraction: float = 0.8, pid: Optional[int] = None) -> bool:
    """Is this process close enough to the cap that a big allocation may kill it?

    Used to decide BEFORE an area build or a run starts, because the kill itself
    leaves nothing behind: no traceback, no output, and a status page that can
    only say "killed or restarted".  A refused start is visible and retryable.
    """
    limit = limit_mb()
    used = rss_mb(pid)
    if not limit or used is None:
        return False
    return used >= limit * fraction


def report() -> Dict[str, Any]:
    """A JSON-safe snapshot for /health."""
    return {
        "rss_mb": rss_mb(),
        "peak_rss_mb": peak_rss_mb(),
        "limit_mb": limit_mb(),
        "available_mb": available_mb(),
        "tight": tight(),
    }


def trace(label: str, **extra: Any) -> Dict[str, Any]:
    """One stdout line per call, so the platform log keeps the memory history.

    stdout rather than the task message log: a sampler that ran every 20 s for
    a ten-minute stage would bury the pipeline's own output in the page.
    """
    data = report()
    parts = [f"{key}={data[key]}" for key in ("rss_mb", "peak_rss_mb", "limit_mb")]
    parts.extend(f"{key}={value}" for key, value in extra.items())
    print(f"[mem] {label}: " + " ".join(parts), flush=True)
    return data


def trim() -> Optional[bool]:
    """Return free malloc arenas to the OS (glibc only; None elsewhere).

    Python releases the parsed area's element list once its inputs are written,
    but glibc keeps the arena, so the peak of the run is the SUM of that peak
    and QGIS's.  Trimming before the pipeline starts hands that room back.
    """
    try:
        import ctypes

        return bool(ctypes.CDLL("libc.so.6").malloc_trim(0))
    except Exception:  # noqa: BLE001 - best effort: absent off glibc
        return None
