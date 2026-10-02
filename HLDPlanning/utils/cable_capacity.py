"""Fibre-count sizing policy shared by HLD and LLD cable stages."""

from typing import Any, Optional, Sequence, Tuple


RESERVED_SPARE_FIBERS = 2
DROP_FIBER_LADDER: Tuple[int, ...] = (12, 24, 48, 72, 96, 144, 288)
DISTRIBUTION_FIBER_LADDER: Tuple[int, ...] = (48, 72, 96, 144, 288)
DROP_FIBER_MAX = DROP_FIBER_LADDER[-1]


def household_count(value: Any) -> int:
    """Parse a non-negative integral HH load, defaulting malformed values to 0."""
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _size_on_ladder(households: int, ladder: Sequence[int], minimum: int) -> Optional[int]:
    required = max(int(minimum), households + RESERVED_SPARE_FIBERS)
    return next((size for size in ladder if size >= required), None)


def drop_fiber_capacity(hh_count: Any) -> Optional[int]:
    """Smallest standard 12/24/48/72/96/144/288F drop for HH + two spare.

    ``None`` means demand exceeds the hard 288F maximum (286 HH with two spare).
    """
    return _size_on_ladder(
        household_count(hh_count), DROP_FIBER_LADDER, DROP_FIBER_LADDER[0]
    )


def distribution_fiber_capacity(hh_count: Any, minimum: int = 48) -> int:
    """Smallest standard distribution size for HH plus two spare fibres.

    Distribution trunks retain 48F minimum. Above 288F, return exact required
    capacity rather than emitting a standard-sized cable that is undersized.
    """
    households = household_count(hh_count)
    required = max(int(minimum), households + RESERVED_SPARE_FIBERS)
    return _size_on_ladder(households, DISTRIBUTION_FIBER_LADDER, minimum) or required


def drop_capacity_warning(hh_count: Any) -> Optional[str]:
    households = household_count(hh_count)
    if households + RESERVED_SPARE_FIBERS <= DROP_FIBER_MAX:
        return None
    return (
        f"physical service location has {households} HH; one {DROP_FIBER_MAX}F "
        f"drop carries at most {DROP_FIBER_MAX - RESERVED_SPARE_FIBERS} HH with "
        f"{RESERVED_SPARE_FIBERS} spare. Add a building distribution point or "
        "source distinct unit locations before designing per-unit drops."
    )
