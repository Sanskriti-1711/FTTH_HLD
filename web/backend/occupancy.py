"""Duct / cable occupancy registry for the engine API.

The design stages already *compute* occupancy — the duct stage records
``WAYS_TOTAL`` / ``ways_used`` / ``cables_carried`` / ``BUNDLE_LEN_M`` on every
corridor, the cable stage records ``FIBER_COUNT`` / ``HH_COUNT`` /
``AVAILABLE_FIBERS`` / ``UTIL_PCT`` on every cable.  Until now those numbers
only existed as attributes on the layer that produced them: nothing carried
them forward, so a later run (or the LLD) re-planned the network with no idea
which ducts still had spare ways.

This module publishes them as two first-class layers of their own —
``Duct_Occupancy`` and ``Cable_Occupancy`` — so the occupancy is stored in the
project and in PostGIS with every run, and can be read back:

* as brownfield ducts for a re-run / LLD, using the field names the brownfield
  loader takes (``capacity_field="WAYS_TOTAL"``,
  ``capacity_used_field="WAYS_USED"``) — see ``HLDPlanning/utils/brownfield.py``
  (``has_spare`` / ``free`` / ``reserve``), so spare ways are consumed before a
  new duct is laid and a full duct is never routed through;
* by the platform (BOQ, permits utility-coexistence evidence, the results map);
* by the survey app, which records the same ``CAPACITY_TOTAL`` /
  ``CAPACITY_USED`` pair for existing ducts found in the field.

The layers are DERIVED from the design outputs — no algorithm changes — so the
registry always agrees with the run it belongs to.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# (output geojson, tier, duct/cable) — the files the duct and cable stages
# already write.  Missing files are skipped, so a partial run still publishes
# whatever occupancy it produced.
_DUCT_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("Feeder_Ducts.geojson", "Feeder"),
    ("Distribution_Ducts.geojson", "Distribution"),
    ("Drop_Ducts.geojson", "Drop"),
)

_CABLE_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("Feeder_Cable.geojson", "Feeder"),
    ("Distribution_Cable.geojson", "Distribution"),
)


def _num(value: Any, default: float = 0.0) -> float:
    """Coerce a layer attribute to a number, tolerating strings and nulls."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    return int(round(_num(value, default)))


def _asset_id(raw: Any, tier: str, kind: str, serial: int) -> str:
    """Use the layer's own id when it is one, otherwise mint a stable one.

    A bare sequence number (some drop ducts carry ``DUCT_UID = "7"``) is not
    an identity — it collides across tiers, so those rows get a tiered id.
    """
    if raw is not None:
        text = str(raw).strip()
        if text and not text.isdigit():
            return text
    return f"{tier.upper()}-{kind}-{serial:03d}"


def _split_count(value: Any) -> int:
    """Number of comma-joined ids in a value (0 when empty)."""
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    return len([part for part in text.split(",") if part.strip()])


def _load(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _features(doc: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(doc, dict):
        return []
    features = doc.get("features")
    return [f for f in features if isinstance(f, dict)] if features else []


def _write(path: Path, features: List[Dict[str, Any]], crs: str = "EPSG:4326") -> None:
    """Write a feature collection next to the layer it was derived from."""
    payload = {
        "type": "FeatureCollection",
        "name": path.stem,
        "crs": {"type": "name", "properties": {"name": crs}},
        "features": features,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def duct_rows(output_dir: Path) -> List[Dict[str, Any]]:
    """One occupancy row per duct, normalised across the three duct tiers."""
    rows: List[Dict[str, Any]] = []
    serial = 0
    for filename, tier in _DUCT_SOURCES:
        for feature in _features(_load(output_dir / filename)):
            geom = feature.get("geometry")
            if not geom:
                continue
            props = feature.get("properties") or {}
            serial += 1

            # The clubbed corridor carries WAYS_TOTAL = ways x parallel runs;
            # the per-run form carries capacity_total = ways.  Both reduce to
            # "how many ways this corridor provides, how many are taken".
            ways_total = _int(props.get("WAYS_TOTAL"))
            if ways_total <= 0:
                ways_total = _int(props.get("capacity_total"))
            if ways_total <= 0:
                ways_total = _int(props.get("WAYS"), 1)
            n_ducts = max(1, _int(props.get("N_DUCTS"), 1))
            if ways_total <= 0:
                ways_total = n_ducts

            ways_used = _int(props.get("ways_used"))
            if ways_used <= 0:
                ways_used = _split_count(props.get("cables_carried"))
            if ways_used <= 0 and tier == "Drop":
                # A drop duct is laid for exactly one premise, so exactly one
                # drop cable occupies its single way.
                ways_used = 1
            ways_used = min(ways_used, ways_total)
            spare = max(0, ways_total - ways_used)

            occupancy_pct = _num(props.get("OCCUPANCY_PCT"), -1.0)
            if occupancy_pct < 0:
                occupancy_pct = (100.0 * ways_used / ways_total) if ways_total else 0.0

            length_m = _num(props.get("length_m"))
            if length_m <= 0:
                length_m = _num(props.get("LENGTH_M"))
            if length_m <= 0:
                length_m = _num(props.get("length"))

            rows.append(
                {
                    "type": "Feature",
                    "geometry": geom,
                    "properties": {
                        "DUCT_ID": _asset_id(
                            props.get("DUCT_ID") or props.get("DUCT_UID"),
                            tier,
                            "DUCT",
                            serial,
                        ),
                        "TIER": tier,
                        "DUCT_TYPE": props.get("DUCT_TYPE"),
                        "length_m": round(length_m, 2),
                        "WAYS_TOTAL": ways_total,
                        "WAYS_USED": ways_used,
                        "SPARE_WAYS": spare,
                        "OCCUPANCY_PCT": round(occupancy_pct, 1),
                        "SPARE_PCT": round(max(0.0, 100.0 - occupancy_pct), 1),
                        "N_DUCTS": n_ducts,
                        "BUNDLE_LEN_M": _num(props.get("BUNDLE_LEN_M"), length_m),
                        "CLUBS": _int(props.get("CLUBS"), 1),
                        "CABLES_CARRIED": _split_count(props.get("cables_carried")),
                        "PDP_ID": props.get("PDP_ID") or props.get("pdp_ids"),
                        "PARENT_TRENCH": props.get("PARENT_TRENCH"),
                        "START_CHAMBER": props.get("START_CHAMBER"),
                        "END_CHAMBER": props.get("END_CHAMBER"),
                        "INFRA_STATUS": props.get("INFRA_STATUS") or "Proposed",
                        "VERIFY_STATUS": props.get("VERIFY_STATUS") or "Designed",
                        # Marks a row the next run may reuse spare ways from.
                        "REUSABLE": 1 if spare > 0 else 0,
                    },
                }
            )
    return rows


def cable_rows(output_dir: Path) -> List[Dict[str, Any]]:
    """One occupancy row per cable: fibres provided, used, reserved, free."""
    rows: List[Dict[str, Any]] = []
    serial = 0
    for filename, tier in _CABLE_SOURCES:
        for feature in _features(_load(output_dir / filename)):
            geom = feature.get("geometry")
            if not geom:
                continue
            props = feature.get("properties") or {}
            serial += 1

            fiber_count = _int(props.get("FIBER_COUNT"))
            used = _int(props.get("HH_COUNT"), _int(props.get("hhs")))
            if used <= 0 and tier == "Feeder":
                # A feeder cable's fibres in use are the splitter modules it
                # feeds (SPLIT_MODULES = the PDP's splitter demand).
                used = _int(props.get("SPLIT_MODULES"))
            spare = _int(props.get("RESERVED_SPARE_FIBERS"), 2)
            available = _int(props.get("AVAILABLE_FIBERS"))
            if available <= 0 and fiber_count > 0:
                available = max(0, fiber_count - spare - used)

            # Recompute utilisation from the occupancy we actually recorded:
            # the layer's own UTIL_PCT is written by different stages with
            # different denominators (splitter demand vs households), so it is
            # not safe to publish as-is.  Fall back to it only when the cable
            # carries no fibre count to divide by.
            if fiber_count > 0:
                util = 100.0 * used / fiber_count
            else:
                util = _num(props.get("UTIL_PCT"), 0.0)

            length_m = _num(props.get("length_m"))
            if length_m <= 0:
                length_m = _num(props.get("LENGTH_M"))

            rows.append(
                {
                    "type": "Feature",
                    "geometry": geom,
                    "properties": {
                        "CABLE_ID": _asset_id(
                            props.get("cable_id") or props.get("CABLE_ID"),
                            tier,
                            "CABLE",
                            serial,
                        ),
                        "TIER": tier,
                        "CABLE_TYPE": props.get("CABLE_TYPE") or tier,
                        "length_m": round(length_m, 2),
                        "FIBER_COUNT": fiber_count,
                        "FIBRES_USED": used,
                        "SPARE_FIBRES": spare,
                        "AVAILABLE_FIBRES": available,
                        "UTIL_PCT": round(util, 1),
                        "ADDR_COUNT": _split_count(props.get("ADDR_IDS"))
                        or (1 if props.get("addr_id") else 0),
                        "TRUNK_NO": props.get("TRUNK_NO"),
                        "SPLIT_MODULES": props.get("SPLIT_MODULES"),
                        "CONNECTION_TYPE": props.get("CONNECTION_TYPE"),
                        "PDP_ID": props.get("PDP_ID") or props.get("PDP_IDS"),
                        "MFG_ID": props.get("MFG_ID"),
                        "POLYGON_ID": props.get("POLYGON_ID"),
                        # The tier the cable is laid in — the duct it occupies.
                        "DUCT_TIER": tier,
                        "INFRA_STATUS": props.get("INFRA_STATUS") or "Proposed",
                        "VERIFY_STATUS": props.get("VERIFY_STATUS") or "Designed",
                    },
                }
            )
    return rows


def publish(output_dir: Path) -> Dict[str, int]:
    """Derive the occupancy rows for this run.

    Returns ``{"ducts": n, "cables": m}`` row counts. The rows are NOT written
    as map layers — they are stored in the ``gis.duct_occupancy`` /
    ``gis.cable_occupancy`` tables by the engine's ingest (see ``store``).
    Never raises: an occupancy derivation that fails must not fail a completed
    pipeline run.
    """
    summary = {"ducts": 0, "cables": 0}
    try:
        summary["ducts"] = len(duct_rows(output_dir))
    except Exception:  # noqa: BLE001 - derived artefact, never fatal
        pass
    try:
        summary["cables"] = len(cable_rows(output_dir))
    except Exception:  # noqa: BLE001
        pass
    return summary


def store(output_dir: Path, project_id: str) -> Dict[str, int]:
    """Derive the occupancy rows and load them into PostGIS (no map layers).

    The registry is data for the next run / the LLD (brownfield capacity
    read-back), not a design layer, so it goes straight to the database and
    does not appear on the results map or in the downloads.
    Returns the row counts stored; ``{}`` when PostGIS is unavailable.
    Never raises.
    """
    from . import postgis  # local import: occupancy is also usable standalone

    try:
        if not postgis.is_available():
            return {}
        postgis.init_schema()
        stored = {"ducts": 0, "cables": 0}
        ducts = duct_rows(output_dir)
        if ducts:
            stored["ducts"] = postgis.store_occupancy(
                project_id, "duct_occupancy", ducts)
        cables = cable_rows(output_dir)
        if cables:
            stored["cables"] = postgis.store_occupancy(
                project_id, "cable_occupancy", cables)
        return stored
    except Exception:  # noqa: BLE001 - derived artefact, never fatal
        return {}


def publish_for_project(output_dir: Path) -> Dict[str, int]:
    """Alias kept explicit for callers that read as ``occupancy.publish_for_project``."""
    return publish(output_dir)
