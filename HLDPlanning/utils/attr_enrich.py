# -*- coding: utf-8 -*-
"""
HLD_attr catalogue enrichment.

Post-processes the pipeline's saved GPKG outputs in place (via OGR) to add
the attribute set described in HLD_attr.docx:

  Trench   : construct, width/depth, surface, reinstatement
  Duct     : parent trench, duct type, diameter, ways, start/end chamber,
             occupancy / spare capacity
  Cable    : cable type, fibre count, length, utilization, source node
  Equipment: equip type/name, location, capacity, split ratio, vendor,
             power requirement, maintenance zone

IMPORTANT — field creation pattern: all new fields are created UP FRONT,
before any feature iteration.  Calling CreateField() and then SetField() on
a feature read before the schema change writes out of bounds in GDAL and
segfaults.  Every function here therefore:
  1. builds the layer's full list of (name, type, width) additions,
  2. creates them in one pass,
  3. then iterates features and sets values.
"""
import os

try:
    from osgeo import ogr
    _HAS_OGR = True
except Exception:  # pragma: no cover
    ogr = None
    _HAS_OGR = False


# ── Catalogue defaults (per-deployment tuning points) ───────────────────────

TRENCH_CONSTRUCT = {
    "Feeder": "Open Cut",
    "Distribution": "Open Cut",
    "Garden": "Micro Trench",
}
TRENCH_WIDTH_MM = {"Feeder": 300, "Distribution": 300, "Garden": 150}
TRENCH_DEPTH_MM = {"Feeder": 900, "Distribution": 900, "Garden": 450}

DUCT_PROFILE = {
    "Feeder": {"ways": 4, "diameter_mm": 110, "occupied": 1, "duct_type": "4-Way HDPE"},
    "Distribution": {"ways": 2, "diameter_mm": 63, "occupied": 1, "duct_type": "2-Way HDPE"},
    "Drop": {"ways": 1, "diameter_mm": 32, "occupied": 1, "duct_type": "1-Way HDPE"},
}

CABLE_PROFILE = {
    "Feeder": {"fiber_count": 288},
    "Distribution": {"fiber_count": 48},
    "Garden": {"fiber_count": 12},
}

PDP_SPARE_CAP = "SPL_PORTS"
MFG_SPARE_CAP = 288


# ── low-level helpers ────────────────────────────────────────────────────────

def _open_lyr(path):
    if not _HAS_OGR or not path or not os.path.isfile(path):
        return None, None
    try:
        ds = ogr.Open(path, 1)  # update
    except Exception:
        return None, None
    if ds is None:
        return None, None
    lyr = ds.GetLayer(0)
    if lyr is None:
        ds = None
        return None, None
    return ds, lyr


def _create_fields(lyr, fields):
    """Create all (name, type[, width]) fields up front, before iterating."""
    if lyr is None:
        return
    existing = {lyr.GetLayerDefn().GetFieldDefn(i).GetName()
                for i in range(lyr.GetLayerDefn().GetFieldCount())}
    for spec in fields:
        name = spec[0]
        if name in existing:
            continue
        ftype = spec[1] if len(spec) > 1 else ogr.OFTString
        width = spec[2] if len(spec) > 2 else 48
        try:
            fld = ogr.FieldDefn(name, ftype)
            if ftype == ogr.OFTString:
                fld.SetWidth(width)
            lyr.CreateField(fld)
        except Exception:
            pass


def _get(lyr, feat, name):
    i = lyr.GetLayerDefn().GetFieldIndex(name)
    if i < 0:
        return None
    return feat.GetField(i)


def _num(lyr, feat, name, default=0):
    try:
        v = _get(lyr, feat, name)
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _geom_len_m(feat):
    g = feat.GetGeometryRef()
    if g is None or g.IsEmpty():
        return 0.0
    try:
        return g.Length()
    except Exception:
        return 0.0


def _line_points(feat):
    g = feat.GetGeometryRef()
    if g is None or g.IsEmpty():
        return
    try:
        if g.GetGeometryName().startswith("MULTI"):
            for part in g:
                for i in range(part.GetPointCount()):
                    pt = part.GetPoint(i)
                    yield pt[0], pt[1]
        else:
            for i in range(g.GetPointCount()):
                pt = g.GetPoint(i)
                yield pt[0], pt[1]
    except Exception:
        return


def _nearest_id(path, x, y, tol_m, id_fields):
    """Find the value of the first populated id field on the feature nearest to (x, y)."""
    if isinstance(id_fields, str):
        id_fields = [id_fields]
    ds, lyr = _open_lyr(path)
    if lyr is None:
        return ""
    best = ""
    best_d = tol_m
    dg = ogr.Geometry(ogr.wkbPoint)
    dg.AddPoint(x, y)
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        try:
            dist = g.Distance(dg)
        except Exception:
            continue
        if dist <= best_d:
            best_d = dist
            for fld in id_fields:
                v = _get(lyr, f, fld)
                if v not in (None, ""):
                    best = str(v)
                    break
    ds = None
    return best


def _first_field_value(path, field):
    ds, lyr = _open_lyr(path)
    if lyr is None:
        return ""
    val = ""
    f = lyr.GetNextFeature()
    if f is not None:
        val = str(_get(lyr, f, field) or "")
    ds = None
    return val


# ── Trench enrichment ────────────────────────────────────────────────────────

def enrich_trenches(trench_path, feedback=None):
    ds, lyr = _open_lyr(trench_path)
    if lyr is None:
        return 0
    _create_fields(lyr, [
        ("USAGE_TYPE", ogr.OFTString, 24),
        ("CONSTRUCT", ogr.OFTString, 24),
        ("WIDTH_MM", ogr.OFTInteger),
        ("DEPTH_MM", ogr.OFTInteger),
        ("SURFACE", ogr.OFTString, 24),
        ("REINSTATE", ogr.OFTString, 24),
        ("LENGTH_M", ogr.OFTReal),
        ("INFRA_STATUS", ogr.OFTString, 24),
    ])
    n = 0
    for f in lyr:
        tt = str(_get(lyr, f, "trench_type") or _get(lyr, f, "USAGE_TYPE") or "")
        tt_canon = tt.strip().title() or "Distribution"
        sidewalk = str(_get(lyr, f, "sidewalk") or "")
        f.SetField("USAGE_TYPE", tt_canon)
        f.SetField("CONSTRUCT", TRENCH_CONSTRUCT.get(tt_canon, "Open Cut"))
        f.SetField("WIDTH_MM", TRENCH_WIDTH_MM.get(tt_canon, 300))
        f.SetField("DEPTH_MM", TRENCH_DEPTH_MM.get(tt_canon, 900))
        f.SetField("SURFACE", "Footpath" if sidewalk else "Asphalt")
        f.SetField("REINSTATE", "Sidewalk" if sidewalk else "Road")
        f.SetField("LENGTH_M", round(_geom_len_m(f), 1))
        if not _get(lyr, f, "INFRA_STATUS"):
            f.SetField("INFRA_STATUS", "Proposed")
        lyr.SetFeature(f)
        n += 1
    ds = None
    if feedback:
        feedback.pushInfo(f"  [enrich] Final_Trenches: {n} civil attributes applied.")
    return n


# ── Duct enrichment ──────────────────────────────────────────────────────────

def enrich_ducts(feeder_path, dist_path, drop_path, trench_path, chamber_path, feedback=None):
    total = 0
    for path, profile_key in (
        (feeder_path, "Feeder"),
        (dist_path, "Distribution"),
        (drop_path, "Drop"),
    ):
        ds, lyr = _open_lyr(path)
        if lyr is None:
            continue
        _create_fields(lyr, [
            ("DUCT_TYPE", ogr.OFTString, 24),
            ("WAYS", ogr.OFTInteger),
            ("DIAMETER_MM", ogr.OFTInteger),
            ("LENGTH_M", ogr.OFTReal),
            ("OCCUPANCY_PCT", ogr.OFTReal),
            ("SPARE_PCT", ogr.OFTReal),
            ("PARENT_TRENCH", ogr.OFTString, 32),
            ("START_CHAMBER", ogr.OFTString, 16),
            ("END_CHAMBER", ogr.OFTString, 16),
            ("INFRA_STATUS", ogr.OFTString, 24),
        ])
        prof = DUCT_PROFILE[profile_key]
        for f in lyr:
            f.SetField("DUCT_TYPE", prof["duct_type"])
            f.SetField("WAYS", prof["ways"])
            f.SetField("DIAMETER_MM", prof["diameter_mm"])
            f.SetField("LENGTH_M", round(_geom_len_m(f), 1))
            occ = (prof["occupied"] / prof["ways"]) * 100.0
            f.SetField("OCCUPANCY_PCT", round(occ, 1))
            f.SetField("SPARE_PCT", round(100.0 - occ, 1))
            f.SetField("INFRA_STATUS", "Proposed")
            pts = list(_line_points(f))
            if pts:
                mid = pts[len(pts) // 2]
                # Distribution ducts route on the sidewalk graph, offset from
                # the trench lines — allow 10 m.  Prefer SRC_ID (stable id),
                # fall back to the numeric row id.
                f.SetField("PARENT_TRENCH", _nearest_id(
                    trench_path, mid[0], mid[1], 10.0, ("SRC_ID", "id")))
                sx, sy = pts[0]
                ex, ey = pts[-1]
                f.SetField("START_CHAMBER", _nearest_id(
                    chamber_path, sx, sy, 15.0, "STRUCT_ID"))
                f.SetField("END_CHAMBER", _nearest_id(
                    chamber_path, ex, ey, 15.0, "STRUCT_ID"))
            lyr.SetFeature(f)
            total += 1
        ds = None
    if feedback:
        feedback.pushInfo(f"  [enrich] Ducts: {total} catalogue attributes applied.")
    return total


# ── Cable enrichment ─────────────────────────────────────────────────────────

def _hh_per_pdp(objects_path):
    """Homes per PDP from the Objects layer.

    PDP ids are normalised to UPPERCASE: the Objects layer carries 'PDP00001'
    while distribution cables/ducts carry lowercase 'pdp00001'.  Without the
    normalisation every utilization lookup misses and reports 0%.
    """
    out = {}
    ds, lyr = _open_lyr(objects_path)
    if lyr is None:
        return out
    for f in lyr:
        pid = str(_get(lyr, f, "PDP_ID") or "").upper()
        if not pid:
            continue
        hh = _num(lyr, f, "HH", 0)
        out[pid] = out.get(pid, 0) + hh
    ds = None
    return out


def enrich_cables(feeder_path, dist_path, objects_path, mfg_path, feedback=None):
    total = 0
    hh_by_pdp = _hh_per_pdp(objects_path)

    ds, lyr = _open_lyr(feeder_path)
    if lyr is not None:
        _create_fields(lyr, [
            ("CABLE_TYPE", ogr.OFTString, 24),
            ("FIBER_COUNT", ogr.OFTInteger),
            ("LENGTH_M", ogr.OFTReal),
            ("SOURCE_NODE", ogr.OFTString, 24),
            ("UTIL_PCT", ogr.OFTReal),
            ("INFRA_STATUS", ogr.OFTString, 24),
        ])
        prof = CABLE_PROFILE["Feeder"]
        mfg_id = _first_field_value(mfg_path, "MFG_ID") or "MFG00001"
        for f in lyr:
            f.SetField("CABLE_TYPE", "Feeder")
            f.SetField("FIBER_COUNT", prof["fiber_count"])
            f.SetField("LENGTH_M", round(_geom_len_m(f), 1))
            f.SetField("SOURCE_NODE", mfg_id)
            pid = str(_get(lyr, f, "PDP_ID") or "").upper()
            hh = hh_by_pdp.get(pid, 0)
            util = min(100.0, (hh / prof["fiber_count"]) * 100.0)
            f.SetField("UTIL_PCT", round(util, 1))
            f.SetField("INFRA_STATUS", "Proposed")
            lyr.SetFeature(f)
            total += 1
        ds = None

    ds, lyr = _open_lyr(dist_path)
    if lyr is not None:
        _create_fields(lyr, [
            ("CABLE_TYPE", ogr.OFTString, 24),
            ("FIBER_COUNT", ogr.OFTInteger),
            ("LENGTH_M", ogr.OFTReal),
            ("SOURCE_NODE", ogr.OFTString, 24),
            ("UTIL_PCT", ogr.OFTReal),
            ("INFRA_STATUS", ogr.OFTString, 24),
        ])
        prof = CABLE_PROFILE["Distribution"]
        for f in lyr:
            f.SetField("CABLE_TYPE", "Distribution")
            f.SetField("FIBER_COUNT", prof["fiber_count"])
            f.SetField("LENGTH_M", round(_geom_len_m(f), 1))
            pid = str(_get(lyr, f, "pdp_id") or _get(lyr, f, "PDP_ID") or "").upper()
            f.SetField("SOURCE_NODE", pid)
            hh = hh_by_pdp.get(pid, 0)
            util = min(100.0, (hh / prof["fiber_count"]) * 100.0)
            f.SetField("UTIL_PCT", round(util, 1))
            f.SetField("INFRA_STATUS", "Proposed")
            lyr.SetFeature(f)
            total += 1
        ds = None
    if feedback:
        feedback.pushInfo(f"  [enrich] Cables: {total} catalogue attributes applied.")
    return total


# ── Equipment enrichment ─────────────────────────────────────────────────────

def enrich_equipment(pdp_path, mfg_path, feedback=None):
    total = 0

    ds, lyr = _open_lyr(pdp_path)
    if lyr is not None:
        _create_fields(lyr, [
            ("EQUIP_TYPE", ogr.OFTString, 16),
            ("EQUIP_NAME", ogr.OFTString, 24),
            ("LOCATION", ogr.OFTString, 24),
            ("EQUIP_CAPACITY", ogr.OFTInteger),
            ("SPLIT_RATIO", ogr.OFTString, 16),
            ("VENDOR", ogr.OFTString, 32),
            ("POWER_REQ", ogr.OFTString, 8),
            ("MAINT_ZONE", ogr.OFTString, 24),
        ])
        for f in lyr:
            pid = str(_get(lyr, f, "PDP_ID") or "")
            f.SetField("EQUIP_TYPE", "PDP")
            f.SetField("EQUIP_NAME", pid)
            f.SetField("LOCATION", "Street Cabinet")
            cap = int(_num(lyr, f, PDP_SPARE_CAP, 0)) or 32
            f.SetField("EQUIP_CAPACITY", cap)
            split = str(_get(lyr, f, "SPLIT_SIZE") or _get(lyr, f, "SPLIT_RATIO") or "")
            f.SetField("SPLIT_RATIO", split or "1:32")
            f.SetField("VENDOR", "")
            f.SetField("POWER_REQ", "Yes")
            f.SetField("MAINT_ZONE", "")
            lyr.SetFeature(f)
            total += 1
        ds = None

    ds, lyr = _open_lyr(mfg_path)
    if lyr is not None:
        _create_fields(lyr, [
            ("EQUIP_TYPE", ogr.OFTString, 16),
            ("EQUIP_NAME", ogr.OFTString, 24),
            ("LOCATION", ogr.OFTString, 24),
            ("EQUIP_CAPACITY", ogr.OFTInteger),
            ("SPLIT_RATIO", ogr.OFTString, 16),
            ("VENDOR", ogr.OFTString, 32),
            ("POWER_REQ", ogr.OFTString, 8),
            ("MAINT_ZONE", ogr.OFTString, 24),
        ])
        for f in lyr:
            f.SetField("EQUIP_TYPE", "MFG")
            f.SetField("EQUIP_NAME", str(_get(lyr, f, "MFG_ID") or "MFG00001"))
            f.SetField("LOCATION", "Central Office")
            f.SetField("EQUIP_CAPACITY", MFG_SPARE_CAP)
            f.SetField("SPLIT_RATIO", "")
            f.SetField("VENDOR", "")
            f.SetField("POWER_REQ", "Yes")
            f.SetField("MAINT_ZONE", "")
            lyr.SetFeature(f)
            total += 1
        ds = None
    if feedback:
        feedback.pushInfo(f"  [enrich] Equipment: {total} catalogue attributes applied.")
    return total


# ── Combined entry point ─────────────────────────────────────────────────────

def enrich_all(out_dir, feedback=None):
    """Enrich every pipeline GPKG inside out_dir (no-op when out_dir is empty)."""
    if not out_dir or not os.path.isdir(out_dir):
        if feedback:
            feedback.pushInfo(
                "  [enrich] No output directory — catalogue attributes skipped "
                "(they are applied to the saved GPKG files)."
            )
        return False

    def p(name):
        return os.path.join(out_dir, name)

    n = 0
    n += enrich_trenches(p("Final_Trenches.gpkg"), feedback)
    n += enrich_ducts(
        p("Feeder_Ducts.gpkg"), p("Distribution_Ducts.gpkg"), p("Drop_Ducts.gpkg"),
        p("Final_Trenches.gpkg"), p("Chambers.gpkg"), feedback,
    )
    n += enrich_cables(
        p("Feeder_Cable.gpkg"), p("Distribution_Cable.gpkg"),
        p("Objects.gpkg"), p("MFG.gpkg"), feedback,
    )
    n += enrich_equipment(p("PDPs.gpkg"), p("MFG.gpkg"), feedback)
    if feedback:
        feedback.pushInfo(
            f"  [enrich] HLD_attr catalogue pass complete ({n} features updated)."
        )
    return True
