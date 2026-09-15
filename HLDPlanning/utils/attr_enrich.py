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
import json
import math
import os

try:
    from osgeo import ogr
    _HAS_OGR = True
except Exception:  # pragma: no cover
    ogr = None
    _HAS_OGR = False


# ── Catalogue defaults (per-deployment tuning points) ───────────────────────

TRENCH_CONSTRUCT = {
    # Construction classes (user spec): the trench network carries ONLY
    # Open Cut / HDD / Garden — the fibre tier (feeder/distribution/drop)
    # is a duct+cable attribute, not a trench property.
    "Open Cut": "Open Cut",
    "HDD": "HDD",
    "Garden": "Garden",     # Micro-Trenching for pseudo-object → object legs
    # Legacy tier tags map onto the construction catalogue so outputs from
    # earlier runs still enrich correctly when re-processed.
    "Feeder": "Open Cut",
    "Distribution": "Open Cut",
    "Drop": "Garden",
    "Hdd": "HDD",
}
TRENCH_WIDTH_MM = {"Open Cut": 300, "HDD": 300, "Garden": 150,
                   "Feeder": 300, "Distribution": 300, "Drop": 150}
TRENCH_DEPTH_MM = {"Open Cut": 900, "HDD": 900, "Garden": 450,
                   "Feeder": 900, "Distribution": 900, "Drop": 450}

DUCT_PROFILE = {
    "Feeder": {"ways": 4, "diameter_mm": 110, "occupied": 1, "duct_type": "4-Way HDPE"},
    "Distribution": {"ways": 2, "diameter_mm": 63, "occupied": 1, "duct_type": "2-Way HDPE"},
    "Drop": {"ways": 1, "diameter_mm": 32, "occupied": 1, "duct_type": "1-Way HDPE"},
}

CABLE_PROFILE = {
    "Feeder": {"fiber_count": 288},
    "Distribution": {"fiber_count": 48},
    "Drop": {"fiber_count": 12},
    # Backward-compat alias for old pipeline outputs.
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
    # Compare case-insensitively: OGR/SQLite field names are, so asking to
    # create LENGTH_M on a layer that already has length_m fails with
    # "A field with the same name already exists" and spams the run log.
    existing = {lyr.GetLayerDefn().GetFieldDefn(i).GetName().lower()
                for i in range(lyr.GetLayerDefn().GetFieldCount())}
    for spec in fields:
        name = spec[0]
        if name.lower() in existing:
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

def enrich_trench_sublayers(out_dir, feedback=None):
    """Write USAGE_TYPE / CONSTRUCT (+ trench_type when missing) onto the
    Feeder/Distribution/Garden sub-layer GPKGs so the per-tier layers carry
    the same civil-infrastructure classification as Final_Trenches."""
    specs = [
        ("Feeder_Trench.gpkg", "Open Cut"),
        ("Distribution_Trench.gpkg", "Open Cut"),
        ("Garden_Trench.gpkg", "Garden"),
        ("Drill_Trench.gpkg", "HDD"),
    ]
    total = 0
    for fname, usage in specs:
        path = os.path.join(out_dir, fname)
        ds, lyr = _open_lyr(path)
        if lyr is None:
            continue
        _create_fields(lyr, [
            ("USAGE_TYPE", ogr.OFTString, 24),
            ("CONSTRUCT", ogr.OFTString, 24),
        ])
        for f in lyr:
            f.SetField("USAGE_TYPE", usage)
            f.SetField("CONSTRUCT", TRENCH_CONSTRUCT.get(usage, "Open Cut"))
            lyr.SetFeature(f)
            total += 1
        ds = None
    if feedback and total:
        feedback.pushInfo(f"  [enrich] Trench sub-layers: {total} classification attributes applied.")
    return total


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
        ("length_m", ogr.OFTReal),
        ("INFRA_STATUS", ogr.OFTString, 24),
    ])
    n = 0
    for f in lyr:
        # trench_type now carries the construction class (Open Cut / HDD /
        # Garden) straight from the pipeline; legacy tier tags (Feeder /
        # Distribution / Drop / Garden-from-old-runs) canonicalise onto it.
        tt = str(_get(lyr, f, "trench_type") or _get(lyr, f, "CONSTRUCT") or
                 _get(lyr, f, "USAGE_TYPE") or "Open Cut")
        tt_canon = tt.strip().title()
        if tt_canon == "Hdd":
            tt_canon = "HDD"
        if tt_canon not in ("Open Cut", "HDD", "Garden"):
            tt_canon = TRENCH_CONSTRUCT.get(tt_canon, "Open Cut")
        # ``sidewalk`` is a STRING field in the delivered GPKG, so the literal
        # "false"/"0" are truthy in Python — that silently flipped every
        # reinstatement to Footpath/Sidewalk. Normalise it explicitly.
        _sw_raw = str(_get(lyr, f, "sidewalk") or "").strip().lower()
        _sw_mixed = _sw_raw == "mixed"
        sidewalk = _sw_raw not in ("", "false", "0", "no", "none", "null")
        f.SetField("USAGE_TYPE", tt_canon)
        f.SetField("CONSTRUCT", TRENCH_CONSTRUCT.get(tt_canon, "Open Cut"))
        f.SetField("WIDTH_MM", TRENCH_WIDTH_MM.get(tt_canon, 300))
        f.SetField("DEPTH_MM", TRENCH_DEPTH_MM.get(tt_canon, 900))
        # A grouped trench (one feature per construction sub-category) carries
        # the whole category, so its runs no longer share one sidewalk flag.
        # trench_layer aggregates it to "Mixed" when they disagree; report the
        # real reinstatement mix instead of defaulting the category to asphalt.
        if _sw_mixed:
            f.SetField("SURFACE", "Mixed (Footpath + Asphalt)")
            f.SetField("REINSTATE", "Mixed (Sidewalk + Road)")
        else:
            f.SetField("SURFACE", "Footpath" if sidewalk else "Asphalt")
            f.SetField("REINSTATE", "Sidewalk" if sidewalk else "Road")
        # The layer carries ``length_m`` (created by the trench layer); OGR
        # field names are case-insensitive, so write that field rather than
        # trying to add a duplicate LENGTH_M column.
        f.SetField("length_m", round(_geom_len_m(f), 1))
        if not _get(lyr, f, "INFRA_STATUS"):
            f.SetField("INFRA_STATUS", "Proposed")
        lyr.SetFeature(f)
        n += 1
    ds = None
    if feedback:
        feedback.pushInfo(f"  [enrich] Final_Trenches: {n} civil attributes applied.")
    return n


# ── Duct segmentation at chambers ───────────────────────────────────────────

def _chamber_points(chamber_path, feedback=None):
    """Read all chamber positions (x, y, struct_id) from Chambers.gpkg."""
    ds, lyr = _open_lyr(chamber_path)
    pts = []
    if lyr is None:
        return pts
    id_idx = lyr.GetLayerDefn().GetFieldIndex("STRUCT_ID")
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        try:
            pt = g.GetPoint(0)
        except Exception:
            continue
        sid = str(f.GetField(id_idx)) if (id_idx >= 0 and f.GetField(id_idx)) else ""
        pts.append((pt[0], pt[1], sid))
    ds = None
    if feedback:
        feedback.pushInfo(f"  [segment] {len(pts)} chamber anchor points loaded.")
    return pts





def _splice_points_into_line(coords, cut_pts, tol_m):
    """Splice chamber positions INTO a polyline as vertices (geometry stays
    one continuous line — ducts run unbroken through chambers).

    Returns (new_coords, hits, sections):
      hits     — chamber ids spliced in, in corridor order
      sections — [(start_chamber|None, end_chamber|None, length_m), …] the
                 chamber-bounded sections along the part (start/end of the
                 whole part are None unless a chamber sits at the endpoint).
    """
    if len(coords) < 2 or not cut_pts:
        return coords, [], []

    def _dist(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    total_len = sum(_dist(coords[i], coords[i + 1]) for i in range(len(coords) - 1))
    if total_len <= 0:
        return coords, [], []

    # Project every chamber onto the part: (arc_pos, xy, id)
    proj = []
    for cx, cy, cid in cut_pts:
        best_d, best_pos, best_xy = None, None, None
        cum = 0.0
        for i in range(len(coords) - 1):
            ax, ay = coords[i]
            bx, by = coords[i + 1]
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            if seg2 == 0:
                continue
            t = ((cx - ax) * dx + (cy - ay) * dy) / seg2
            t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
            qx, qy = ax + t * dx, ay + t * dy
            d = math.hypot(cx - qx, cy - qy)
            if best_d is None or d < best_d:
                best_d, best_pos, best_xy = d, cum + t * math.sqrt(seg2), (qx, qy)
            cum += math.sqrt(seg2)
        if best_d is not None and best_d <= tol_m:
            proj.append((best_pos, best_xy, cid))
    if not proj:
        return coords, [], []
    # Clamp projections that land within 2 m of the part endpoints onto the
    # endpoint itself — a chamber at a corridor start/end becomes the
    # section boundary instead of a sliver stub section.
    clamped = []
    for pos, xy, cid in proj:
        if pos < min(2.0, total_len * 0.01):
            pos = 0.0
        elif total_len - pos < min(2.0, total_len * 0.01):
            pos = total_len
        clamped.append((pos, xy, cid))
    proj = clamped
    # Deduplicate chambers splicing at the same spot; keep corridor order.
    proj.sort(key=lambda p: p[0])
    dedup = []
    for pos, xy, cid in proj:
        if dedup and abs(dedup[-1][0] - pos) < 0.01:
            continue
        dedup.append((pos, xy, cid))
    proj = dedup

    # Splice the projected points into the vertex list, in arc order.
    out = []
    pi = 0
    cum = 0.0
    for i in range(len(coords) - 1):
        ax, ay = coords[i]
        bx, by = coords[i + 1]
        seg_len = _dist(coords[i], coords[i + 1])
        seg_end = cum + seg_len
        if not out:
            out.append([ax, ay])
        # All projections inside this segment are appended IN ORDER before
        # the segment's end vertex — never after it (prevents duplicates).
        while pi < len(proj) and proj[pi][0] < seg_end - 1e-9:
            pos, (qx, qy), _cid = proj[pi]
            if pos > cum + 1e-9:  # skip one sitting exactly on the start vertex
                out.append([qx, qy])
            pi += 1
        out.append([bx, by])
        cum = seg_end
    # Any projection exactly at total_len: the final end vertex is already
    # there (appended above), nothing to do.

    # Section chain: boundaries at 0, each hit arc, total_len. Zero-length
    # boundary sections (chamber exactly on a part endpoint) are dropped.
    boundaries = [0.0] + [p[0] for p in proj] + [total_len]
    ids = [None] + [p[2] for p in proj] + [None]
    sections = []
    for i in range(len(boundaries) - 1):
        L = round(boundaries[i + 1] - boundaries[i], 2)
        if L <= 0:
            continue
        sections.append((ids[i], ids[i + 1], L))
    return out, [p[2] for p in proj], sections


def splice_ducts_at_chambers(feeder_path, dist_path, chamber_path, feedback=None,
                             snap_tol_m=3.0):
    """Splice chamber positions into duct corridors (keeps ducts continuous).

    Feeder and distribution ducts are continuous routed corridors (the feeder
    is a single branched MultiLineString). Chambers sit ON the duct — ducts
    pass through them. This pass:

      1. adds a vertex at every chamber lying on a part (within ``snap_tol_m``)
         so the geometry carries the section breaks, without splitting the
         corridor into separate features;
      2. records the chamber-bounded sections per feature in
         ``SECTIONS_JSON``  [{start, end, length_m}, …]  and the ordered
         chamber chain in ``SECTION_CHAIN`` ("|A|B|C|"), plus ``N_SECTIONS``.

    Feature count is unchanged. START/END_CHAMBER keep their meaning from
    enrich_ducts (endpoints of the corridor). Drop ducts are NOT touched.
    """
    chambers = _chamber_points(chamber_path, feedback)
    if not chambers:
        if feedback:
            feedback.pushInfo("  [splice] No chambers — duct splicing skipped.")
        return 0

    total_spliced = 0
    for path, label in ((feeder_path, "Feeder"), (dist_path, "Distribution")):
        ds, lyr = _open_lyr(path)
        if lyr is None:
            continue
        _create_fields(lyr, [
            ("SECTION_CHAIN", ogr.OFTString, 256),
            ("N_SECTIONS", ogr.OFTInteger),
            ("SECTIONS_JSON", ogr.OFTString, 0),  # 0 = unlimited (GPKG text)
        ])

        n_spliced = 0
        lyr.StartTransaction()
        try:
            for f in lyr:
                g = f.GetGeometryRef()
                if g is None or g.IsEmpty():
                    continue
                multi = g.GetGeometryName().startswith("MULTI")
                parts = []
                if multi:
                    for part in g:
                        pts = part.GetPoints()
                        if pts and len(pts) >= 2:
                            parts.append([(p[0], p[1]) for p in pts])
                else:
                    pts = g.GetPoints()
                    if pts and len(pts) >= 2:
                        parts.append([(p[0], p[1]) for p in pts])

                chain: list = []
                all_sections = []
                changed = False
                new_geoms = []
                for part in parts:
                    new_pts, hits, sections = _splice_points_into_line(
                        part, chambers, snap_tol_m)
                    if hits:
                        changed = True
                        chain.extend(hits)
                        all_sections.extend(sections)
                    new_geoms.append(new_pts)

                if not changed:
                    continue

                def _mk_ls(pts):
                    ls = ogr.Geometry(ogr.wkbLineString)
                    for x, y in pts:
                        ls.AddPoint_2D(x, y)
                    return ls

                if multi:
                    ng = ogr.Geometry(ogr.wkbMultiLineString)
                    for pts in new_geoms:
                        if len(pts) >= 2:
                            ng.AddGeometry(_mk_ls(pts))
                else:
                    ng = _mk_ls(new_geoms[0]) if new_geoms else None
                if ng is None or ng.IsEmpty():
                    continue
                f.SetGeometry(ng)
                f.SetField("SECTION_CHAIN", "|" + "|".join(chain) + "|")
                f.SetField("N_SECTIONS", len(all_sections))
                try:
                    f.SetField("SECTIONS_JSON", json.dumps([
                        {"start": s, "end": e, "length_m": L}
                        for s, e, L in all_sections]))
                except Exception:
                    f.SetField("SECTIONS_JSON", "")
                lyr.SetFeature(f)
                n_spliced += 1
            lyr.CommitTransaction()
        except Exception:
            lyr.RollbackTransaction()
            raise
        ds = None
        total_spliced += n_spliced
        if feedback:
            feedback.pushInfo(
                f"  [splice] {label} ducts: {n_spliced} corridor feature(s) "
                "spliced at chambers (geometry continuous).")
    return total_spliced


def _endpoints(feat):
    """(first_vertex, last_vertex) of a (multi)linestring feature.

    NOTE: ogr's Geometry.GetPoints() returns None on MultiLineString
    geometries — parts must be iterated explicitly. First point of the first
    part and last point of the last part are the run's endpoints."""
    g = feat.GetGeometryRef()
    if g is None or g.IsEmpty():
        return None, None
    first = last = None
    if g.GetGeometryName().startswith("MULTI"):
        for part in g:
            pts = part.GetPoints() if part else None
            if not pts:
                continue
            if first is None:
                first = (pts[0][0], pts[0][1])
            last = (pts[-1][0], pts[-1][1])
    else:
        pts = g.GetPoints()
        if pts:
            first = (pts[0][0], pts[0][1])
            last = (pts[-1][0], pts[-1][1])
    return first, last


def _stamp_run_chambers(path, chamber_path, tol_m=15.0):
    """Populate START_CHAMBER/END_CHAMBER on every duct run from the chambers
    nearest its first/last vertex (within tol_m). Returns count stamped."""
    if not chamber_path or not os.path.isfile(chamber_path):
        return 0
    ds, lyr = _open_lyr(path)
    if lyr is None:
        return 0
    si = lyr.GetLayerDefn().GetFieldIndex("START_CHAMBER")
    ei = lyr.GetLayerDefn().GetFieldIndex("END_CHAMBER")
    if si < 0 and ei < 0:
        ds = None
        return 0
    n = 0
    for f in lyr:
        sx_sy, ex_ey = _endpoints(f)
        if sx_sy is None or ex_ey is None:
            continue
        start_id = _nearest_id(chamber_path, sx_sy[0], sx_sy[1], tol_m, "STRUCT_ID")
        end_id = _nearest_id(chamber_path, ex_ey[0], ex_ey[1], tol_m, "STRUCT_ID")
        changed = False
        if si >= 0 and f.GetField(si) != start_id:
            f.SetField(si, start_id)
            changed = True
        if ei >= 0 and f.GetField(ei) != end_id:
            f.SetField(ei, end_id)
            changed = True
        if changed:
            lyr.SetFeature(f)
            n += 1
    ds = None
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
            # Occupancy comes from the real cables_carried count when the
            # route-based duct builder ran; else fall back to the catalogue
            # default occupancy (1 cable per duct).
            cc = str(_get(lyr, f, "cables_carried") or "")
            n_cab = len([c for c in cc.split(",") if c.strip()]) if cc.strip() else 0
            occupied = n_cab if n_cab > 0 else int(prof.get("occupied", 1))
            occ = (occupied / prof["ways"]) * 100.0
            f.SetField("OCCUPANCY_PCT", round(min(occ, 100.0), 1))
            f.SetField("SPARE_PCT", round(max(0.0, 100.0 - occ), 1))
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
            # The shared-feeder planner writes the real FIBER_COUNT / UTIL_PCT
            # / SPLIT_MODULES; prefer those, fall back to the catalogue profile.
            fc = _num(lyr, f, "FIBER_COUNT", 0) or prof["fiber_count"]
            f.SetField("FIBER_COUNT", int(fc))
            f.SetField("LENGTH_M", round(_geom_len_m(f), 1))
            f.SetField("SOURCE_NODE", mfg_id)
            util = _num(lyr, f, "UTIL_PCT", 0)
            if not util:
                pid = str(_get(lyr, f, "PDP_IDS") or _get(lyr, f, "PDP_ID") or "")
                pid0 = (pid.split(",")[0] if pid else "").upper()
                hh = hh_by_pdp.get(pid0, 0)
                util = min(100.0, (hh / fc) * 100.0)
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
            ("PRIMARY_SPLIT_RATIO", ogr.OFTString, 16),
            ("DISTRIBUTION_SPLIT_RATIO", ogr.OFTString, 16),
            ("SPLITTER_MODULE_COUNT", ogr.OFTInteger),
            ("SPLITTER_TOTAL_PORTS", ogr.OFTInteger),
            ("SPLITTER_USED_PORTS", ogr.OFTInteger),
            ("SPLITTER_SPARE_PORTS", ogr.OFTInteger),
            ("SPLITTER_UTILIZATION_PCT", ogr.OFTReal),
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

            # Use the network-layer splitter plan when available.  Never derive
            # a splitter ratio from cable FIBER_COUNT: a 48-fibre distribution
            # cable is not a 1:48 splitter.
            split = str(_get(lyr, f, "SPLIT_SIZE") or _get(lyr, f, "SPLIT_RATIO") or "")
            split = split if split in {"1:8", "1:16", "1:32", "1:64"} else "1:32"
            module_count = int(_num(lyr, f, "SPLIT_CNT", 0))
            total_ports = int(_num(lyr, f, "SPL_PORTS", 0))
            used_ports = int(_num(lyr, f, "HH", 0))
            if total_ports <= 0:
                total_ports = cap
            if module_count <= 0:
                module_count = 1
            spare_ports = max(0, total_ports - used_ports)
            util_pct = round((100.0 * used_ports / total_ports), 1) if total_ports else 0.0

            f.SetField("SPLIT_RATIO", split)
            f.SetField("PRIMARY_SPLIT_RATIO", "1:8")
            f.SetField("DISTRIBUTION_SPLIT_RATIO", split)
            f.SetField("SPLITTER_MODULE_COUNT", module_count)
            f.SetField("SPLITTER_TOTAL_PORTS", total_ports)
            f.SetField("SPLITTER_USED_PORTS", used_ports)
            f.SetField("SPLITTER_SPARE_PORTS", spare_ports)
            f.SetField("SPLITTER_UTILIZATION_PCT", util_pct)
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
            ("PRIMARY_SPLIT_RATIO", ogr.OFTString, 16),
            ("DISTRIBUTION_SPLIT_RATIO", ogr.OFTString, 16),
            ("SPLITTER_MODULE_COUNT", ogr.OFTInteger),
            ("SPLITTER_TOTAL_PORTS", ogr.OFTInteger),
            ("SPLITTER_USED_PORTS", ogr.OFTInteger),
            ("SPLITTER_SPARE_PORTS", ogr.OFTInteger),
            ("SPLITTER_UTILIZATION_PCT", ogr.OFTReal),
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
    # Splice chambers into the duct corridors FIRST (continuous geometry —
    # ducts run unbroken through chambers; sections recorded in attributes).
    # enrich_ducts then stamps catalogue attrs + endpoint chambers on the
    # still-whole corridors.
    try:
        n_sp = splice_ducts_at_chambers(
            p("Feeder_Ducts.gpkg"), p("Distribution_Ducts.gpkg"),
            p("Chambers.gpkg"), feedback,
        )
        if feedback and n_sp:
            feedback.pushInfo(
                f"  [enrich] Duct splicing: {n_sp} corridor(s) spliced at chambers.")
    except Exception as exc:
        if feedback:
            feedback.pushInfo(f"  [splice] Duct splicing skipped: {exc}")

    n += enrich_trenches(p("Final_Trenches.gpkg"), feedback)
    n += enrich_trench_sublayers(out_dir, feedback)
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
