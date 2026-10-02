"""Offline fibre/BOQ audit for a completed HLD GeoPackage output directory.

Run from the repo root with Anaconda Python after the full HLD run:

    env -u PYTHONPATH -u PYTHONHOME python HLD_Planning_01/tools/audit_fiber_boq.py <run-dir>

This audit is read-only and reports every size/status exception before exiting
non-zero. It validates the actual cable GeoPackages as well as the production
BOQ's cable code mapping.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from unittest import mock

ENGINE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE_ROOT.parent
FIBER_BACKEND = REPO_ROOT / "fiber-backend"
for import_path in (ENGINE_ROOT, FIBER_BACKEND):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.test_settings")
import django  # noqa: E402
django.setup()

from osgeo import ogr  # noqa: E402
from ftth_hld import boq  # noqa: E402
from HLDPlanning.utils.cable_capacity import (  # noqa: E402
    DISTRIBUTION_FIBER_LADDER,
    DROP_FIBER_LADDER,
    RESERVED_SPARE_FIBERS,
    distribution_fiber_capacity,
    drop_capacity_warning,
    drop_fiber_capacity,
)


def _features(path: Path):
    ds = ogr.Open(str(path), 0)
    if ds is None:
        raise RuntimeError(f"Could not read {path}")
    layer = ds.GetLayer(0)
    names = [field.name for field in layer.schema]
    output = []
    for feature in layer:
        props = {name: feature.GetField(name) for name in names}
        output.append({
            "type": "Feature",
            "geometry": None,
            "properties": props,
            "_fid": feature.GetFID(),
        })
    ds = None
    return output


def _production_boq(cables: list[dict]) -> tuple[dict, list[dict]]:
    """Run production quantity/row assembly against supplied persisted-style features."""
    class Layer:
        def __init__(self, features):
            self.geojson = {"type": "FeatureCollection", "features": features}

    empty = Layer([])
    cable_layer = Layer(cables)
    with (
        mock.patch.object(
            boq, "_get_layer",
            side_effect=lambda _project, name: cable_layer if name == "cables" else empty,
        ),
        mock.patch.object(boq, "_count_otb_tiers", return_value={}),
        mock.patch.object(boq.BoqRate.objects, "filter", return_value=[]),
    ):
        quantities = boq.compute_quantities("fiber-ladder-audit")
        rows = boq.build_boq_rows(quantities)
    return quantities, rows


def audit(run_dir: Path) -> dict:
    failures = []
    counters = Counter()
    boq_rows = []
    cables = []
    for layer_name in ("Feeder_Cable", "Distribution_Cable", "Aerial_Cable"):
        path = run_dir / f"{layer_name}.gpkg"
        if not path.exists():
            continue
        features = _features(path)
        cables.extend(features)
        for feature in features:
            props = feature["properties"]
            cable_type = str(props.get("CABLE_TYPE") or (
                "Feeder" if layer_name == "Feeder_Cable" else "Distribution"
            )).strip()
            normalized = cable_type.lower()
            try:
                fiber_count = int(float(props.get("FIBER_COUNT") or 0))
                hh_count = int(float(props.get("HH_COUNT") or props.get("hhs") or 0))
                spare = int(float(props.get("RESERVED_SPARE_FIBERS") or 0))
            except (TypeError, ValueError):
                failures.append(f"{layer_name} FID {feature['_fid']}: invalid fibre/HH/spare attributes")
                continue
            key = (layer_name, cable_type, fiber_count)
            counters[key] += 1
            drop_like = normalized in ("drop", "aerial")
            is_trunk = normalized == "distribution"
            if drop_like:
                if fiber_count not in DROP_FIBER_LADDER:
                    failures.append(f"{layer_name} FID {feature['_fid']}: drop size {fiber_count} is outside {DROP_FIBER_LADDER}")
                    continue
                expected = drop_fiber_capacity(hh_count)
                expected_size = expected if expected is not None else DROP_FIBER_LADDER[-1]
                status = str(props.get("CAPACITY_STATUS") or "")
                over = drop_capacity_warning(hh_count) is not None
                if fiber_count != expected_size:
                    failures.append(f"{layer_name} FID {feature['_fid']}: {hh_count} HH needs {expected_size}F; found {fiber_count}F")
                if spare != RESERVED_SPARE_FIBERS:
                    failures.append(f"{layer_name} FID {feature['_fid']}: reserved spares {spare}, expected {RESERVED_SPARE_FIBERS}")
                if over:
                    if status != "OVER_CAPACITY" or int(props.get("REVIEW") or 0) != 1:
                        failures.append(f"{layer_name} FID {feature['_fid']}: >286 HH is not flagged OVER_CAPACITY/REVIEW")
                    if not str(props.get("CAPACITY_WARNING") or "").strip():
                        failures.append(f"{layer_name} FID {feature['_fid']}: missing capacity warning")
                    if float(props.get("UTIL_PCT") or 0) > 100:
                        failures.append(f"{layer_name} FID {feature['_fid']}: utilization exceeds 100%")
                elif status not in ("OK", "") or int(props.get("REVIEW") or 0) != 0:
                    failures.append(f"{layer_name} FID {feature['_fid']}: ordinary drop incorrectly flagged for review")
            elif is_trunk:
                if fiber_count not in DISTRIBUTION_FIBER_LADDER and fiber_count < DISTRIBUTION_FIBER_LADDER[-1]:
                    failures.append(f"{layer_name} FID {feature['_fid']}: distribution trunk size {fiber_count} is below/absent from ladder")
                required = max(DISTRIBUTION_FIBER_LADDER[0], hh_count + RESERVED_SPARE_FIBERS)
                if fiber_count < required:
                    failures.append(f"{layer_name} FID {feature['_fid']}: trunk capacity {fiber_count} below {required}F for {hh_count} HH")
            code = boq._cable_item_code(cable_type, str(fiber_count))
            if normalized in ("drop", "aerial") and code is None:
                code = boq._cable_item_code("Drop", str(fiber_count))
            if code is None:
                failures.append(f"{layer_name} FID {feature['_fid']}: no exact BOQ mapping for {cable_type} {fiber_count}F")
            else:
                raw = props.get("LENGTH_M") or props.get("length_m")
                try:
                    quantity = float(raw) if raw is not None else 0.0
                except (TypeError, ValueError):
                    quantity = 0.0
                boq_rows.append({"item_code": code, "item_name": boq._cable_item_name(code), "length_m": quantity})

    over_count = sum(
        1 for cable in cables
        if str(cable["properties"].get("CABLE_TYPE") or "").strip().lower() in ("drop", "aerial")
        and drop_capacity_warning(cable["properties"].get("HH_COUNT"))
    )
    expected_cable_quantities = {
        code: round(sum(row["length_m"] for row in boq_rows if row["item_code"] == code), 2)
        for code in sorted({row["item_code"] for row in boq_rows})
    }
    quantities, production_rows = _production_boq(cables)
    for code, expected in expected_cable_quantities.items():
        actual = quantities.get(code, 0.0)
        if abs(actual - expected) > 0.01:
            failures.append(f"Production BOQ quantity {code} is {actual}m, expected {expected}m from audited cables")
    production_review_count = quantities.get("4.10", 0.0)
    if production_review_count != over_count:
        failures.append(
            f"Production BOQ 4.10 review count is {production_review_count}, expected {over_count}"
        )

    # Exercise the hard capacity boundary with production compute_quantities and
    # build_boq_rows: a 300-HH service location retains 288F and is billed for
    # review, never mistaken for adequately sized capacity.
    synthetic_over = {
        "type": "Feature", "geometry": None,
        "properties": {
            "CABLE_TYPE": "Drop", "FIBER_COUNT": 288, "HH_COUNT": 300,
            "CAPACITY_STATUS": "OVER_CAPACITY", "CAPACITY_WARNING": "requires review",
            "REVIEW": 1, "LENGTH_M": 45.0,
        },
    }
    synthetic_quantities, synthetic_rows = _production_boq([synthetic_over])
    review_row = next((row for row in synthetic_rows if row["item_code"] == "4.10"), None)
    if synthetic_quantities.get("4.17") != 45.0 or synthetic_quantities.get("4.10") != 1.0:
        failures.append("Synthetic 300-HH drop did not produce 45m of 288F cable and one 4.10 review")
    if not review_row or "CAPACITY_WARNING" not in review_row["notes"]:
        failures.append("Synthetic over-capacity drop lacks the BOQ 4.10 engineering-review note")

    return {
        "cable_counts": {" | ".join(map(str, k)): v for k, v in sorted(counters.items())},
        "cable_features": len(cables),
        "capacity_review_drops": over_count,
        "boq_cable_length_m": expected_cable_quantities,
        "production_boq_cable_quantities_m": {
            code: quantities.get(code, 0.0) for code in sorted(expected_cable_quantities)
        },
        "production_boq_review_quantity_4.10": production_review_count,
        "synthetic_300_hh_overcapacity": {
            "drop_288f_quantity_m": synthetic_quantities.get("4.17", 0.0),
            "review_quantity_4.10": synthetic_quantities.get("4.10", 0.0),
            "review_row_name": review_row["item_name"] if review_row else None,
            "review_row_notes": review_row["notes"] if review_row else None,
        },
        "production_boq_rows": len(production_rows),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    report = audit(args.run_dir.resolve())
    print(json.dumps(report, indent=2))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
