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


# ── Road attribution per trench SECTION ─────────────────────────────────────
# The Final_Trenches layer publishes ONE feature per construction class, each
# holding every continuous run as a geometry part.  A single road class / street
# name for a whole 4 km grouped feature is meaningless: permits are street-wise,
# so each SECTION (part) is attributed to the road it runs along.
#
# This runs in the QGIS python (GDAL present) where the project's roads layer is
# already loaded, and writes a per-section lookup onto the feature:
#   SECTION_STREETS — JSON { "lon,lat": {n: street, f: fclass, h: highway} }
# keyed by the section's MID VERTEX (5 dp ≈ 1 m), which the backend resolves
# without any GDAL.  STREET_NAME carries the dominant street of the feature for
# the map tooltip; N_SECTIONS the part count.

ROAD_SNAP_M = 40.0     # must match permits/analysis/road_class.SNAP_METERS
_ROAD_CELL = 0.002     # ~150-220 m grid cell for the nearest-road lookup
_ROAD_MARGIN = 0.003   # roads beyond this around the AOI can never serve


def _mid_key(lon, lat):
    """Section key — MUST match permits.analysis.sections.coord_key.

    Always WGS84 (lon, lat): the trench GPKG the pipeline writes is projected
    (EPSG:25833) while the roads input and the ingested PostGIS layer are 4326,
    so section mid points are transformed before keying and before the street
    lookup — otherwise the keys never match on the backend side.
    """
    return "%.5f,%.5f" % (float(lon), float(lat))


def _wgs84_transform(src_srs):
    """CRS → EPSG:4326 transform (None when the source is already 4326)."""
    try:
        from osgeo import osr
    except Exception:
        return None
    if src_srs is None:
        return None
    try:
        if src_srs.GetAuthorityCode(None) == "4326":
            return None
        src = src_srs.Clone()
        dst = osr.SpatialReference()
        dst.ImportFromEPSG(4326)
        for srs in (src, dst):
            try:
                srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            except Exception:
                pass
        return osr.CoordinateTransformation(src, dst)
    except Exception:
        return None


def _aoi_wgs84(layer, transform, margin=_ROAD_MARGIN):
    """Layer extent converted to a WGS84 bbox expanded by ``margin`` degrees."""
    ext = layer.GetExtent()
    if not ext or ext[0] > ext[1]:
        return None
    minx, maxx, miny, maxy = ext
    pts = [(minx, miny), (maxx, miny), (minx, maxy), (maxx, maxy),
           ((minx + maxx) / 2, (miny + maxy) / 2)]
    if transform is not None:
        pts = [(transform.TransformPoint(x, y)[0], transform.TransformPoint(x, y)[1])
               for x, y in pts]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs) - margin, max(xs) + margin, min(ys) - margin, max(ys) + margin)


def _line_parts(geom):
    """Every LineString inside a (multi)line geometry as a coordinate list."""
    parts = []
    if geom is None or geom.IsEmpty():
        return parts
    name = geom.GetGeometryName().upper()
    if "MULTI" in name or name in ("GEOMETRYCOLLECTION", "POLYGON", "MULTIPOLYGON"):
        for i in range(geom.GetGeometryCount()):
            parts.extend(_line_parts(geom.GetGeometryRef(i)))
        return parts
    if name == "LINESTRING":
        coords = [(geom.GetX(i), geom.GetY(i)) for i in range(geom.GetPointCount())]
        if len(coords) >= 2:
            parts.append(coords)
    return parts


class _RoadIndex:
    """Nearest-road lookup (name + fclass) for section mid vertices.

    Roads are split into consecutive-point segments, clipped to the trench AOI
    and bucketed into a coarse lon/lat grid, so a nearest query inspects a
    handful of candidates instead of the whole 50k-road extract.  Distances are
    metres via a local equirectangular approximation — exact enough for the
    40 m snap the attribution uses.
    """

    def __init__(self, roads_source, aoi, feedback=None):
        self._cell = _ROAD_CELL
        self._grid = {}
        self._keys = []
        minx, maxx, miny, maxy = aoi
        n = 0
        roads_lyr, _roads_ds = _as_ogr_layer(roads_source)
        if roads_lyr is None:
            if feedback:
                feedback.pushInfo("  [street] roads layer not readable — skipped.")
            return
        d = roads_lyr.GetLayerDefn()
        i_name = next((d.GetFieldIndex(f) for f in ("name", "NAME", "street", "STREET")
                       if d.GetFieldIndex(f) >= 0), -1)
        i_class = next((d.GetFieldIndex(f) for f in ("fclass", "FCLASS", "highway", "class")
                        if d.GetFieldIndex(f) >= 0), -1)
        for feat in roads_lyr:
            name = (feat.GetField(i_name) or "") if i_name >= 0 else ""
            fclass = (feat.GetField(i_class) or "") if i_class >= 0 else ""
            if not name and not fclass:
                continue
            for coords in _line_parts(feat.GetGeometryRef()):
                for i in range(len(coords) - 1):
                    (ax, ay), (bx, by) = coords[i], coords[i + 1]
                    if (max(ax, bx) < minx or min(ax, bx) > maxx
                            or max(ay, by) < miny or min(ay, by) > maxy):
                        continue
                    idx = len(self._keys)
                    self._keys.append((ax, ay, bx, by, str(name), str(fclass)))
                    n += 1
                    for cx in range(int(min(ax, bx) / self._cell), int(max(ax, bx) / self._cell) + 1):
                        for cy in range(int(min(ay, by) / self._cell), int(max(ay, by) / self._cell) + 1):
                            self._grid.setdefault((cx, cy), []).append(idx)
        _roads_ds = None  # segments are copied into the index; release GDAL
        if feedback:
            feedback.pushInfo(
                f"  [street] road index: {n} segment(s) inside the AOI "
                f"({len(self._grid)} grid cell(s)).")

    def nearest(self, lon, lat, snap_m=ROAD_SNAP_M):
        """Nearest road within ``snap_m`` metres: (street, fclass) or None."""
        if not self._grid:
            return None
        kx = 111320.0 * math.cos(math.radians(lat))
        ky = 110540.0
        cx0 = int(lon / self._cell)
        cy0 = int(lat / self._cell)
        best = None
        best_d = snap_m
        # ±2 cells ≈ ±300 m lon / ±440 m lat — more than enough for a 40 m snap.
        for cx in range(cx0 - 2, cx0 + 3):
            for cy in range(cy0 - 2, cy0 + 3):
                for idx in self._grid.get((cx, cy), ()):
                    ax, ay, bx, by, name, fclass = self._keys[idx]
                    axm, aym = (ax - lon) * kx, (ay - lat) * ky
                    bxm, bym = (bx - lon) * kx, (by - lat) * ky
                    dx, dy = bxm - axm, bym - aym
                    L = dx * dx + dy * dy
                    if L <= 0:
                        dist = math.hypot(axm, aym)
                    else:
                        t = -(axm * dx + aym * dy) / L
                        t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                        dist = math.hypot(axm + t * dx, aym + t * dy)
                    if dist < best_d:
                        best_d = dist
                        best = (name, fclass)
        return best


def _as_ogr_layer(source):
    """Accept an OGR layer, a QgsVectorLayer or a path → (OGR layer, dataset).

    The pipeline hands over a ``QgsVectorLayer``; the OGR API used here needs a
    plain OGR layer, so the layer's ``source()`` is opened with GDAL.  QGIS
    sources can carry ``|layername=…`` suffixes and ``/vsizip/`` prefixes, both
    of which OGR understands.
    """
    if source is None or not _HAS_OGR:
        return None, None
    if hasattr(source, "GetLayerDefn"):        # already an OGR layer
        return source, None
    path = source.source() if hasattr(source, "source") else source
    if not isinstance(path, str) or not path:
        return None, None
    for candidate in (path, path.split("|")[0], "/vsizip/" + path.split("|")[0]):
        try:
            ds = ogr.Open(candidate, 0)
        except Exception:
            ds = None
        if ds is not None:
            lyr = ds.GetLayer(0)
            if lyr is not None:
                return lyr, ds
            ds = None
    return None, None


def _aoi_of(layer, margin=_ROAD_MARGIN):
    """Extent of a layer expanded by ``margin`` degrees (bbox union)."""
    ext = layer.GetExtent()
    if not ext or ext[0] > ext[1]:
        return None
    return (ext[0] - margin, ext[1] + margin, ext[2] - margin, ext[3] + margin)


def attribute_section_streets(trench_path, roads_source, feedback=None):
    """Give every trench SECTION the street it runs along (see _RoadIndex)."""
    ds, lyr = _open_lyr(trench_path)
    if lyr is None or roads_source is None:
        return 0
    _create_fields(lyr, [
        ("SECTION_STREETS", ogr.OFTString, 0),
        ("STREET_NAME", ogr.OFTString, 96),
        ("N_SECTIONS", ogr.OFTInteger),
    ])
    transform = _wgs84_transform(lyr.GetSpatialRef())
    aoi = _aoi_wgs84(lyr, transform)
    idx = _RoadIndex(roads_source, aoi or (-180.0, 180.0, -90.0, 90.0), feedback)
    total = 0
    named = 0
    for f in lyr:
        parts = _line_parts(f.GetGeometryRef())
        sections = {}
        counts = {}
        for part in parts:
            mid = part[len(part) // 2]
            lon, lat = mid[0], mid[1]
            if transform is not None:
                try:
                    lon, lat = transform.TransformPoint(mid[0], mid[1])[:2]
                except Exception:
                    continue
            hit = idx.nearest(lon, lat)
            if not hit:
                continue
            street, fclass = hit
            sections[_mid_key(lon, lat)] = {"n": street, "f": fclass}
            if street:
                counts[street] = counts.get(street, 0) + 1
        f.SetField("SECTION_STREETS", json.dumps(sections, ensure_ascii=False))
        f.SetField("N_SECTIONS", len(parts))
        if counts:
            f.SetField("STREET_NAME", max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0])
            named += 1
        lyr.SetFeature(f)
        total += len(sections)
    ds = None
    if feedback:
        feedback.pushInfo(
            f"  [street] Final_Trenches: {total} section(s) attributed to a road "
            f"across {named} feature(s).")
    return total


def enrich_trenches(trench_path, feedback=None, roads_lyr=None):
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
    ds, lyr = None, None
    if feedback:
        feedback.pushInfo(f"  [enrich] Final_Trenches: {n} civil attributes applied.")
    # Street attribution needs the roads layer, so it runs after the attribute
    # pass (which reopened the layer once already).
    if roads_lyr is not None:
        try:
            attribute_section_streets(trench_path, roads_lyr, feedback)
        except Exception as exc:
            if feedback:
                feedback.pushInfo(f"  [street] Section attribution skipped: {exc}")
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
            # One component per tier carries the WHOLE chamber chain, so this
            # is unlimited text (a 256-char cap truncated the distribution
            # chain at ~1/25th of its length).
            ("SECTION_CHAIN", ogr.OFTString, 0),   # 0 = unlimited (GPKG text)
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
            # A grouped component holds every route of its tier, so its ways
            # are the AGGREGATE (WAYS_USED over WAYS_TOTAL) rather than one
            # duct profile — otherwise seven feeder cables on a 4-way profile
            # read as 175 % occupancy. Fall back to the single-duct rule when
            # the aggregates are absent (older outputs / drop ducts).
            total_ways = 0
            try:
                total_ways = int(_num(lyr, f, "WAYS_TOTAL", 0))
            except Exception:
                total_ways = 0
            used_ways = 0
            try:
                used_ways = int(_num(lyr, f, "WAYS_USED", 0))
            except Exception:
                used_ways = 0
            if total_ways > 0 and used_ways > 0:
                occ = (used_ways / total_ways) * 100.0
            else:
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
            # A grouped component (one feature per tier) no longer begins at a
            # chamber — its first vertex is simply wherever the first member
            # run started. Take the corridor's endpoints from the chamber
            # section chain instead: first chamber reached → last chamber
            # reached. Falls back to the geometric values above when the
            # corridor has no chamber splices.
            sec_json = str(_get(lyr, f, "SECTIONS_JSON") or "")
            if sec_json:
                try:
                    secs = json.loads(sec_json)
                except Exception:
                    secs = []
                if isinstance(secs, list) and secs:
                    starts = [str(s.get("start")) for s in secs
                              if isinstance(s, dict) and s.get("start")]
                    ends = [str(s.get("end")) for s in secs
                            if isinstance(s, dict) and s.get("end")]
                    if starts:
                        f.SetField("START_CHAMBER", starts[0])
                    if ends:
                        f.SetField("END_CHAMBER", ends[-1])
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

def enrich_all(out_dir, feedback=None, roads_lyr=None):
    """Enrich every pipeline GPKG inside out_dir (no-op when out_dir is empty).

    ``roads_lyr`` is the project's roads input layer (a QgsVectorLayer).  It is
    only used to attribute each trench SECTION to the road it runs along — the
    one attribute that cannot be derived from the pipeline's own geometry.
    """
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
    # Splice chambers into the duct corridors (continuous geometry —
    # ducts run unbroken through chambers; sections recorded in attributes).
    # The duct stage already publishes ONE component per tier (feeder =
    # MFG → every PDP, distribution = PDP → pseudo objects), so the chamber
    # chain is recorded on that single component.  enrich_ducts then stamps
    # catalogue attrs + endpoint chambers on the still-whole corridors.
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

    n += enrich_trenches(p("Final_Trenches.gpkg"), feedback, roads_lyr=roads_lyr)
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
