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
    # Construction classes: Open Cut / HDD / Garden / **Aerial** — the fibre
    # tier (feeder/distribution/drop) is a duct+cable attribute, not a trench
    # property.
    "Open Cut": "Open Cut",
    "HDD": "HDD",
    "Garden": "Garden",     # Micro-Trenching for pseudo-object → object legs
    # Aerial is a construction class of its OWN (docs/aerial planning.docx):
    # the fibre runs on poles and NOTHING is excavated. These keys are load
    # bearing — without them an aerial row fell through the canonicalisation
    # allow-list below and was stamped "Open Cut", so an aerial drop/duct
    # showed up as an open-cut trench carrying the feeder/distribution tier.
    "Aerial": "Aerial",
    "Aerial Drop": "Aerial",
    "Aerial_Drop": "Aerial",
    "Overhead": "Aerial",
    # Legacy tier tags map onto the construction catalogue so outputs from
    # earlier runs still enrich correctly when re-processed.
    "Feeder": "Open Cut",
    "Distribution": "Open Cut",
    "Drop": "Garden",
    "Hdd": "HDD",
}
# The closed set of construction classes. Anything outside it is canonicalised
# through TRENCH_CONSTRUCT (and an aerial row is forced onto Aerial).
CONSTRUCT_CLASSES = ("Open Cut", "HDD", "Garden", "Aerial")
# Cross-sections. Aerial has NONE — it is not excavated, so a width/depth would
# be a fabricated trench. 0 = "not applicable", never billed as a section.
TRENCH_WIDTH_MM = {"Open Cut": 300, "HDD": 300, "Garden": 150,
                   "Aerial": 0,
                   "Feeder": 300, "Distribution": 300, "Drop": 150}
TRENCH_DEPTH_MM = {"Open Cut": 900, "HDD": 900, "Garden": 450,
                   "Aerial": 0,
                   "Feeder": 900, "Distribution": 900, "Drop": 450}


def is_aerial_row(feature) -> bool:
    """True when an already-classified row is an AERIAL span.

    Two independent pieces of evidence, because either can be the only one
    present depending on the stage that wrote the row:
      * ``EXCAVATION = 0`` — the contract the BOQ and the platform read
        instead of inferring the method from the type string;
      * a construction type / method that says aerial (``TRENCH_TYPE``,
        ``CONSTRUCT``, ``USAGE_TYPE``, ``CONSTRUCTION_METHOD``).
    """
    for name in ("EXCAVATION",):
        v = str(feature.GetField(name) if feature.GetFieldIndex(name) >= 0
                else "").strip().lower()
        if v in ("0", "false", "no"):
            return True
    for name in ("TRENCH_TYPE", "CONSTRUCT", "USAGE_TYPE",
                 "CONSTRUCTION_METHOD"):
        if feature.GetFieldIndex(name) < 0:
            continue
        v = str(feature.GetField(name) or "").strip().lower()
        if v.replace("_", " ").replace("-", " ").strip() == "aerial":
            return True
        if v.startswith("aerial"):
            return True
        if v == "overhead":
            return True
    return False

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


# Lookup layers are static while the enrichment runs, and ``_nearest_id`` is
# called twice per duct row, so the parsed features are kept per process.
_NEAREST_CACHE = {}


def _nearest_geoms(path, id_fields):
    """``[(cloned geometry, id value)]`` for ``path``, cached for the process.

    ``_nearest_id`` used to reopen the GeoPackage and re-walk every feature on
    every call — two calls per duct row (1100 on Berlin), each re-parsing 92
    chambers or 535 trenches.  Once the writes were batched into transactions
    this was the largest remaining cost of the enrichment step.  The lookup
    layer does not change while the enrichment runs, so the geometries are
    cloned once and every later call is only a distance scan.  They are cloned
    rather than borrowed because a borrowed geometry is invalidated when its
    datasource closes.
    """
    key = (str(path), tuple(id_fields))
    hit = _NEAREST_CACHE.get(key)
    if hit is not None:
        return hit
    out = []
    ds, lyr = _open_lyr(path)
    if lyr is not None:
        for f in lyr:
            g = f.GetGeometryRef()
            if g is None or g.IsEmpty():
                continue
            v = ""
            for fld in id_fields:
                val = _get(lyr, f, fld)
                if val not in (None, ""):
                    v = str(val)
                    break
            if v:
                out.append((g.Clone(), v))
        ds = None
    _NEAREST_CACHE[key] = out
    return out


def _nearest_id(path, x, y, tol_m, id_fields):
    """Value of the first populated id field on the feature nearest to (x, y).

    Only features that actually carry one of ``id_fields`` are considered — an
    unnamed structure closer to the point must not mask the nearest named one
    (the callers pass a preference order and take the first that is populated).
    """
    if isinstance(id_fields, str):
        id_fields = [id_fields]
    best = ""
    best_d = tol_m
    dg = ogr.Geometry(ogr.wkbPoint)
    dg.AddPoint(x, y)
    for g, v in _nearest_geoms(path, id_fields):
        try:
            dist = g.Distance(dg)
        except Exception:
            continue
        if dist <= best_d:
            best_d = dist
            best = v
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
        lyr.StartTransaction()
        for f in lyr:
            if is_aerial_row(f):
                # Aerial legs are never excavated: they keep the Aerial class
                # even when they are read out of a per-tier sub-layer file.
                f.SetField("USAGE_TYPE", "Aerial")
                f.SetField("CONSTRUCT", "Aerial")
                lyr.SetFeature(f)
                total += 1
                continue
            # The per-FILE constant is a FALLBACK, not the answer. It used to be
            # stamped unconditionally, so an HDD drill crossing that happens to
            # sit in Feeder_Trench.gpkg was published as Open Cut and one in
            # Garden_Trench.gpkg as Garden — an excavation method it is not, and
            # both the rate card and the permit/TMP rules key off this field.
            # Measured on Berlin: 141 sub-trench rows were mislabelled (36
            # feeder + 92 distribution + 13 garden, all HDD).
            own = ""
            for key in ("trench_type", "TRENCH_TYPE", "USAGE_TYPE", "CONSTRUCT"):
                v = _get(lyr, f, key)
                if v not in (None, ""):
                    own = str(v).strip()
                    break
            cls = TRENCH_CONSTRUCT.get(own.title(), None) if own else None
            if cls not in CONSTRUCT_CLASSES:
                cls = usage
            f.SetField("USAGE_TYPE", cls)
            f.SetField("CONSTRUCT", TRENCH_CONSTRUCT.get(cls, "Open Cut"))
            lyr.SetFeature(f)
            total += 1
        lyr.CommitTransaction()
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
    lyr.StartTransaction()
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
    lyr.CommitTransaction()
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
    lyr.StartTransaction()
    for f in lyr:
        # trench_type now carries the construction class (Open Cut / HDD /
        # Garden) straight from the pipeline; legacy tier tags (Feeder /
        # Distribution / Drop / Garden-from-old-runs) canonicalise onto it.
        tt = str(_get(lyr, f, "trench_type") or _get(lyr, f, "CONSTRUCT") or
                 _get(lyr, f, "USAGE_TYPE") or "Open Cut")
        tt_canon = tt.strip().title()
        if tt_canon == "Hdd":
            tt_canon = "HDD"
        if tt_canon not in CONSTRUCT_CLASSES:
            tt_canon = TRENCH_CONSTRUCT.get(tt_canon, "Open Cut")
        # Defence in depth: a row the pipeline already classified as aerial is
        # NEVER re-stamped as an excavated class, whatever its tier says.
        if tt_canon != "Aerial" and is_aerial_row(f):
            tt_canon = "Aerial"
        # ``sidewalk`` arrives in TWO shapes across the stages:
        #   * a boolean-ish flag from the legacy stage ("true"/"false"/"1"/"0")
        #   * the actual surface NAME from the designer ("Footway"/"Asphalt"/
        #     "Garden" — `design.trench_design._surface_for`)
        # The literal "false"/"0" are truthy in Python, and a surface NAME is
        # truthy too, so reading a name as a flag made every span come out
        # SURFACE=Footpath / REINSTATE=Sidewalk — including the **77 HDD road
        # crossings** the designer had correctly marked Asphalt/Full. That fed
        # the Surface Restoration Plan, the traffic-plan reinstatement text, the
        # permit drawings and TRAFFIC_001. Read a name as a name.
        _sw_raw = str(_get(lyr, f, "sidewalk") or "").strip().lower()
        _sw_mixed = _sw_raw == "mixed"
        sidewalk = _sw_raw not in ("", "false", "0", "no", "none", "null")
        # Road surfaces are not sidewalk: a drilled/dug crossing under asphalt is
        # reinstated as road, which is what the (consumer-side) SURFACE vocabulary
        # "Asphalt" + REINSTATE "Road" already means.
        if _sw_raw in ("asphalt", "road", "carriageway", "street", "tarmac"):
            sidewalk = False
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
    lyr.CommitTransaction()
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





def _splice_points_into_line_UNUSED(coords, cut_pts, tol_m):
    """Retired: chambers are no longer spliced INTO a corridor.

    The network is published as chamber-to-chamber spans instead (see
    ``_segment_layer_at_chambers``), so nothing splices extra vertices into an
    existing line any more. Kept only so old callers fail loudly rather than
    silently, and to be deleted with the next cleanup.
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


# The old ``splice_ducts_at_chambers`` pass (one long corridor carrying a whole
# chamber chain in SECTION_CHAIN / SECTIONS_JSON / N_SECTIONS) was removed: the
# corridor is no longer spliced, it is BROKEN at its chambers — see
# ``segment_ducts_at_chambers``.


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
    lyr.StartTransaction()
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
    lyr.CommitTransaction()
    ds = None
    return n


# ── Chamber-to-chamber segmentation (the selective sequence) ────────────────

SPAN_KIND_CHAMBER = "Chamber span"      # bounded by a chamber at each end
SPAN_KIND_UNCHAMBERED = "Unchambered"   # nothing on it — kept as one run


# The trench network is designed as long routed runs, and the duct pass splices
# every chamber into the duct corridors as an extra vertex so they stay one
# continuous feature. That is the wrong unit for the trench itself: a trench is
# dug chamber to chamber, and the field team, the attribute table and the LLD
# all work span by span. This pass cuts every run at its chamber anchors and
# publishes ONE FEATURE PER SPAN — the selective chamber-to-chamber sequence —
# carrying START_CHAMBER / END_CHAMBER / SPAN_INDEX / SPAN_COUNT / RUN_ID and
# its own length. Runs with no chamber on them stay whole.


def _project_chambers_on_part(coords, chambers, tol_m):
    """Project chamber points onto one polyline part.

    Returns [(arc_pos, (x, y), chamber_id), …] in corridor order, deduped, for
    every chamber within ``tol_m`` of the part. Positions within ~2 m of a part
    endpoint snap onto that endpoint, so a chamber sitting on the run's start
    or end becomes the span boundary instead of a 30 cm stub.
    """
    if len(coords) < 2 or not chambers:
        return []
    cum = [0.0]
    for i in range(len(coords) - 1):
        cum.append(cum[-1] + math.hypot(coords[i + 1][0] - coords[i][0],
                                       coords[i + 1][1] - coords[i][1]))
    total = cum[-1]
    if total <= 0:
        return []
    hits = []
    for cx, cy, cid in chambers:
        best = None
        for i in range(len(coords) - 1):
            ax, ay = coords[i]
            bx, by = coords[i + 1]
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            if seg2 <= 0:
                continue
            t = ((cx - ax) * dx + (cy - ay) * dy) / seg2
            t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
            qx, qy = ax + t * dx, ay + t * dy
            d = math.hypot(cx - qx, cy - qy)
            if best is None or d < best[0]:
                best = (d, cum[i] + t * math.sqrt(seg2), (qx, qy))
        if best is None or best[0] > tol_m:
            continue
        pos, xy = best[1], best[2]
        if pos < min(2.0, total * 0.01):
            pos, xy = 0.0, (coords[0][0], coords[0][1])
        elif total - pos < min(2.0, total * 0.01):
            pos, xy = total, (coords[-1][0], coords[-1][1])
        hits.append((pos, xy, cid))
    hits.sort(key=lambda h: h[0])
    out = []
    for pos, xy, cid in hits:
        if out and abs(out[-1][0] - pos) < 0.5:
            continue  # two structures at the same spot — one boundary
        out.append((pos, xy, cid))
    return out


def _spans_of_part(coords, hits):
    """Cut a polyline part at its chamber hits.

    Returns [(coords, start_id|None, end_id|None), …] — one entry per
    chamber-to-chamber span. A part with no hit returns [] so the caller can
    keep it whole instead.
    """
    if len(coords) < 2 or not hits:
        return []
    cum = [0.0]
    for i in range(len(coords) - 1):
        cum.append(cum[-1] + math.hypot(coords[i + 1][0] - coords[i][0],
                                       coords[i + 1][1] - coords[i][1]))

    spans = []
    start_idx = 0
    # A chamber exactly at the run start becomes the first span's start id.
    start_id = None
    if hits[0][0] <= 1e-9:
        start_id = hits[0][2]
        start_idx = 1
    prev_pos = 0.0
    current = [list(coords[0])]
    for pos, xy, cid in hits[start_idx:]:
        for j in range(1, len(coords) - 1):
            if prev_pos + 1e-9 < cum[j] < pos - 1e-9:
                current.append(list(coords[j]))
        current.append([xy[0], xy[1]])
        if len(current) >= 2:
            spans.append((current, start_id, cid))
        prev_pos = pos
        start_id = cid
        current = [[xy[0], xy[1]]]
    # Trailing piece from the last chamber to the run end (no end chamber).
    for j in range(1, len(coords)):
        if cum[j] > prev_pos + 1e-9:
            current.append(list(coords[j]))
    if len(current) >= 2 and _coords_len(current) > 0.05:
        spans.append((current, start_id, None))
    return [sp for sp in spans if _coords_len(sp[0]) > 0.05]


def _coords_len(coords):
    return sum(math.hypot(coords[i + 1][0] - coords[i][0],
                          coords[i + 1][1] - coords[i][1])
               for i in range(len(coords) - 1))


def _chamber_at(pts, x, y, tol_m=6.0):
    """Nearest chamber id within ``tol_m`` of (x, y), else ''."""
    best, best_d = "", tol_m
    for cx, cy, cid in pts:
        d = math.hypot(cx - x, cy - y)
        if d <= best_d:
            best_d, best = d, cid
    return best


def _segment_layer_at_chambers(path, chambers, feedback=None, label="layer",
                               snap_tol_m=5.0, span_len_fields=(),
                               snap_ends_m=0.0, end_tol_m=6.0):
    """Break every line feature of ``path`` at its chamber anchors.

    Publishes ONE FEATURE PER CHAMBER-TO-CHAMBER SPAN: each feature starts at a
    chamber and ends at the next one (or at the run end when the run finishes
    without a structure). All original attributes are carried over unchanged —
    construction class, surface, reinstatement, PDP/polygon ids, brownfield and
    capacity flags — and the span's own length is written to ``length_m`` /
    ``SPAN_LEN_M`` (BOQ and permits sum these per feature). Features the
    chambers do not touch keep their geometry. Returns the number of spans.

    ``span_len_fields`` names extra fields that must also be rewritten with the
    span's own length. Published ducts pass ``("BUNDLE_LEN_M",)``: each
    published duct row IS one duct, so its bundle metres are its own metres —
    inheriting the pre-cut run's figure onto every span would multiply the
    billed material by the number of chambers on the run.

    ``snap_ends_m`` pulls a labelled span's end vertices onto the chambers they
    name (see ``_snap_ends``). 0 leaves geometry untouched.

    ``end_tol_m`` is how far a span's endpoint may sit from a chamber and still
    be labelled with it.  It must not be tighter than ``snap_tol_m``: a chamber
    that *produced a cut* has to be able to name the end it created.
    """
    ds, lyr = _open_lyr(path)
    if lyr is None:
        return 0

    _create_fields(lyr, [
        ("START_CHAMBER", ogr.OFTString, 24),
        ("END_CHAMBER", ogr.OFTString, 24),
        ("SPAN_INDEX", ogr.OFTInteger),
        ("SPAN_COUNT", ogr.OFTInteger),
        ("SPAN_LEN_M", ogr.OFTReal),
        ("RUN_ID", ogr.OFTString, 40),
        ("SPAN_KIND", ogr.OFTString, 24),
    ])
    defn = lyr.GetLayerDefn()
    i_start = defn.GetFieldIndex("START_CHAMBER")
    i_end = defn.GetFieldIndex("END_CHAMBER")
    i_idx = defn.GetFieldIndex("SPAN_INDEX")
    i_cnt = defn.GetFieldIndex("SPAN_COUNT")
    i_len = defn.GetFieldIndex("SPAN_LEN_M")
    i_run = defn.GetFieldIndex("RUN_ID")
    i_kind = defn.GetFieldIndex("SPAN_KIND")
    i_lm = defn.GetFieldIndex("length_m")
    i_span_len = [defn.GetFieldIndex(nm) for nm in span_len_fields]
    i_span_len = [i for i in i_span_len if i >= 0]

    def _mk_coords(coords):
        ls = ogr.Geometry(ogr.wkbLineString)
        for x, y in coords:
            ls.AddPoint_2D(x, y)
        return ls

    def _mk_line(coords):
        # The published layer is MULTILINESTRING, so every span is written as a
        # single-part MultiLineString — a bare LINESTRING would be a geometry
        # type mismatch for the GeoPackage driver (and for the LLD readers that
        # expect the layer's declared type).
        ml = ogr.Geometry(ogr.wkbMultiLineString)
        ml.AddGeometry(_mk_coords(coords))
        return ml

    # Chamber id -> its coordinate, for pulling a labelled span's ends onto the
    # structure it claims to terminate at.
    chamber_xy = {cid: (cx, cy) for cx, cy, cid in chambers if cid}

    def _snap_ends(feat, start_id, end_id):
        """Pull a span's end vertices onto the chambers it is labelled with.

        The two are snapped to the trench independently, so a structure can sit
        a few metres off the duct's own line (Berlin: p50 0.8 m, p90 5.6 m).  A
        span that is *labelled* chamber-to-chamber but physically stops metres
        short of the chamber is not the component it claims to be, and the next
        stage (LLD continuity, chamber-to-chamber ducts) reads that gap as a
        break.  Only single-part geometry is moved — a branched remainder keeps
        its shape.
        """
        if snap_ends_m <= 0 or not (start_id or end_id):
            return
        g = feat.GetGeometryRef()
        parts = _line_parts(g) if g is not None else []
        if len(parts) != 1 or len(parts[0]) < 2:
            return
        pts = list(parts[0])
        moved = False
        for idx, cid in ((0, start_id), (-1, end_id)):
            p = chamber_xy.get(cid) if cid else None
            if not p:
                continue
            if math.hypot(p[0] - pts[idx][0], p[1] - pts[idx][1]) <= snap_ends_m:
                if (p[0], p[1]) != (pts[idx][0], pts[idx][1]):
                    pts[idx] = (p[0], p[1])
                    moved = True
        if moved:
            feat.SetGeometry(_mk_line(pts))

    def _ends_of(first_pt, last_pt):
        """(start, end) chamber ids for a piece — never the same one twice.

        ``X -> X`` is not a span: the piece leaves a chamber and comes back to
        it, which for duct geometry means a tap/fragment at that structure.
        The span guard below already dropped the end for pieces the chambers
        CUT; the two "publish the piece whole" paths did not, and that is
        exactly where the 90 distribution self-pairs came from.
        """
        s = _chamber_at(chambers, first_pt[0], first_pt[1], tol_m=end_tol_m)
        e = _chamber_at(chambers, last_pt[0], last_pt[1], tol_m=end_tol_m)
        if s and s == e:
            e = ""
        return s, e

    def _stamp(feat, start_id, end_id, index, count, run_id):
        feat.SetField(i_kind, SPAN_KIND_CHAMBER)
        # Snap before measuring: a span's length must be the length of the
        # component it publishes, not of the pre-snap geometry.
        _snap_ends(feat, start_id, end_id)
        length = round(_geom_len_m(feat), 1)
        feat.SetField(i_start, start_id or "")
        feat.SetField(i_end, end_id or "")
        feat.SetField(i_idx, index)
        feat.SetField(i_cnt, count)
        feat.SetField(i_len, length)
        feat.SetField(i_run, run_id)
        if i_lm >= 0:
            feat.SetField(i_lm, length)
        for i_f in i_span_len:
            feat.SetField(i_f, length)

    planned = []
    runs = 0
    cut_runs = 0
    unchambered = 0
    anchors = 0
    for f in lyr:
        g = f.GetGeometryRef()
        parts = _line_parts(g)
        if not parts:
            continue
        runs += 1
        run_id = "RUN-%05d" % f.GetFID()
        spans = []    # chamber-bounded pieces (the selective sequences)
        whole = []    # pieces no chamber touches — one uncut run
        for part in parts:
            hits = _project_chambers_on_part(part, chambers, snap_tol_m)
            anchors += len(hits)
            part_spans = _spans_of_part(part, hits) if hits else []
            if part_spans:
                spans.extend(part_spans)
            else:
                whole.append(part)
        if spans and (len(spans) + (1 if whole else 0)) > 1:
            cut_runs += 1
        planned.append((f, spans, whole, run_id))

    published = 0
    lyr.StartTransaction()
    try:
        for feat, spans, whole, run_id in planned:
            if not spans:
                # Nothing chamber-bounded on this run (``whole`` holds all of
                # its parts): publish it unchanged as a single unchambered run
                # — geometry untouched, metadata stamped — so every trench row
                # states which kind it is.
                first, last = whole[0][0], whole[-1][-1]
                _s_id, _e_id = _ends_of(first, last)
                _stamp(feat, _s_id, _e_id, 1, 1, run_id)
                # A run whose BOTH ends resolve to a chamber is chamber-bounded
                # even when no chamber projects onto its middle — the label has
                # to say so. Forcing UNCHAMBERED here published 432 m and 565 m
                # rows carrying START_CHAMBER and END_CHAMBER while calling
                # themselves "unchambered".
                if not (_s_id and _e_id):
                    feat.SetField(i_kind, SPAN_KIND_UNCHAMBERED)
                    unchambered += 1
                lyr.SetFeature(feat)
                published += 1
                continue
            count = len(spans)
            for pos, (coords, start_id, end_id) in enumerate(spans):
                start_id = start_id or _chamber_at(
                    chambers, coords[0][0], coords[0][1], tol_m=end_tol_m)
                end_id = end_id or _chamber_at(
                    chambers, coords[-1][0], coords[-1][1], tol_m=end_tol_m)
                if start_id and start_id == end_id:
                    # a run that ends at the chamber it started from (a short
                    # tail past the last structure) — X -> X is not a span
                    end_id = ""
                if pos == 0:
                    # reuse the source feature for the first span (keeps the
                    # original attribute row + fid)
                    feat.SetGeometry(_mk_line(coords))
                    _stamp(feat, start_id, end_id, pos + 1, count, run_id)
                    lyr.SetFeature(feat)
                else:
                    clone = ogr.Feature(defn)
                    for i in range(defn.GetFieldCount()):
                        clone.SetField(i, feat.GetField(i))
                    clone.SetGeometry(_mk_line(coords))
                    _stamp(clone, start_id, end_id, pos + 1, count, run_id)
                    lyr.CreateFeature(clone)
                published += 1
            if whole:
                # Everything the chambers do not touch stays ONE feature (a
                # branched corridor must not explode into one row per piece) —
                # published as a single uncut run beside the chamber spans.
                remainder = ogr.Feature(defn)
                for i in range(defn.GetFieldCount()):
                    remainder.SetField(i, feat.GetField(i))
                ml = ogr.Geometry(ogr.wkbMultiLineString)
                for coords in whole:
                    ml.AddGeometry(_mk_coords(coords))
                remainder.SetGeometry(ml)
                first, last = whole[0][0], whole[-1][-1]
                _s_id, _e_id = _ends_of(first, last)
                _stamp(remainder, _s_id, _e_id, 1, 1, run_id)
                if not (_s_id and _e_id):
                    remainder.SetField(i_kind, SPAN_KIND_UNCHAMBERED)
                    unchambered += 1
                lyr.CreateFeature(remainder)
                published += 1
        lyr.CommitTransaction()
    except Exception:
        lyr.RollbackTransaction()
        raise
    ds = None
    if feedback:
        feedback.pushInfo(
            f"  [segment] {label}: {runs} run(s) -> {published} feature(s): "
            f"{published - unchambered} chamber-to-chamber span(s) + "
            f"{unchambered} unchambered run(s) ({cut_runs} run(s) cut at "
            f"{anchors} chamber anchor(s); tol {snap_tol_m:g} m).")
    return published


def segment_trenches_at_chambers(trench_path, chamber_path, feedback=None,
                                 snap_tol_m=5.0):
    """Publish Final_Trenches as chamber-to-chamber spans (see the segmenter)."""
    chambers = _chamber_points(chamber_path, feedback)
    if not chambers:
        if feedback:
            feedback.pushInfo("  [segment] No chambers — trench segmentation skipped.")
        return 0
    return _segment_layer_at_chambers(
        trench_path, chambers, feedback, "Final_Trenches", snap_tol_m)


def segment_ducts_at_chambers(feeder_path, dist_path, chamber_path,
                              feedback=None, snap_tol_m=10.0):
    """Break the feeder + distribution ducts at their chambers.

    Replaces the old chamber *splicing* (one long corridor carrying a
    50-chamber chain in ``SECTION_CHAIN``/``SECTIONS_JSON``): a duct is pulled
    chamber to chamber, so the published unit is the span between two
    structures. ``enrich_ducts`` runs afterwards and stamps the catalogue
    attributes + endpoint chambers on every span. Drop ducts are left alone.

    The cut tolerance is **10 m**, and that number is not a fudge: below the
    feeder tier the ducts are routed on the **sidewalk graph**, which is offset
    from the trench centreline — the same offset ``enrich_ducts`` already allows
    for when it looks up ``PARENT_TRENCH`` ("distribution ducts route on the
    sidewalk graph, offset from the trench lines — allow 10 m").  The chambers
    are snapped to the **trench** (measured: within 0.00 m of it), so at a 3 m
    tolerance the two never met: whole 400-500 m runs stayed uncut and the
    distribution tier published 21 ``Unchambered`` rows, one per region run,
    each of them a corridor passing five to eight chambers.

    ``snap_ends_m`` and ``end_tol_m`` take the same 10 m so a span ends exactly
    on the structure that cut it.
    """
    chambers = _chamber_points(chamber_path, feedback)
    if not chambers:
        if feedback:
            feedback.pushInfo("  [segment] No chambers — duct segmentation skipped.")
        return 0
    total = 0
    for path, label in ((feeder_path, "Feeder ducts"), (dist_path, "Distribution ducts")):
        total += _segment_layer_at_chambers(
            path, chambers, feedback, label, snap_tol_m,
            span_len_fields=("BUNDLE_LEN_M",), snap_ends_m=snap_tol_m,
            end_tol_m=snap_tol_m)
    return total


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
        lyr.StartTransaction()
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
                # the trench lines — allow 10 m.  ``TRENCH_ID`` first, because
                # that is the id the trench stage publishes (`SRC_ID`/`id` are
                # the legacy spellings, and `id` is NULL on every published
                # row): naming only the legacy fields is what left
                # ``PARENT_TRENCH`` blank on all 555 duct rows of a Berlin run
                # while every duct sat 0.00 m on the trench.
                f.SetField("PARENT_TRENCH", _nearest_id(
                    trench_path, mid[0], mid[1], 10.0,
                    ("TRENCH_ID", "SRC_ID", "id")))
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
        lyr.CommitTransaction()
        ds = None
    if feedback:
        feedback.pushInfo(f"  [enrich] Ducts: {total} catalogue attributes applied.")
    return total


# ── Duct tier rules (operator spec, 2026-09-19) ──────────────────────────────
#
# FEEDER       MFG → every PDP, laid ONCE; one duct per chamber pair, with the
#              capacity-balanced trunk CABLES riding inside it. The cables are
#              what is plural, not the duct.
# DISTRIBUTION PDP → the pseudo-object points of the same POLYGON_ID / PDP_ID;
#              one duct, or 2–3 of them, depending on splitter capacity and the
#              profile (2-Way / 4-Way). Multiple per corridor is CORRECT here,
#              so distribution ducts are never folded.
# DROP         one 1-Way duct per premise (unchanged).
# COUPLER      the joint where a drop duct leaves a distribution duct: it must
#              name BOTH ducts, the polygon and the premise.

# Smallest profile that can carry n cables. 4-Way is the feeder default.
_DUCT_WAYS_LADDER = (1, 2, 4, 6, 12)


def _duct_ways_for(n_cables, default=4):
    """Smallest ladder profile that carries ``n_cables``, never below
    ``default``.

    ``default`` is the tier's own profile and acts as a **floor**, not a
    fallback: the feeder profile is 4-Way HDPE sized once for the run, so a
    single trunk cable on it is still a 4-Way duct. Reading it as a fallback
    is what let two Berlin feeder ducts be published as **1-Way** (the ladder
    returns 1 for one cable, and the parameter was never consulted).
    """
    n = max(1, int(n_cables or 1))
    floor = max(1, int(default or 1))
    for w in _DUCT_WAYS_LADDER:
        if w >= n:
            return max(w, floor)
    return max(_DUCT_WAYS_LADDER[-1], floor)


def _split_list(value):
    """Comma-separated attribute → ordered unique list, case preserved."""
    out = []
    for item in str(value or "").split(","):
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return out


# ── Duplicate detection (coverage conservation) ─────────────────────────────
# A dedupe passes only ever deletes a row whose geometry the receiving row
# ALREADY covers. Two rows can name the same chamber pair and still be two
# different paths between those chambers (the duct builder clubs by cable
# proximity, so two clubs can both run A->B on different corridors) — folding
# those onto one row deleted duct the field has to build, and on Berlin the
# feeder layer came out as 7 disconnected pieces with 11 PDPs stranded
# (measured: `merge` dropped 8 rows whose kept row covered 5-50 % of them,
# `absorb` dropped 28 whose span covered 7-13 %).
#
# ``_DBL_COVER_M`` is what counts as "the same corridor" (the clubbing
# tolerance of the duct builder) and ``_DBL_COVER_SHARE`` how much of the row
# must lie on the receiver before deleting it costs no coverage.
_DBL_COVER_M = 0.5
_DBL_COVER_SHARE = 0.95


def _covered_share(geom_a, geom_b, tol_m=_DBL_COVER_M):
    """Share of ``geom_a``'s length lying within ``tol_m`` of ``geom_b``.

    GEOS first (buffer + intersection, exact for the polyline/polyline case
    that matters here); a pure-Python vertex walk is the fallback so a GEOS
    error can never turn a geometry question into a silent delete.
    """
    if geom_a is None or geom_b is None:
        return 0.0
    try:
        la = geom_a.Length()
    except Exception:
        return 0.0
    if la <= 0:
        return 1.0
    try:
        buf = geom_b.Buffer(tol_m, 8)
        inter = geom_a.Intersection(buf)
        if inter is not None:
            return max(0.0, min(1.0, inter.Length() / la))
    except Exception:
        pass
    parts_a = _geom_parts(geom_a)
    parts_b = _geom_parts(geom_b)
    if not parts_a or not parts_b:
        return 0.0
    total = 0.0
    covered = 0.0
    for xy in parts_a:
        for i in range(len(xy) - 1):
            seg = math.hypot(xy[i + 1][0] - xy[i][0], xy[i + 1][1] - xy[i][1])
            if seg <= 0:
                continue
            total += seg
            n = max(2, int(seg) + 1)
            inside = 0
            for k in range(n):
                t = k / (n - 1)
                px = xy[i][0] + t * (xy[i + 1][0] - xy[i][0])
                py = xy[i][1] + t * (xy[i + 1][1] - xy[i][1])
                best = float("inf")
                for by in parts_b:
                    for j in range(len(by) - 1):
                        d = _dist_point_seg(px, py, by[j][0], by[j][1],
                                            by[j + 1][0], by[j + 1][1])
                        if d < best:
                            best = d
                if best <= tol_m:
                    inside += 1
            covered += seg * (inside / n)
    return covered / total if total > 0 else 1.0


def merge_ducts_per_chamber_span(path, feedback=None, label="Feeder ducts"):
    """Fold the ducts that ride the SAME corridor of a chamber pair into ONE.

    Operator rule: *"the feeders … will only follow the path once … one chamber
    to another only one feeder duct will be present."* The route builder emits
    one duct per trunk cable and the trunks share a corridor, so a chamber pair
    carried up to **5** parallel rows (Berlin ``DHH-0017``); tiny segmentation
    fragments whose both ends snap to the same chamber added more.

    One duct is pulled between two chambers and the trunks ride inside it, so
    each group collapses onto its **longest** row (the most complete path
    between the two structures — the others only contributed cables) and the
    ``cables_carried`` / ``pdp_ids`` become the union. Capacity is recomputed
    from the distinct cables actually inside, and ``REVIEW`` is raised when they
    no longer fit the 4-Way profile.

    **A chamber pair can legitimately carry several ducts when they are
    different paths** — the rule is one duct per *corridor*, not one per
    chamber pair across the project — so a row is only folded when the row
    that would keep it actually covers its geometry (``_covered_share``).
    Rows that stand off keep their geometry as their own duct; they are
    reported in the log so a reviewer can see the pair carries two paths.

    Applied to the feeder tier only — the operator spec allows distribution
    ducts to be one or several per corridor.
    """
    ds, lyr = _open_lyr(path)
    if lyr is None:
        return 0
    _create_fields(lyr, [
        ("capacity_used", ogr.OFTReal),
        ("capacity_spare", ogr.OFTReal),
        ("DUCTS_MERGED", ogr.OFTInteger),
    ])
    groups: Dict[Tuple[str, str], List] = {}
    order: List[Tuple[str, str]] = []
    lyr.StartTransaction()
    for f in lyr:
        sc = str(_get(lyr, f, "START_CHAMBER") or "").strip()
        ec = str(_get(lyr, f, "END_CHAMBER") or "").strip()
        if not sc and not ec:
            continue
        key = (sc, ec)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f)

    merged = 0
    dropped = 0
    split_pairs = 0
    kept_paths = 0
    for key in order:
        feats = groups[key]
        if len(feats) < 2:
            continue
        feats.sort(key=lambda f: _geom_len_m(f), reverse=True)
        # Cluster the group by geometry: fold a row only into a representative
        # that already carries its corridor.
        clusters: List[Tuple[Any, List[Any]]] = []
        for f in feats:
            g = f.geometry()
            host = None
            for rep, _mem in clusters:
                rg = rep.geometry()
                if g is None or rg is None:
                    continue
                if _covered_share(g, rg) >= _DBL_COVER_SHARE:
                    host = rep
                    break
            if host is None:
                clusters.append((f, [f]))
            else:
                for entry in clusters:
                    if entry[0] is host:
                        entry[1].append(f)
                        break
        if len(clusters) > 1:
            split_pairs += 1
            kept_paths += len(clusters)
        for keep, members in clusters:
            if len(members) < 2:
                # Its own corridor between the same two chambers — nothing to
                # fold, and deleting it would remove duct no other row has.
                continue
            _fold_duct_group(lyr, keep, members)
            for f in members[1:]:
                lyr.DeleteFeature(f.GetFID())
                dropped += 1
            merged += 1
    lyr.CommitTransaction()
    ds = None
    if feedback and merged:
        feedback.pushInfo(
            f"  [enrich] {label}: {merged} chamber pair corridor(s) folded to ONE "
            f"duct ({dropped} parallel row(s) removed — trunks ride inside).")
    if feedback and split_pairs:
        feedback.pushInfo(
            f"  [enrich] {label}: {split_pairs} chamber pair(s) carry "
            f"{kept_paths} ducts — the rows are different paths (not "
            f"duplicates), so every one keeps its geometry.")
    return merged


def _fold_duct_group(lyr, keep, members):
    """Fold ``members`` into ``keep``: union the cables, re-size the profile."""
    cables: List[str] = []
    pdps: List[str] = []
    for f in members:
        for c in _split_list(_get(lyr, f, "cables_carried")):
            if c not in cables:
                cables.append(c)
        for pdp in _split_list(_get(lyr, f, "pdp_ids")):
            if pdp not in pdps:
                pdps.append(pdp)
    n_cables = len(cables)
    ways = _duct_ways_for(n_cables, 4)
    over = n_cables > ways
    keep.SetField("cables_carried", ",".join(cables))
    keep.SetField("pdp_ids", ",".join(pdps))
    keep.SetField("N_DUCTS", 1)
    keep.SetField("capacity_total", ways)
    keep.SetField("capacity_used", n_cables)
    keep.SetField("capacity_spare", max(0, ways - n_cables))
    keep.SetField("ways_used", n_cables)
    keep.SetField("WAYS_TOTAL", ways)
    keep.SetField("WAYS", ways)
    keep.SetField("REVIEW", 1 if over else 0)
    keep.SetField("DUCTS_MERGED", len(members))
    occ = (n_cables / ways) * 100.0 if ways else 100.0
    keep.SetField("OCCUPANCY_PCT", round(min(occ, 100.0), 1))
    keep.SetField("SPARE_PCT", round(max(0.0, 100.0 - occ), 1))
    lyr.SetFeature(keep)


def _min_line_dist(geom_a, geom_b):
    """Smallest distance between two line geometries, part by part."""
    pa, pb = _geom_parts(geom_a), _geom_parts(geom_b)
    best = float("inf")
    for a in pa:
        for b in pb:
            for i in range(len(a) - 1):
                for j in range(len(b) - 1):
                    d = min(_dist_point_seg(a[i][0], a[i][1], b[j][0], b[j][1],
                                            b[j + 1][0], b[j + 1][1]),
                            _dist_point_seg(a[i + 1][0], a[i + 1][1],
                                            b[j][0], b[j][1],
                                            b[j + 1][0], b[j + 1][1]))
                    if d < best:
                        best = d
    return best


# How close a stub must lie to the span that would inherit it before deleting
# it is free. Beyond this the stub carries geometry nothing else has (a tap
# running out to a coupler), so it is re-labelled instead of removed.
_STUB_COINCIDENT_M = 1.0


def absorb_chamber_stubs(path, feedback=None,
                         label="Feeder ducts", mode="absorb", floor=4):
    """Remove ducts that begin and end at the SAME chamber.

    A duct is pulled *between two structures*, so a component whose two ends
    snap to one chamber is not a span — it is a fragment the segmenter left at a
    junction. Measured on Berlin: **16 of the 87 feeder ducts** were these
    stubs, 2.7–14.7 m long, and they were what inflated the capacity profile
    (a 12-Way duct was being chosen for a 7 m stub, because the stub's
    ``cables_carried`` is the union of every fragment that snapped there).

    Each stub's cables and PDPs are handed to the nearest REAL span touching
    that same chamber — its cables have to continue somewhere, and that span is
    where they physically do — then the stub is deleted.    A stub with no real
    span to absorb it is kept but flagged (``REVIEW = 1``, ``SPAN_KIND =
    "Chamber stub"``) rather than silently dropped, so it shows up instead of
    disappearing.

    ``mode="flag"`` marks stubs without deleting anything.

    ``mode="coincident"`` (the DISTRIBUTION tier) deletes only the stubs whose
    geometry **lies on** the span that inherits them (``_STUB_COINCIDENT_M``),
    where deleting costs no coverage; a stub that genuinely stands off
    (a tap running out to a coupler) keeps its geometry but stops pretending to
    be a span — ``END_CHAMBER`` is cleared and ``SPAN_KIND`` becomes
    ``Duct tap``, so no published duct claims to leave a chamber and come back
    to it. ``mode="flag"`` was the old answer here and left Berlin with 90 of
    188 distribution ducts reading ``START == END``.

    The absorb mode (feeder) applies the same coverage test before deleting:
    a stub whose geometry the receiving span does **not** cover keeps its
    geometry as a ``Duct tail``. Deleting those unconditionally removed 28 real
    pieces on Berlin and broke the feeder duct chain into 7 disconnected parts
    (11 PDPs stranded), so the rule now is *delete only what the receiver
    already carries*.

    ``floor`` is the minimum profile for this tier (4-Way feeder / 2-Way
    distribution) — see ``_duct_ways_for``.
    """
    ds, lyr = _open_lyr(path)
    if lyr is None:
        return 0
    _create_fields(lyr, [
        ("STUB_ABSORBED", ogr.OFTInteger),
        # Not present on the distribution layer (the merge creates them on the
        # feeder) — without these, SetField fails with "Invalid index : -1".
        ("capacity_used", ogr.OFTReal),
        ("capacity_spare", ogr.OFTReal),
    ])
    flag_only = str(mode).lower() == "flag"
    coincident_only = str(mode).lower() == "coincident"
    i_end = lyr.GetLayerDefn().GetFieldIndex("END_CHAMBER")
    rows = []
    lyr.StartTransaction()
    for f in lyr:
        sc = str(_get(lyr, f, "START_CHAMBER") or "").strip()
        ec = str(_get(lyr, f, "END_CHAMBER") or "").strip()
        rows.append({
            "f": f,
            "sc": sc, "ec": ec,
            "stub": bool(sc) and sc == ec,
            "cables": _split_list(_get(lyr, f, "cables_carried")),
            "pdps": _split_list(_get(lyr, f, "pdp_ids")),
            "geom": f.geometry(),
        })

    real = [r for r in rows if not r["stub"] and (r["sc"] or r["ec"])]
    absorbed = 0
    kept_unabsorbed = 0
    relabelled = 0
    for r in rows:
        if not r["stub"]:
            continue
        if flag_only:
            f = r["f"]
            f.SetField("REVIEW", 1)
            f.SetField("SPAN_KIND", "Chamber stub")
            lyr.SetFeature(f)
            kept_unabsorbed += 1
            continue
        chamber = r["sc"]
        # Only spans that actually touch this chamber may take the cables on.
        candidates = [x for x in real
                      if x["sc"] == chamber or x["ec"] == chamber]
        if not candidates:
            f = r["f"]
            f.SetField("REVIEW", 1)
            f.SetField("SPAN_KIND", "Chamber stub")
            lyr.SetFeature(f)
            kept_unabsorbed += 1
            continue
        best, best_d = None, float("inf")
        for x in candidates:
            if r["geom"] is None or x["geom"] is None:
                continue
            d = _min_line_dist(r["geom"], x["geom"])
            if d < best_d:
                best_d, best = d, x
        if best is None:
            continue
        if coincident_only and best_d > _STUB_COINCIDENT_M:
            # The stub stands off the span it would be absorbed into, so it
            # carries geometry nothing else has (a tap out to a coupler).
            # Keep it, but stop it claiming to be a chamber-to-chamber span.
            sf = r["f"]
            if i_end >= 0:
                sf.SetField(i_end, "")
            sf.SetField("SPAN_KIND", "Duct tap")
            sf.SetField("REVIEW", 1)
            lyr.SetFeature(sf)
            relabelled += 1
            continue
        if not coincident_only and r["geom"] is not None and best["geom"] is not None:
            # Same conservation rule as the merge: hand the cables over only
            # when the receiving span already carries this geometry. A stub
            # the span does NOT cover is duct nothing else has (a tail running
            # out of the chamber and back, or a branch), and deleting it left
            # the Berlin feeder layer in 7 disconnected pieces. Keep it, stop
            # it claiming to be a chamber-to-chamber span, flag it for review.
            if _covered_share(r["geom"], best["geom"], _STUB_COINCIDENT_M) < _DBL_COVER_SHARE:
                sf = r["f"]
                if i_end >= 0:
                    sf.SetField(i_end, "")
                sf.SetField("SPAN_KIND", "Duct tail")
                sf.SetField("REVIEW", 1)
                lyr.SetFeature(sf)
                relabelled += 1
                continue
        # Hand the stub's cables over, then recompute the receiver's capacity:
        # one duct still leaves that chamber, it just carries these too.
        cables = list(best["cables"])
        for c in r["cables"]:
            if c not in cables:
                cables.append(c)
        pdps = list(best["pdps"])
        for pdp in r["pdps"]:
            if pdp not in pdps:
                pdps.append(pdp)
        best["cables"], best["pdps"] = cables, pdps
        ways = _duct_ways_for(len(cables), floor)
        bf = best["f"]
        bf.SetField("cables_carried", ",".join(cables))
        bf.SetField("pdp_ids", ",".join(pdps))
        bf.SetField("capacity_total", ways)
        bf.SetField("capacity_used", len(cables))
        bf.SetField("capacity_spare", max(0, ways - len(cables)))
        bf.SetField("ways_used", len(cables))
        bf.SetField("WAYS_TOTAL", ways)
        bf.SetField("WAYS", ways)
        bf.SetField("REVIEW", 1 if len(cables) > ways else 0)
        occ = (len(cables) / ways) * 100.0 if ways else 100.0
        bf.SetField("OCCUPANCY_PCT", round(min(occ, 100.0), 1))
        bf.SetField("SPARE_PCT", round(max(0.0, 100.0 - occ), 1))
        lyr.SetFeature(bf)
        sf = r["f"]
        sf.SetField("STUB_ABSORBED", 1)
        lyr.DeleteFeature(sf.GetFID())
        absorbed += 1

    lyr.CommitTransaction()
    ds = None
    if feedback and relabelled:
        feedback.pushInfo(
            f"  [enrich] {label}: {relabelled} stood-off stub(s) kept as 'Duct "
            f"tap'/'Duct tail' (END_CHAMBER cleared) — a tap is not a "
            f"chamber-to-chamber span, and its geometry is not covered by the "
            f"span that would have inherited it.")
    if feedback and (absorbed or kept_unabsorbed):
        if flag_only:
            feedback.pushInfo(
                f"  [enrich] {label}: {kept_unabsorbed} chamber stub(s) FLAGGED "
                f"(REVIEW=1, SPAN_KIND='Chamber stub') — topology left alone.")
        else:
            feedback.pushInfo(
                f"  [enrich] {label}: {absorbed} chamber stub(s) absorbed into "
                f"the adjacent span, {kept_unabsorbed} kept with REVIEW=1 (no "
                f"real span touches their chamber).")
    return absorbed


def propagate_duct_pdp_id(path, feedback=None, label="Distribution ducts"):
    """Give every duct its owning splitter, so POLYGON_ID *and* PDP_ID resolve.

    A distribution duct is defined as running *"from the pdps to the pseudo obj
    points in that particular polygon (same POLYGON_ID or PDP_ID)"*. The route
    builder already records the splitters it serves in ``pdp_ids`` (lowercase),
    but nothing copied it onto the published ``PDP_ID`` — measured 202/202 blank
    on Berlin, so the duct and the coupler that joins it to a drop duct could
    not be matched from the duct side.
    """
    ds, lyr = _open_lyr(path)
    if lyr is None:
        return 0
    _create_fields(lyr, [("PDP_ID", ogr.OFTString, 32)])
    n = 0
    lyr.StartTransaction()
    for f in lyr:
        if str(_get(lyr, f, "PDP_ID") or "").strip():
            continue
        src = _split_list(_get(lyr, f, "pdp_ids"))
        if not src:
            continue
        # The duct layers use uppercase PDP ids (PDP00007); pdp_ids is
        # lowercase, so normalise rather than publish two spellings.
        f.SetField("PDP_ID", src[0].upper())
        lyr.SetFeature(f)
        n += 1
    lyr.CommitTransaction()
    ds = None
    if feedback and n:
        feedback.pushInfo(f"  [enrich] {label}: PDP_ID resolved on {n} duct(s).")
    return n


def _geom_parts(geom):
    """Point lists of a geometry — ONE LIST PER PART.

    ``_line_points`` flattens a MultiLineString into a single sequence, which
    silently joins the end of one part to the start of the next and creates a
    segment that is not in the data. Measuring against that reported distances
    to geometry that does not exist (a coupler 147 m away from the duct it was
    matched to). Anything doing distance work must use this instead.
    """
    if geom is None or geom.IsEmpty():
        return []
    out = []
    try:
        n = geom.GetGeometryCount()
        if n:
            for i in range(n):
                part = geom.GetGeometryRef(i)
                if part is None:
                    continue
                out.append([(part.GetX(j), part.GetY(j))
                            for j in range(part.GetPointCount())])
        else:
            out.append([(geom.GetX(j), geom.GetY(j))
                        for j in range(geom.GetPointCount())])
    except Exception:
        return out
    return [p for p in out if len(p) >= 2]


def _dist_point_seg(px, py, ax, ay, bx, by):
    """Distance from (px,py) to the segment (ax,ay)-(bx,by)."""
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def link_couplers_to_ducts(coupler_path, dist_path, drop_path,
                           feedback=None, tol_m=5.0):
    """Name BOTH ducts on every coupler: the distribution and the drop side.

    The coupler is created while the drop ducts are built, before the
    distribution network exists, so it could only ever carry the drop duct's
    ``DUCT_UID`` — measured Berlin: all 291 ids a subset of
    ``Drop_Ducts.DUCT_UID``, with no distribution reference at all. Now that
    both tiers are published, each coupler is joined to the distribution duct it
    sits on (same ``POLYGON_ID`` preferred, nearest geometry otherwise) and the
    drop side is renamed explicitly.
    """
    dl, dlyr = _open_lyr(dist_path)
    if dlyr is None:
        return 0
    # One entry PER PART (a duct is published as a MultiLineString): measuring
    # across parts would invent segments that are not in the data.
    ducts = []
    for f in dlyr:
        did = str(_get(dlyr, f, "DUCT_ID") or "").strip()
        dpoly = str(_get(dlyr, f, "POLYGON_ID") or "").strip()
        for part in _geom_parts(f.geometry()):
            ducts.append({"id": did, "poly": dpoly, "pts": part})
    dl = None
    if not ducts:
        return 0

    ds, lyr = _open_lyr(coupler_path)
    if lyr is None:
        return 0
    _create_fields(lyr, [
        ("DIST_DUCT_ID", ogr.OFTString, 32),
        ("DIST_DUCT_SAME_POLY", ogr.OFTInteger),
        ("DROP_DUCT_UID", ogr.OFTInteger),
        ("PREMISE_ID", ogr.OFTString, 32),
    ])
    n = 0
    lyr.StartTransaction()
    for f in lyr:
        # OGR geometry API (not the QGIS one): IsEmpty(), and GetX()/GetY()
        # on a point — asPoint()/isEmpty() belong to QgsGeometry and raise here.
        geom = f.geometry()
        if geom is None or geom.IsEmpty() or geom.GetGeometryType() != ogr.wkbPoint:
            continue
        px, py = geom.GetX(), geom.GetY()
        drop_uid = _get(lyr, f, "DUCT_UID")
        if drop_uid is not None:
            f.SetField("DROP_DUCT_UID", int(drop_uid))
        premise = str(_get(lyr, f, "ADDR_ID") or "").strip()
        if premise:
            f.SetField("PREMISE_ID", premise)
        poly = str(_get(lyr, f, "POLYGON_ID") or "").strip().upper()
        best_id, best_d, best_same = "", float("inf"), 0
        best_score = float("inf")
        for d in ducts:
            pts = d["pts"]
            dist = min(_dist_point_seg(px, py, pts[i][0], pts[i][1],
                                       pts[i + 1][0], pts[i + 1][1])
                       for i in range(len(pts) - 1))
            # The SERVICE POLYGON IS A TIE-BREAK, NOT A FILTER. A coupler
            # usually sits at 0.00 m from several ducts at once (it is at a
            # junction), so the own-polygon duct is preferred only when the
            # geometry is equally close. Folding the preference into the value
            # that is then thresholded rejected ducts sitting exactly ON the
            # coupler, which is how 133 joints were wrongly reported as
            # unreachable (measured: all 292 are <= 0.25 m, worst 0.00 m).
            same_poly = bool(poly) and d["poly"].upper() == poly
            score = dist - (1e-6 if same_poly else 0.0)
            if score < best_score:
                best_score, best_d, best_id, best_same = score, dist, d["id"], int(same_poly)
        if best_id and best_d <= tol_m:
            f.SetField("DIST_DUCT_ID", best_id)
            # Recorded explicitly: a drop tapping a duct planned for ANOTHER
            # service polygon is normal in the street (the duct runs past both
            # sides), but it means that duct carries drops its own polygon did
            # not account for — the splitter capacity has to see it.
            f.SetField("DIST_DUCT_SAME_POLY", best_same)
            n += 1
        # Written unconditionally: the drop side and the premise belong on
        # EVERY coupler, including the ones whose distribution duct is not in
        # reach (those stay flagged by a blank DIST_DUCT_ID).
        lyr.SetFeature(f)
    lyr.CommitTransaction()
    ds = None
    if feedback and n:
        feedback.pushInfo(
            f"  [enrich] Couplers: {n} joint(s) linked to their distribution "
            f"duct (drop side kept in DROP_DUCT_UID).")
    return n


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
        lyr.StartTransaction()
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
        lyr.CommitTransaction()
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
        lyr.StartTransaction()
        for f in lyr:
            # The distribution layer carries the shared spine trunks AND the
            # one-per-premise garden-leg drop cables. A drop is a 12F garden
            # cable (docs/stages/HLD.md §Duct & cable rules), so it must not
            # be restamped with the 48F distribution catalogue profile — the
            # cable stage already sized both classes from the real household
            # counts, exactly as the feeder branch above preserves the shared
            # planner's FIBER_COUNT.
            conn = str(_get(lyr, f, "CONNECTION_TYPE") or "")
            is_drop = conn.lower().startswith("drop") or \
                str(_get(lyr, f, "CABLE_TYPE") or "").strip().lower() == "drop"
            if is_drop:
                own = _num(lyr, f, "FIBER_COUNT", 0)
                fc = int(own) if own else 12
                f.SetField("CABLE_TYPE", "Drop")
            else:
                own = _num(lyr, f, "FIBER_COUNT", 0)
                fc = int(own) if own else prof["fiber_count"]
                f.SetField("CABLE_TYPE", "Distribution")
            f.SetField("FIBER_COUNT", fc)
            f.SetField("LENGTH_M", round(_geom_len_m(f), 1))
            pid = str(_get(lyr, f, "pdp_id") or _get(lyr, f, "PDP_ID") or "").upper()
            f.SetField("SOURCE_NODE", pid)
            # Utilisation for a drop is its own fibre count (one premise); the
            # PDP-wide household total is meaningless there.
            hh = _num(lyr, f, "HH_COUNT", 0) if is_drop else hh_by_pdp.get(pid, 0)
            util = min(100.0, (hh / fc) * 100.0) if fc else 0.0
            f.SetField("UTIL_PCT", round(util, 1))
            f.SetField("INFRA_STATUS", "Proposed")
            lyr.SetFeature(f)
            total += 1
        lyr.CommitTransaction()
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
        lyr.StartTransaction()
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
        lyr.CommitTransaction()
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
        lyr.StartTransaction()
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
        lyr.CommitTransaction()
        ds = None
    if feedback:
        feedback.pushInfo(f"  [enrich] Equipment: {total} catalogue attributes applied.")
    return total


# ── Combined entry point ─────────────────────────────────────────────────────

# ── Duct continuity check ───────────────────────────────────────────────────
#
# A duct is laid along a trench, so the feeder network is one continuous chain
# from the MFG to every PDP and the distribution network has to reach the
# couplers that tap it. Both facts are cheap to test on the published layers
# and expensive to notice by eye, so every run now states them in its log —
# the dedupe passes above were silently deleting duct until this check existed.
_DUCT_SNAP_M = 0.5


def _is_empty(geom) -> bool:
    """OGR spells this ``IsEmpty``; QGIS wraps it as ``isEmpty``."""
    if geom is None:
        return True
    for attr in ("IsEmpty", "isEmpty"):
        fn = getattr(geom, attr, None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                continue
    return False


def _line_segments(path, feedback=None):
    """Every polyline segment of a GPKG layer as ((x0,y0),(x1,y1))."""
    ds, lyr = _open_lyr(path)
    if lyr is None:
        return []
    out = []
    for f in lyr:
        g = f.geometry()
        if _is_empty(g):
            continue
        for xy in _geom_parts(g):
            for i in range(len(xy) - 1):
                out.append((xy[i], xy[i + 1]))
    return out


def _point_xy(path, id_fields=("SRC_ID", "PDP_ID", "MFG_ID", "id")):
    ds, lyr = _open_lyr(path)
    if lyr is None:
        return []
    names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName()
             for i in range(lyr.GetLayerDefn().GetFieldCount())]
    idf = next((n for n in id_fields if n in names), None)
    out = []
    for f in lyr:
        g = f.geometry()
        if _is_empty(g):
            continue
        try:
            c = g.centroid().asPoint()
            x, y = c.x(), c.y()
        except Exception:
            # A point layer: read the vertex itself (``centroid`` is a QGIS
            # spelling and this layer can hand back a bare OGR point).
            try:
                x, y = g.GetX(0), g.GetY(0)
            except Exception:
                continue
        out.append(((x, y), str(f.GetField(idf)) if idf else str(f.GetFID())))
    return out


def _components(segments, snap_m=_DUCT_SNAP_M):
    """Union-find over segment endpoints snapped within ``snap_m``."""
    nodes: List[Tuple[float, float]] = []
    parent: Dict[int, int] = {}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def node_at(pt):
        for i, q in enumerate(nodes):
            if math.hypot(pt[0] - q[0], pt[1] - q[1]) <= snap_m:
                return i
        nodes.append(pt)
        parent[len(nodes) - 1] = len(nodes) - 1
        return len(nodes) - 1

    for a, b in segments:
        ia, ib = node_at(a), node_at(b)
        if ia != ib:
            union(ia, ib)
    groups: Dict[int, List[int]] = {}
    for i in range(len(nodes)):
        groups.setdefault(find(i), []).append(i)
    return nodes, groups


def verify_duct_continuity(out_dir, feedback=None):
    """Log the feeder chain and the distribution/coupler reach of a run.

    Returns a dict with the numbers (also useful to tests): ``feeder_parts``,
    ``pdp_reached``, ``pdp_total``, ``couplers_off``, ``coupler_total``.
    """
    p = lambda n: os.path.join(out_dir, n)  # noqa: E731
    report = {}
    mfg = _point_xy(p("MFG.gpkg"))
    pdps = _point_xy(p("PDPs.gpkg"))
    segs = _line_segments(p("Feeder_Ducts.gpkg"), feedback)
    if mfg and pdps and segs:
        nodes, groups = _components(segs)
        mi = min(range(len(nodes)),
                 key=lambda i: math.hypot(mfg[0][0][0] - nodes[i][0],
                                          mfg[0][0][1] - nodes[i][1]))
        parent_root = next(r for r, mem in groups.items() if mi in mem)
        reached, stranded = 0, []
        for pt, pid in pdps:
            pi = min(range(len(nodes)),
                     key=lambda i: math.hypot(pt[0] - nodes[i][0],
                                              pt[1] - nodes[i][1]))
            rr = next((r for r, mem in groups.items() if pi in mem), None)
            if rr == parent_root:
                reached += 1
            else:
                stranded.append(pid)
        report["feeder_parts"] = len(groups)
        report["pdp_reached"] = reached
        report["pdp_total"] = len(pdps)
        report["pdp_stranded"] = stranded
        if feedback:
            feedback.pushInfo(
                f"  [verify] Feeder ducts: {len(groups)} connected part(s), "
                f"MFG reaches {reached}/{len(pdps)} PDP(s).")
            if stranded:
                feedback.pushWarning(
                    "  [verify] Feeder ducts: %d PDP(s) are on a duct network the "
                    "MFG does not reach (%s) — the chain is broken, check the "
                    "chamber-to-chamber spans." % (len(stranded),
                                                   ", ".join(stranded[:8])))
    couplers = _point_xy(p("Coupleurs.gpkg"), ("DUCT_UID", "SRC_ID", "id"))
    dsegs = _line_segments(p("Distribution_Ducts.gpkg"), feedback)
    if couplers and dsegs:
        off, worst = 0, 0.0
        for pt, _cid in couplers:
            best = float("inf")
            for a, b in dsegs:
                d = _dist_point_seg(pt[0], pt[1], a[0], a[1], b[0], b[1])
                if d < best:
                    best = d
                    if best <= 1.0:
                        break
            if best > 1.0:
                off += 1
            worst = max(worst, best)
        report["couplers_off"] = off
        report["coupler_total"] = len(couplers)
        report["coupler_worst_m"] = round(worst, 2)
        if feedback:
            feedback.pushInfo(
                f"  [verify] Distribution ducts: {off}/{len(couplers)} coupler(s) "
                f"are >1 m off the duct line (worst {worst:.2f} m) — a coupler is "
                f"the joint on that duct.")
            if off:
                feedback.pushWarning(
                    "  [verify] %d coupler(s) do not sit on the distribution "
                    "duct — the joint layer and the duct layer disagree." % off)
    return report


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

    # The nearest-neighbour lookup cache survives grow-a-layer passes; the
    # enrichment is a fresh look at freshly written files.
    _NEAREST_CACHE.clear()

    n = 0
    # ── Chamber-to-chamber spans ────────────────────────────────────────
    # A trench is dug chamber to chamber, so the network is published as spans:
    # every run is broken at the chambers sitting on it and each published
    # feature is one selective sequence (START_CHAMBER -> END_CHAMBER). This
    # replaces the old chamber *splicing* pass, which kept one long corridor
    # and carried a 50-chamber chain in SECTION_CHAIN / SECTIONS_JSON as text.
    # Ducts get the same treatment right after (see below): each published
    # duct row is one duct, cut chamber to chamber.
    try:
        segment_trenches_at_chambers(
            p("Final_Trenches.gpkg"), p("Chambers.gpkg"), feedback)
    except Exception as exc:
        if feedback:
            feedback.pushInfo(f"  [segment] Trench segmentation skipped: {exc}")

    # Ducts are pulled chamber to chamber exactly like the trench: the
    # published feeder/distribution duct layers now carry ONE row per duct
    # (duct_layer publishes the bins, not a per-tier clubbed corridor), so
    # cutting them at the chambers gives the selective sequence actually
    # installed. `enrich_ducts` then stamps the catalogue attributes and the
    # endpoint chambers on every span.  Drop ducts are one-per-premise legs —
    # they stay whole.
    try:
        segment_ducts_at_chambers(
            p("Feeder_Ducts.gpkg"), p("Distribution_Ducts.gpkg"),
            p("Chambers.gpkg"), feedback)
    except Exception as exc:
        if feedback:
            feedback.pushInfo(f"  [segment] Duct segmentation skipped: {exc}")

    n += enrich_trenches(p("Final_Trenches.gpkg"), feedback, roads_lyr=roads_lyr)
    n += enrich_trench_sublayers(out_dir, feedback)
    n += enrich_ducts(
        p("Feeder_Ducts.gpkg"), p("Distribution_Ducts.gpkg"), p("Drop_Ducts.gpkg"),
        p("Final_Trenches.gpkg"), p("Chambers.gpkg"), feedback,
    )

    # ── Duct tier rules, applied AFTER the catalogue attributes exist ────
    # ONE feeder duct per chamber pair (the trunk cables ride inside it) —
    # the operator rule is "one chamber to another only one feeder duct will
    # be present". Distribution ducts are deliberately left as published:
    # several per corridor is correct there (splitter capacity / profile).
    n += merge_ducts_per_chamber_span(p("Feeder_Ducts.gpkg"), feedback)
    # A component that starts and ends at one chamber is not a span — absorb it
    # into the real span leaving that chamber. Both tiers get this: the operator
    # spec cuts distribution ducts at chambers too (it only allows SEVERAL of
    # them per corridor, which is why duplicates are folded for feeder only).
    n += absorb_chamber_stubs(p("Feeder_Ducts.gpkg"), feedback, "Feeder ducts")
    # Distribution keeps its topology (several ducts per corridor is allowed and
    # the couplers tap it) — but it stops publishing fragments as spans. A stub
    # whose geometry LIES ON the span that would inherit it is duplication and
    # goes; one that stands off keeps its geometry as a 'Duct tap'.
    n += absorb_chamber_stubs(p("Distribution_Ducts.gpkg"), feedback,
                              "Distribution ducts", mode="coincident", floor=2)
    n += propagate_duct_pdp_id(p("Distribution_Ducts.gpkg"), feedback)
    # Every run states whether the ducts actually flow: one feeder chain from
    # the MFG to every PDP, and couplers sitting on the distribution duct.
    verify_duct_continuity(out_dir, feedback)
    # The coupler is the joint between the distribution and the drop duct, so
    # it has to name both — it could only carry the drop side when it was
    # created, before the distribution network existed.
    n += link_couplers_to_ducts(
        p("Coupleurs.gpkg"), p("Distribution_Ducts.gpkg"),
        p("Drop_Ducts.gpkg"), feedback,
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
