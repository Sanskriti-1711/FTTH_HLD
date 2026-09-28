"""Shared grouped distribution-cable helpers for LLD GeoJSON processing.

The HLD QGIS algorithm creates one MultiLineString per same-footway group.
LLD uses this small GeoJSON-only implementation after approved survey changes
so Verify mode has the same physical representation without importing QGIS.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


RESERVED_SPARE_FIBERS = 2
DISTRIBUTION_FIBERS = 48


def cable_fiber_capacity(hh_count: Any, minimum: int) -> int:
    """Return a physical cable's fibre count for logical HH load plus spare."""
    try:
        households = max(0, int(float(hh_count or 0)))
    except (TypeError, ValueError):
        households = 0
    return max(int(minimum), households + RESERVED_SPARE_FIBERS)


def _lines(geometry: Optional[Dict[str, Any]]) -> List[List[List[float]]]:
    if not geometry:
        return []
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "LineString":
        return [coords]
    if kind == "MultiLineString":
        return coords
    return []


def _point_distance(a: List[float], b: List[float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _same_footway(a: List[float], b: List[float], tolerance: float) -> bool:
    return _point_distance(a, b) <= tolerance


def _is_drop(props: Dict[str, Any]) -> bool:
    """True for the one-per-physical-service-location garden-leg cable.

    A drop is not a co-routed trunk: it TAPS the spine at the footway, so its
    end coordinate is within the grouping tolerance of the trunk span it hangs
    off. Grouping it would fold a location drop into the trunk and relabel it as
    a shared trunk cable, which is exactly the sizing error
    ``regroup_distribution_cables`` exists to avoid elsewhere.
    """
    cable_type = str(props.get("CABLE_TYPE") or "").strip().lower()
    connection = str(props.get("CONNECTION_TYPE") or "").strip().lower()
    return (
        cable_type == "drop"
        or connection.startswith("drop")
        or connection == "dedicated drop"
    )


def regroup_distribution_cables(
    features: List[Dict[str, Any]],
    *,
    tolerance: float = 0.5,
) -> int:
    """Group eligible cable features in-place by PDP, polygon and footway end.

    A feature is eligible when it has a distribution cable geometry and a
    member address. Existing non-cable features and garden-leg drops are
    ignored (they pass through untouched, see ``_is_drop``). The first feature
    in each group receives a MultiLineString containing the original trunk/arm
    components; subsequent group members are removed. This preserves geometry
    exactly while eliminating duplicated grouped records.

    Returns the number of grouped output features.
    """
    groups: Dict[Tuple[str, str, int, int], List[Dict[str, Any]]] = {}
    passthrough: List[Dict[str, Any]] = []
    scale = max(tolerance, 1e-12)

    for feature in features:
        props = feature.setdefault("properties", {})
        members = [x.strip() for x in str(props.get("ADDR_IDS") or props.get("addr_id") or "").split(",") if x.strip()]
        lines = _lines(feature.get("geometry"))
        if not lines or not members or _is_drop(props):
            passthrough.append(feature)
            continue
        end = lines[0][-1] if lines[0] else None
        if end is None:
            passthrough.append(feature)
            continue
        key = (
            str(props.get("PDP_ID") or props.get("pdp_id") or ""),
            str(props.get("POLYGON_ID") or props.get("polygon_id") or ""),
            round(end[0] / scale),
            round(end[1] / scale),
        )
        groups.setdefault(key, []).append(feature)

    output: List[Dict[str, Any]] = list(passthrough)
    for group in groups.values():
        if len(group) == 1:
            output.append(group[0])
            continue
        base = group[0]
        base_props = base.setdefault("properties", {})
        coordinates: List[Any] = []
        members: List[str] = []
        hh_count = 0
        for feature in group:
            props = feature.setdefault("properties", {})
            coordinates.extend([line for line in _lines(feature.get("geometry")) if len(line) >= 2])
            item_members = [x.strip() for x in str(props.get("ADDR_IDS") or props.get("addr_id") or "").split(",") if x.strip()]
            members.extend(item_members)
            try:
                hh_count += int(float(props.get("HH_COUNT") or props.get("hhs") or len(item_members) or 1))
            except (TypeError, ValueError):
                hh_count += max(1, len(item_members))
        # De-duplicate member IDs while preserving source order.
        members = list(dict.fromkeys(members))
        hh_count = max(hh_count, len(members), 1)
        base["geometry"] = {"type": "MultiLineString", "coordinates": coordinates}
        base_props["addr_id"] = members[0]
        base_props["ADDR_IDS"] = ",".join(members)
        base_props["HH_COUNT"] = hh_count
        base_props["hhs"] = str(hh_count)
        # Match the HLD capacity rule: trunk floor 48F, location-drop floor
        # 12F, and enough fibres for its HH load plus reserved spare.
        base_props["FIBER_COUNT"] = cable_fiber_capacity(hh_count, DISTRIBUTION_FIBERS)
        base_props["RESERVED_SPARE_FIBERS"] = RESERVED_SPARE_FIBERS
        base_props["ACTIVE_FIBERS"] = hh_count
        base_props["AVAILABLE_FIBERS"] = max(
            0, base_props["FIBER_COUNT"] - RESERVED_SPARE_FIBERS - hh_count
        )
        base_props["CONNECTION_TYPE"] = "Shared trunk + branches"
        base_props["length_m"] = sum(
            _line_length(line) for line in coordinates
        )
        output.append(base)

    features[:] = output
    return len(output)


def _line_length(line: List[List[float]]) -> float:
    return sum(_point_distance(a, b) for a, b in zip(line, line[1:]))
