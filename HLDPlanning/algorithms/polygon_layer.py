# -*- coding: utf-8 -*-
import heapq
import math

from collections import Counter, defaultdict
from statistics import median

from qgis.PyQt.QtCore import QCoreApplication, QMetaType
from qgis.core import (
    QgsFeature, QgsFields, QgsField, QgsWkbTypes, QgsGeometry, QgsGeometryCollection,
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource, QgsProcessingParameterField, QgsProcessingParameterEnum,
    QgsProcessingParameterNumber, QgsProcessingParameterFeatureSink, QgsProcessingParameterBoolean,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterDistance,
    QgsFeatureSink, QgsFeatureRequest, QgsSpatialIndex, QgsVectorLayer, QgsPointXY, QgsProcessingException,
    QgsProcessingParameterString, QgsProcessingParameterMultipleLayers, QgsRectangle, QgsCoordinateTransform
)
from qgis import processing
from ..utils.fields import COMMON_FIELDS


def _normalize_method_value(val, options, alt_options=None):
    """
    Convert METHOD value (int or string) to enum index.
    `options` is your displayed Enum options (METHOD_LABELS).
    `alt_options` can be canonical machine keys, if you have them.
    """
    # already an index?
    if isinstance(val, int):
        return val

    if isinstance(val, str):
        v = val.strip().lower()

        # 1) try alt_options (e.g. ['convex_hull','concave_hull',...])
        if alt_options:
            for i, opt in enumerate(alt_options):
                if v == opt.strip().lower():
                    return i

        # 2) try labels (e.g. ['Convex hull','Concave hull',...])
        for i, opt in enumerate(options):
            if v == opt.strip().lower():
                return i

    raise QgsProcessingException(f"Invalid METHOD value: {val}")


METHOD_LABELS = [
    "Convex Hull (optional inset)",
    "Concave Hull (alpha shape)",
    "Voronoi Partition → Dissolve by group",
    "Seeded Growth (splitter-driven builder)",
]
M_CONVEX, M_CONCAVE, M_VORONOI, M_GROWTH = range(4)

# Canonical machine keys in the SAME order and length as METHOD_LABELS
METHOD_KEYS = ["convex_hull", "concave_hull", "voronoi", "seeded_growth"]

# Half-width of the corridor cut out of built polygons along barriers [m].
GROWTH_ROAD_HALF_WIDTH = 4.0

# Protective radius around premises [m]: a polygon may never claim ground closer
# than this to ANOTHER cluster's premise, and its OWN members keep at least this
# much geometry around them through the barrier-corridor cut.
GROWTH_MEMBER_PROTECT = 10.0

# Defragmentation overflow: a capacity-locked stray building (surrounded by a
# full neighbour) may be absorbed into that neighbour up to this multiple of the
# max-homes cap, to avoid leaving it as a lone satellite square. The absorbing
# polygon is flagged REVIEW=1. (User choice: presentable > strict cap by a hair.)
GROWTH_OVERFLOW = 1.10

# Splitter sizing (computed OUTPUT, not a growth constraint): the full mix of
# splitters used per polygon/PDP lives in utils/splitters.py (shared with stage 03).
from ..utils.splitters import SPLITTER_SIZES, plan_splitters, recommend_splitter


SEED_LABELS = ["All points", "Group representative (core centroid)"]
SEED_ALL, SEED_REP = 0, 1

ALPHA_MODE_LABELS = ["Manual", "Auto (k × median NN dist)"]
ALPHA_MANUAL, ALPHA_AUTO = 0, 1

XY_NAMES = {"x","X","y","Y","lon","Lng","lng","Lon","lat","Lat"}


class PolygonLayerAlgorithm(QgsProcessingAlgorithm):
    # Parameters — slimmed 2026-07-03: stage 02 consumes ONLY the premises layer
    # (PDP/MFG/cluster assignment happens in stage 03), so the GROUP field and the
    # Objects-enrichment inputs were removed. Legacy hull methods now run with
    # fixed internal defaults (single group, auto alpha, seedlock rebuild on).
    P_INPUT = "INPUT"; P_METHOD = "METHOD"; P_BUFFER = "BUFFER"
    P_SEEDBUF = "SEEDBUF"; P_CLIP = "CLIP"; P_THIN = "THIN_EXPORT"; P_OUT = "OUT"
    # Seeded-growth builder
    P_PLAN_FIRST = "PLANNING_FIRST"
    P_MIN_HH = "MIN_HH_PER_POLYGON"; P_MAX_HH = "MAX_HH_PER_POLYGON"
    P_NEIGH_DIST = "NEIGHBOR_DIST"; P_SERVICE_RADIUS = "SERVICE_RADIUS"
    P_ACCESS_DIST = "ROAD_ACCESS_DIST"
    P_BAR_ROADS = "BARRIER_ROADS"; P_BAR_FIELD = "BARRIER_CLASS_FIELD"
    P_BAR_CLASSES = "BARRIER_MAIN_CLASSES"; P_BAR_EXTRA = "BARRIER_EXTRA"

    def tr(self, s):
        return QCoreApplication.translate("PolygonLayerAlgorithm", s)

    def name(self):
        return "02_polygon_layer"

    def displayName(self):
        return self.tr("Generate Polygons")

    def group(self):
        return self.tr("02 Polygon Layer")

    def groupId(self):
        return "02_polygon_layer"

    def shortHelpString(self):
        return self.tr(
            "Generate FDP/PDP serving-area polygons directly from the premises (Object) layer.\n"
            "Default method: Seeded Growth — a constrained agglomerative (Ward-linkage) clusterer groups "
            "nearby buildings into compact service clusters under the rules: "
            "neighbour distance (150 m), 32–128 homes per polygon (nearest rule-compliant pair merged first), "
            "barriers (restricted roads / railway / river / airport zones), 300 m service radius, "
            "road-access validation, no overlaps.\n"
            "Per polygon: exact homes/building counts, coverage area, demand density, and a recommended "
            "splitter (1:8…1:64) targeting 60–90 % utilization.\n"
            "Premises need only geometry + an HH (households) column; PDP/MFG IDs are assigned later in stage 03.\n"
            "Legacy Convex/Concave/Voronoi envelopes remain selectable and run with fixed defaults."
        )

    def createInstance(self):
        return PolygonLayerAlgorithm()

    # ------------- UI definition -------------
    def initAlgorithm(self, _=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.P_INPUT,
            self.tr("Premises / Object layer (points; HH column auto-detected)"),
            [QgsProcessing.TypeVectorAnyGeometry]
        ))

        self.addParameter(QgsProcessingParameterEnum(
            self.P_METHOD,
            self.tr("Generation method"),
            options=METHOD_LABELS,
            defaultValue=M_GROWTH
        ))

        self.addParameter(QgsProcessingParameterDistance(
            self.P_BUFFER,
            self.tr("Post-buffer (+ grow / - shrink) [m]"),
            parentParameterName=self.P_INPUT,
            defaultValue=0.0
        ))

        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_CLIP,
            self.tr("Optional clip layer, e.g. AOI [polygons]"),
            [QgsProcessing.TypeVectorPolygon],
            optional=True
        ))

        self.addParameter(QgsProcessingParameterBoolean(
            self.P_THIN,
            self.tr("Thin output profile (keep only project-critical fields)"),
            defaultValue=False
        ))

        # ---- Seeded-growth (splitter-driven) builder parameters ----
        self.addParameter(QgsProcessingParameterBoolean(
            self.P_PLAN_FIRST,
            self.tr("Planning-first (force the seeded-growth builder regardless of method)"),
            defaultValue=False,
            optional=True
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.P_MIN_HH,
            self.tr("Growth: minimum homes per polygon"),
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=32, minValue=1
        ))

        self.addParameter(QgsProcessingParameterNumber(
            self.P_MAX_HH,
            self.tr("Growth: maximum homes per polygon"),
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=128, minValue=1
        ))

        self.addParameter(QgsProcessingParameterDistance(
            self.P_NEIGH_DIST,
            self.tr("Growth: neighbour distance rule [m]"),
            parentParameterName=self.P_INPUT,
            defaultValue=150.0, minValue=1.0
        ))

        self.addParameter(QgsProcessingParameterDistance(
            self.P_SERVICE_RADIUS,
            self.tr("Growth: service radius — max building distance from FDP [m]"),
            parentParameterName=self.P_INPUT,
            defaultValue=300.0, minValue=10.0
        ))

        self.addParameter(QgsProcessingParameterDistance(
            self.P_ACCESS_DIST,
            self.tr("Growth: road-access check distance [m] (0 = check off)"),
            parentParameterName=self.P_INPUT,
            defaultValue=100.0, minValue=0.0
        ))

        self.addParameter(QgsProcessingParameterDistance(
            self.P_SEEDBUF,
            self.tr("Growth: extra edge margin around built polygons [m]"),
            parentParameterName=self.P_INPUT,
            defaultValue=0.0,
            optional=True
        ))

        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BAR_ROADS,
            self.tr("Growth: road layer for the barrier rule [lines]"),
            [QgsProcessing.TypeVectorLine],
            optional=True
        ))

        self.addParameter(QgsProcessingParameterField(
            self.P_BAR_FIELD,
            self.tr("Growth: road class field (default: fclass/highway)"),
            parentLayerParameterName=self.P_BAR_ROADS,
            type=QgsProcessingParameterField.Any,
            optional=True
        ))

        self.addParameter(QgsProcessingParameterString(
            self.P_BAR_CLASSES,
            self.tr("Growth: restricted road classes (comma-separated; *_link matched too)"),
            defaultValue="motorway,trunk,primary,secondary",
            optional=True
        ))

        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.P_BAR_EXTRA,
            self.tr("Growth: extra barrier layers (railway / river / airport zone) [optional]"),
            QgsProcessing.TypeVectorAnyGeometry,
            optional=True
        ))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.P_OUT,
            self.tr("Output polygons"),
            QgsProcessing.TypeVectorPolygon
        ))

    # ------------- helpers -------------

    def _fields_out(self, src_fields: QgsFields, include_growth: bool = False) -> QgsFields:
        out = QgsFields()
        for f in src_fields:
            if f.name().lower() == "fid":
                continue  # GPKG reserves 'fid' as the FID column; copying it collides on write
            out.append(f)
        extra = [
            ("stg", QMetaType.Type.QString),
            ("area_m2", QMetaType.Type.Double),
            ("POLYGON_ID", QMetaType.Type.QString),
            ("SUM_OBJECT", QMetaType.Type.Int),
            ("SUM_HOMES", QMetaType.Type.Int),
            ("pDp_POL_ID", QMetaType.Type.QString),
            ("OLT_POL_ID", QMetaType.Type.QString),
            ("CLUSTER_ID", QMetaType.Type.QString),
            ("HH", QMetaType.Type.Int),
            ("MFG", QMetaType.Type.QString),
            (COMMON_FIELDS.SRC_ID, QMetaType.Type.QString),
            (COMMON_FIELDS.STAGE, QMetaType.Type.QString),
        ]
        if include_growth:
            extra += [
                ("CENTR_X", QMetaType.Type.Double),
                ("CENTR_Y", QMetaType.Type.Double),
                ("DENSITY", QMetaType.Type.Double),      # homes per hectare
                ("SPLIT_SIZE", QMetaType.Type.QString),   # primary (largest) splitter used, e.g. "1:64"
                ("SPLIT_CNT", QMetaType.Type.Int),        # total number of splitters
                ("SPLIT_UTIL", QMetaType.Type.Double),    # overall utilization %
                ("SPLIT_OK", QMetaType.Type.Int),         # 1 = 60–90 % utilization
                ("SPL_PLAN", QMetaType.Type.QString),      # full mix, e.g. "2x1:64 + 1x1:16"
                ("SPL_PORTS", QMetaType.Type.Int),        # total output ports across all splitters
                ("SPL_4", QMetaType.Type.Int),            # count of 1:4 splitters (retired from catalogue → always 0; kept for schema stability)
                ("SPL_8", QMetaType.Type.Int),            # count of 1:8 splitters
                ("SPL_16", QMetaType.Type.Int),           # count of 1:16 splitters
                ("SPL_32", QMetaType.Type.Int),           # count of 1:32 splitters
                ("SPL_64", QMetaType.Type.Int),           # count of 1:64 splitters
                ("NO_ACCESS", QMetaType.Type.Int),       # members failing the road-access check
                ("REVIEW", QMetaType.Type.Int),
            ]
        for name, qtype in extra:
            if out.indexOf(name) == -1:
                out.append(QgsField(name, qtype))
        return out

    def _thin_fields_out(self, full_fields: QgsFields, group_name: str = None) -> QgsFields:
        """Return a reduced output schema for downstream-safe polygon export."""
        keep = {
            "POLYGON_ID", "area_m2", "SUM_OBJECT", "SUM_HOMES", "pDp_POL_ID", "OLT_POL_ID",
            "CLUSTER_ID", "HH", "MFG", COMMON_FIELDS.SRC_ID, COMMON_FIELDS.STAGE,
            # growth-builder fields (present only when that method ran)
            "CENTR_X", "CENTR_Y", "DENSITY", "SPLIT_SIZE", "SPLIT_CNT", "SPLIT_UTIL",
            "SPLIT_OK", "SPL_PLAN", "SPL_PORTS", "SPL_4", "SPL_8", "SPL_16", "SPL_32",
            "SPL_64", "NO_ACCESS", "REVIEW",
        }
        if group_name:
            keep.add(group_name)

        out = QgsFields()
        for f in full_fields:
            if f.name() in keep:
                out.append(QgsField(f.name(), f.type()))
        return out

    def _majority(self, seq):
        if not seq:
            return None
        c = Counter(seq)
        most_common = c.most_common()
        if len(most_common) == 1:
            return most_common[0][0]
        if most_common[0][1] == most_common[1][1]:
            return None
        return most_common[0][0]

    def _collect_points_layer(self, geoms, crs, gid):
        mem = QgsVectorLayer(f"Point?crs={crs.authid()}", f"pts_{gid}", "memory")
        pr = mem.dataProvider()
        pr.addAttributes([QgsField("gid", QMetaType.Type.QString)])
        mem.updateFields()
        feats = []
        for g in geoms:
            if g is None or g.isEmpty():
                continue
            for pt in g.vertices():
                f = QgsFeature(mem.fields())
                f["gid"] = str(gid)
                f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(pt)))
                feats.append(f)
        if feats:
            pr.addFeatures(feats)
            mem.updateExtents()
        return mem

    def _median_nn(self, pts_layer, k=12):
        if pts_layer is None or not isinstance(pts_layer, QgsVectorLayer):
            return 1.0
        idx = QgsSpatialIndex(pts_layer.getFeatures())
        dists = []
        for f in pts_layer.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            p = g.asPoint() if g.isMultipart() is False else g.asMultiPoint()[0]
            nearest = idx.nearestNeighbor(p, max(2, k + 1))
            if len(nearest) <= 1:
                continue
            this_id = f.id()
            for nid in nearest:
                if nid == this_id:
                    continue
                nf = pts_layer.getFeature(nid)
                ng = nf.geometry()
                if ng is None or ng.isEmpty():
                    continue
                d = g.distance(ng)
                if d > 0:
                    dists.append(d)
        return median(dists) if dists else 1.0

    def _concave_alpha_layer(self, pts_layer, alpha, keep_holes=False):
        if pts_layer is None:
            return None
        # qgis:concavehull ALPHA is a 0–1 ratio (1 == convex hull). Historic
        # callers pass metre-scale values, so clamp: anything outside (0,1] falls
        # back to a moderate 0.3. Never raise — return None to trigger a fallback.
        try:
            a = alpha if (0.0 < alpha <= 1.0) else 0.3
            a = min(1.0, max(0.01, a))
            out = processing.run("qgis:concavehull", {
                "INPUT": pts_layer,
                "ALPHA": float(a),
                "HOLES": bool(keep_holes),
                "NO_MULTIGEOMETRY": False,
                "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
            })["OUTPUT"]
            return out
        except Exception:
            return None

    def _collect_union_geom(self, layer):
        u = None
        for f in layer.getFeatures():
            g = f.geometry()
            if not g or g.isEmpty():
                continue
            u = g if u is None else u.combine(g)
        return u if u is not None else QgsGeometry()

    def _seedlock_planarize_and_label(self, raw_by_gid, cores_by_gid, crs):
        """
        Planarize raw polygons, then assign each cell to gid by overlap with cores/union.
        """
        raw = QgsVectorLayer(f"Polygon?crs={crs.authid()}", "raw_seedlock", "memory")
        raw_dp = raw.dataProvider()
        raw_dp.addAttributes([QgsField("gid", QMetaType.Type.QString)])
        raw.updateFields()

        feats = []
        for gid, g in raw_by_gid.items():
            if g is None or g.isEmpty():
                continue
            f = QgsFeature(raw.fields())
            f["gid"] = str(gid)
            f.setGeometry(QgsGeometry(g))
            feats.append(f)

        if feats:
            raw_dp.addFeatures(feats)
            raw.updateExtents()
        else:
            return raw

        lines = processing.run("native:polygonstolines", {"INPUT": raw, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT})["OUTPUT"]
        dis   = processing.run("native:dissolve", {"INPUT": lines, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT})["OUTPUT"]
        cells = processing.run("native:polygonize", {"INPUT": dis, "KEEP_FIELDS": False, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT})["OUTPUT"]

        labeled = QgsVectorLayer(f"Polygon?crs={crs.authid()}", "labeled_cells", "memory")
        prl = labeled.dataProvider()
        prl.addAttributes([QgsField("gid", QMetaType.Type.QString)])
        labeled.updateFields()

        raw_union_by_gid = {gid: g for gid, g in raw_by_gid.items() if g and not g.isEmpty()}

        for cell in cells.getFeatures():
            cg = cell.geometry()
            if cg is None or cg.isEmpty():
                continue

            best_gid, best_area = None, 0.0
            for gid, core in cores_by_gid.items():
                if core is None or core.isEmpty():
                    continue
                a = cg.intersection(core).area()
                if a > best_area:
                    best_area, best_gid = a, gid

            if best_gid is None:
                for gid, rg in raw_union_by_gid.items():
                    a = cg.intersection(rg).area()
                    if a > best_area:
                        best_area, best_gid = a, gid

            if best_gid is None:
                continue

            f = QgsFeature(labeled.fields())
            f["gid"] = str(best_gid)
            f.setGeometry(cg)
            prl.addFeature(f)

        labeled.updateExtents()
        return labeled

    def _group_aggregates(self, src, group_name):
        """
        Aggregate ALL INPUT fields per group (numeric=sum, text=majority, X/Y=mean).
        """
        fields = src.fields()

        is_numeric = {}
        for f in fields:
            is_numeric[f.name()] = f.type() in (
                QMetaType.Type.Int, QMetaType.Type.LongLong,
                QMetaType.Type.UInt, QMetaType.Type.ULongLong,
                QMetaType.Type.Double
            )

        sums = defaultdict(lambda: defaultdict(float))
        counts = defaultdict(lambda: defaultdict(float))
        bag = defaultdict(lambda: defaultdict(list))

        for feat in src.getFeatures():
            gid = feat[group_name] if group_name else "ALL"

            for f in fields:
                name = f.name()
                v = feat[name]
                if v is None:
                    continue

                if name in XY_NAMES and is_numeric[name]:
                    try:
                        sums[gid][name] += float(v)
                        counts[gid][name] += 1
                    except Exception:
                        pass
                elif is_numeric[name]:
                    try:
                        sums[gid][name] += float(v)
                    except Exception:
                        pass
                else:
                    bag[gid][name].append(v)

        out = {}

        for gid in set(list(sums.keys()) + list(bag.keys())):
            row = {}
            for name, total in sums[gid].items():
                if name in XY_NAMES:
                    n = counts[gid].get(name, 1) or 1
                    val = total / float(n)
                else:
                    val = total
                if name not in XY_NAMES and isinstance(val, float) and val.is_integer():
                    row[name] = int(round(val))
                else:
                    row[name] = val
            for name, lst in bag[gid].items():
                row[name] = self._majority(lst)
            out[gid] = row

        return out

    def _geoms_of_group(self, src, group_name, gid):
        geoms = []
        for feat in src.getFeatures():
            if group_name:
                if feat[group_name] != gid:
                    continue
            g = feat.geometry()
            if g is None or g.isEmpty():
                continue
            geoms.append(g)
        return geoms

    def _enrich_for_geom(self, geom, obj_src, obj_idx,
                         hh_field, pdp_field, mfg_field, cluster_field):
        """
        Aggregate object-layer info inside a polygon geometry.
        Returns SUM_OBJECT, SUM_HOMES, pDp_POL_ID, OLT_POL_ID, CLUSTER_ID.
        """
        result = {
            "SUM_OBJECT": 0,
            "SUM_HOMES": 0,
            "pDp_POL_ID": None,
            "OLT_POL_ID": None,
            "CLUSTER_ID": None,
        }

        if geom is None or geom.isEmpty() or obj_src is None or obj_idx is None:
            return result

        ids = obj_idx.intersects(geom.boundingBox())
        pdp_vals = []
        mfg_vals = []
        cluster_vals = []

        for fid in ids:
            # QgsProcessingFeatureSource has no getFeature(); fetch by request
            of = next(obj_src.getFeatures(QgsFeatureRequest(fid)), None)
            if of is None:
                continue
            og = of.geometry()
            if og is None or og.isEmpty():
                continue

            if not geom.intersects(og):
                continue

            result["SUM_OBJECT"] += 1

            if hh_field and hh_field in of.fields().names():
                v = of[hh_field]
                try:
                    if v is not None:
                        result["SUM_HOMES"] += int(v)
                except Exception:
                    pass

            if pdp_field and pdp_field in of.fields().names():
                val = of[pdp_field]
                if val not in (None, ""):
                    pdp_vals.append(val)

            if mfg_field and mfg_field in of.fields().names():
                val = of[mfg_field]
                if val not in (None, ""):
                    mfg_vals.append(val)

            if cluster_field and cluster_field in of.fields().names():
                val = of[cluster_field]
                if val not in (None, ""):
                    cluster_vals.append(val)

        if pdp_vals:
            result["pDp_POL_ID"] = self._majority(pdp_vals)
        if mfg_vals:
            result["OLT_POL_ID"] = self._majority(mfg_vals)
        if cluster_vals:
            result["CLUSTER_ID"] = self._majority(cluster_vals)

        return result

    # ------------- main algorithm -------------

    def processAlgorithm(self, p, context, feedback):
        src = self.parameterAsSource(p, self.P_INPUT, context)
        if not src or src.featureCount() == 0:
            raise QgsProcessingException("INPUT layer is empty or invalid.")

        # Grouping removed from the UI: premises carry no pre-assigned PDP grouping
        # (that happens in stage 03), so legacy methods run on a single "ALL" group
        # and the growth builder derives clusters itself.
        group_name = None

        method_raw = self.parameterAsInt(p, self.P_METHOD, context)
        method = _normalize_method_value(method_raw, METHOD_LABELS, METHOD_KEYS)

        if self.parameterAsBoolean(p, self.P_PLAN_FIRST, context) and method != M_GROWTH:
            feedback.pushInfo(self.tr(
                "PLANNING_FIRST is set — overriding METHOD with the seeded-growth (splitter-driven) builder."
            ))
            method = M_GROWTH

        buf_m = float(self.parameterAsDouble(p, self.P_BUFFER, context) or 0.0)

        # Fixed internal defaults for the legacy hull methods (parameters removed
        # from the UI 2026-07-03; values match the old parameter defaults).
        inset_m = 0.0
        alpha_mode = ALPHA_AUTO
        alpha_manual = 40.0
        alpha_k = 12
        keep_holes = False
        vor_ext = 50.0
        seed_strat = SEED_REP
        ensure_cover = True
        rebuild = True

        # Enrichment removed: PDP/MFG/cluster IDs do not exist before stage 03.
        enrich = False
        obj_src = None
        hh_field = pdp_field = mfg_field = cluster_field = None

        clip_lyr = self.parameterAsVectorLayer(p, self.P_CLIP, context)

        thin_export = self.parameterAsBoolean(p, self.P_THIN, context)
        # All methods carry the full attribute schema (incl. splitter fields) so
        # legacy hull polygons get the same, spatially-correct metrics as growth.
        fields_full = self._fields_out(src.fields(), include_growth=True)
        fields_out = self._thin_fields_out(fields_full, group_name) if thin_export else fields_full

        (sink, dest_id) = self.parameterAsSink(
            p, self.P_OUT, context,
            fields_out,
            QgsWkbTypes.MultiPolygon,
            src.sourceCrs()
        )

        if method == M_GROWTH:
            self._process_growth(p, context, feedback, src, sink, fields_out, buf_m, clip_lyr)
            return {self.P_OUT: dest_id}

        # ---- Legacy hull methods (Convex / Concave / Voronoi) ----
        # Build a spatial index of premises so each hull polygon gets correct
        # SUM_OBJECT (premises inside), HH (their homes) and a splitter plan —
        # not just a whole-group aggregate. NOTE: these methods draw geometric
        # envelopes and do NOT enforce the 32–128 home cap or split into service
        # areas; use Seeded Growth (default) for rule-compliant, capacity-sized
        # polygons. Attributes here are descriptive of whatever the hull covers.
        _legacy_hh_name = self._detect_hh_field(src.fields())
        _legacy_pt_index = QgsSpatialIndex()
        _legacy_pt_geoms = {}
        _legacy_pt_hh = {}
        for _lf in src.getFeatures():
            _lg = _lf.geometry()
            if _lg is None or _lg.isEmpty():
                continue
            _legacy_pt_geoms[_lf.id()] = QgsGeometry(_lg)
            _hh = 0
            if _legacy_hh_name:
                try:
                    _v = _lf[_legacy_hh_name]
                    _hh = int(float(_v)) if _v not in (None, "") else 0
                except Exception:
                    _hh = 0
            _legacy_pt_hh[_lf.id()] = _hh
            _lft = QgsFeature(_lf.id())
            _lft.setGeometry(_lg)
            _legacy_pt_index.addFeature(_lft)

        group_ids = set()
        if group_name:
            for feat in src.getFeatures():
                group_ids.add(feat[group_name])
        else:
            group_ids = {"ALL"}

        src_aggs = self._group_aggregates(src, group_name)

        cores_by_gid = {}
        raw_by_gid = {}

        for gid in group_ids:
            geoms = self._geoms_of_group(src, group_name, gid)
            if not geoms:
                cores_by_gid[str(gid)] = None
                continue
            merged = geoms[0]
            for g in geoms[1:]:
                merged = merged.combine(g)
            cores_by_gid[str(gid)] = merged

        obj_idx = None
        if enrich and obj_src:
            obj_idx = QgsSpatialIndex(obj_src.getFeatures())

        def write_attr_bundle(feat, geom, gid_str):
            row = src_aggs.get(gid_str, {})
            for name, val in row.items():
                if name in feat.fields().names():
                    feat[name] = val

            if not (enrich and obj_src and obj_idx):
                return

            e = self._enrich_for_geom(geom, obj_src, obj_idx,
                                      hh_field, pdp_field, mfg_field, cluster_field)
            names = feat.fields().names()

            for k in ("SUM_OBJECT", "SUM_HOMES", "pDp_POL_ID", "OLT_POL_ID", "CLUSTER_ID"):
                if k in names and e.get(k) is not None:
                    feat[k] = e[k]

            if "HH" in names and e.get("SUM_HOMES") is not None:
                feat["HH"] = e["SUM_HOMES"]
            if "MFG" in names and e.get("OLT_POL_ID") is not None:
                feat["MFG"] = e["OLT_POL_ID"]

        total = len(group_ids)
        polygon_serial = 1

        for i, gid in enumerate(group_ids):
            if feedback.isCanceled():
                break

            try:
                if total > 0:
                    feedback.setProgress(int(100 * i / total))
            except Exception:
                pass

            gid_str = str(gid)
            geoms = self._geoms_of_group(src, group_name, gid)
            if not geoms:
                continue

            core = cores_by_gid.get(gid_str)
            raw_poly = None

            if method == M_CONVEX:
                merged = geoms[0]
                for g in geoms[1:]:
                    merged = merged.combine(g)
                hull = merged.convexHull()
                if inset_m < 0:
                    try:
                        hull = hull.buffer(inset_m, 8)
                    except Exception:
                        pass
                if abs(buf_m) > 0.0:
                    hull = hull.buffer(buf_m, 8)
                raw_poly = hull

            elif method == M_CONCAVE:
                # Robust: qgis:concavehull can fail (its ALPHA is a 0–1 ratio);
                # on any failure fall back to the convex hull so the run never aborts.
                poly = None
                try:
                    pts = self._collect_points_layer(geoms, src.sourceCrs(), gid)
                    if alpha_mode == ALPHA_AUTO:
                        base = self._median_nn(pts, k=alpha_k or 12)
                        alpha = base * (alpha_k or 1.2)
                    else:
                        alpha = alpha_manual or 40.0
                    hull_lyr = self._concave_alpha_layer(pts, alpha, keep_holes)
                    poly = self._collect_union_geom(hull_lyr) if hull_lyr is not None else None
                except Exception as exc:
                    feedback.pushWarning(f"Concave hull failed ({exc}); using convex hull.")
                    poly = None
                if poly is None or poly.isEmpty():
                    merged = geoms[0]
                    for g in geoms[1:]:
                        merged = merged.combine(g)
                    poly = merged.convexHull()
                if abs(buf_m) > 0.0:
                    poly = poly.buffer(buf_m, 8)
                raw_poly = poly

            elif method == M_VORONOI:
                # Robust: a single-group Voronoi has one seed (or the EXTENT param
                # is rejected) and errors out. Compute the Voronoi partition of ALL
                # premises and union it; on any failure fall back to convex hull.
                # Uses Shapely to avoid QGIS 3.44 SIGSEGV in qgis:voronoipolygons.
                poly = None
                try:
                    from shapely import voronoi_polygons
                    from shapely.geometry import MultiPoint
                    from shapely.ops import unary_union

                    all_coords = []
                    for _g in geoms:
                        if _g is None or _g.isEmpty():
                            continue
                        for pt in _g.vertices():
                            all_coords.append((pt.x(), pt.y()))

                    if len(all_coords) >= 3:
                        mp = MultiPoint(all_coords)
                        vor = voronoi_polygons(mp)
                        cells = [g for g in vor.geoms if g is not None and not g.is_empty]
                        if cells:
                            diss = unary_union(cells)
                            poly = QgsGeometry.fromWkt(diss.wkt)
                except Exception as exc:
                    feedback.pushWarning(f"Voronoi failed ({exc}); using convex hull.")
                    poly = None
                if poly is None or poly.isEmpty():
                    merged = geoms[0]
                    for g in geoms[1:]:
                        merged = merged.combine(g)
                    poly = merged.convexHull()
                if abs(buf_m) > 0.0:
                    poly = poly.buffer(buf_m, 8)
                raw_poly = poly

            if ensure_cover and core and not core.isEmpty():
                raw_poly = raw_poly.combine(core)

            if clip_lyr is not None:
                try:
                    clip_union = self._collect_union_geom(clip_lyr)
                    raw_poly = raw_poly.intersection(clip_union)
                except Exception:
                    pass

            raw_poly = self._safe_polygon(raw_poly)

            if rebuild:
                raw_by_gid[gid_str] = raw_poly
            else:
                feat = QgsFeature(fields_out)
                feat.setGeometry(raw_poly)
                if group_name and fields_out.indexOf(group_name) != -1:
                    feat[group_name] = gid_str
                if "stg" in fields_out.names():
                    feat["stg"] = "stage1_direct"
                if COMMON_FIELDS.STAGE in fields_out.names():
                    feat[COMMON_FIELDS.STAGE] = "polygon"
                if COMMON_FIELDS.SRC_ID in fields_out.names():
                    feat[COMMON_FIELDS.SRC_ID] = str(gid_str)
                if "area_m2" in fields_out.names():
                    feat["area_m2"] = raw_poly.area()
                write_attr_bundle(feat, feat.geometry(), gid_str)

                # Always assign a guaranteed-unique polygon id for downstream sync.
                if "POLYGON_ID" in fields_out.names():
                    poly_id = f"POLY{polygon_serial:05d}"
                    feat["POLYGON_ID"] = poly_id
                    if COMMON_FIELDS.SRC_ID in fields_out.names():
                        feat[COMMON_FIELDS.SRC_ID] = poly_id
                polygon_serial += 1

                self._write_polygon_metrics(
                    feat, feat.geometry(), fields_out.names(),
                    _legacy_pt_index, _legacy_pt_geoms, _legacy_pt_hh)

                sink.addFeature(feat, QgsFeatureSink.FastInsert)

        if rebuild:
            final = self._seedlock_planarize_and_label(raw_by_gid, cores_by_gid, src.sourceCrs())
            names = fields_out.names()
            for f in final.getFeatures():
                out = QgsFeature(fields_out)
                out.setGeometry(f.geometry())
                gid_str = f["gid"] if "gid" in f.fields().names() else None

                if group_name and group_name in names and gid_str is not None:
                    out[group_name] = gid_str

                if "stg" in names:
                    out["stg"] = "stage1_seedlock"
                if COMMON_FIELDS.STAGE in names:
                    out[COMMON_FIELDS.STAGE] = "polygon"
                if COMMON_FIELDS.SRC_ID in names:
                    out[COMMON_FIELDS.SRC_ID] = str(gid_str) if gid_str is not None else ""

                if "area_m2" in names:
                    out["area_m2"] = f.geometry().area()

                write_attr_bundle(out, out.geometry(), gid_str)

                # Always assign a guaranteed-unique polygon id for downstream sync.
                if "POLYGON_ID" in names:
                    poly_id = f"POLY{polygon_serial:05d}"
                    out["POLYGON_ID"] = poly_id
                    if COMMON_FIELDS.SRC_ID in names:
                        out[COMMON_FIELDS.SRC_ID] = poly_id
                polygon_serial += 1

                self._write_polygon_metrics(
                    out, out.geometry(), names,
                    _legacy_pt_index, _legacy_pt_geoms, _legacy_pt_hh)

                sink.addFeature(out, QgsFeatureSink.FastInsert)

        return {self.P_OUT: dest_id}

    def _write_polygon_metrics(self, feat, geom, names, pt_index, pt_geoms, pt_hh):
        """
        Spatially count premises inside `geom` and write correct SUM_OBJECT / HH /
        SUM_HOMES / area / density / splitter fields. Used by the legacy hull
        methods so their polygons carry the same attributes as the growth builder.
        """
        n_obj = 0
        hh = 0
        if geom is not None and not geom.isEmpty() and pt_index is not None:
            try:
                eng = QgsGeometry.createGeometryEngine(geom.constGet())
                eng.prepareGeometry()
                for fid in pt_index.intersects(geom.boundingBox()):
                    pg = pt_geoms.get(fid)
                    if pg is not None and eng.intersects(pg.constGet()):
                        n_obj += 1
                        hh += pt_hh.get(fid, 0)
            except Exception:
                pass
        plan = plan_splitters(hh)
        c = plan["counts"]
        area = geom.area() if (geom is not None and not geom.isEmpty()) else 0.0
        vals = {
            "SUM_OBJECT": n_obj, "HH": hh, "SUM_HOMES": hh, "area_m2": area,
            "DENSITY": round(hh / (area / 10000.0), 2) if area > 0 else 0.0,
            "SPLIT_SIZE": f"1:{plan['primary']}" if plan["primary"] else "-",
            "SPLIT_CNT": plan["total"], "SPLIT_UTIL": plan["util"], "SPLIT_OK": plan["ok"],
            "SPL_PLAN": plan["label"], "SPL_PORTS": plan["ports"],
            "SPL_4": c.get(4, 0), "SPL_8": c.get(8, 0), "SPL_16": c.get(16, 0),
            "SPL_32": c.get(32, 0), "SPL_64": c.get(64, 0), "REVIEW": 0,
        }
        cps = geom.pointOnSurface() if (geom is not None and not geom.isEmpty()) else None
        if cps is not None and not cps.isEmpty():
            cpt = cps.asPoint()
            vals["CENTR_X"] = float(cpt.x())
            vals["CENTR_Y"] = float(cpt.y())
        for k, v in vals.items():
            if k in names:
                feat[k] = v

    def _safe_polygon(self, geom: QgsGeometry) -> QgsGeometry:
        if geom is None or geom.isEmpty():
            return QgsGeometry()

        if geom.type() == QgsWkbTypes.PolygonGeometry:
            try:
                return geom.makeValid() or geom
            except Exception:
                return geom

        if isinstance(geom.constGet(), QgsGeometryCollection):
            polys = [
                QgsGeometry(p)
                for p in geom.asGeometryCollection()
                if QgsWkbTypes.geometryType(p.wkbType()) == QgsWkbTypes.PolygonGeometry
            ]
            if polys:
                u = polys[0]
                for p in polys[1:]:
                    u = u.combine(p)
                try:
                    return u.makeValid()
                except Exception:
                    return u

        try:
            return geom.buffer(0.0, 8).makeValid()
        except Exception:
            return geom

    # ------------- seeded-growth (splitter-driven) builder -------------

    def _detect_hh_field(self, fields):
        """Case-insensitive lookup of a homes/households column on the INPUT."""
        by_lower = {f.name().lower(): f.name() for f in fields}
        for cand in ("hh", "sum_homes", "homes", "households", "hh_count"):
            if cand in by_lower:
                return by_lower[cand]
        return None

    def _detect_addr_field(self, fields):
        """Case-insensitive lookup of the building address-id column on the INPUT."""
        by_lower = {f.name().lower(): f.name() for f in fields}
        for cand in ("addr_id", "addrid", "address_id", "adr_id", "obj_id", "uid"):
            if cand in by_lower:
                return by_lower[cand]
        return None

    def _load_barriers(self, p, context, feedback, target_crs, premises_ext, need_all_roads):
        """
        Returns (barrier_geoms, barrier_index, road_geoms, road_index).

        Barriers (Barrier Rule — polygons may not cross): restricted road classes
        (expressways etc.) + EVERY feature of the extra barrier layers (railway,
        river, airport zone; polygon zones contribute their boundary rings).
        Roads of ANY class feed the Road Connectivity (access) check.
        Features are pre-filtered to the premises extent for speed.
        """
        def _request_for(lyr, grow):
            try:
                rect = QgsRectangle(premises_ext)
                rect.grow(grow)
                if lyr.crs().isValid() and lyr.crs() != target_crs:
                    xf = QgsCoordinateTransform(target_crs, lyr.crs(), context.transformContext())
                    rect = xf.transformBoundingBox(rect)
                return QgsFeatureRequest().setFilterRect(rect)
            except Exception:
                return QgsFeatureRequest()

        def _xform_for(lyr):
            if lyr.crs().isValid() and lyr.crs() != target_crs:
                return QgsCoordinateTransform(lyr.crs(), target_crs, context.transformContext())
            return None

        barrier_geoms, road_geoms = {}, {}
        barrier_index, road_index = QgsSpatialIndex(), QgsSpatialIndex()
        bid = rid = 0

        def _add(geoms, index, key, g):
            geoms[key] = g
            tf = QgsFeature(key)
            tf.setGeometry(g)
            index.addFeature(tf)

        # ---- road layer: all classes → access check; restricted classes → barriers ----
        lyr = self.parameterAsVectorLayer(p, self.P_BAR_ROADS, context)
        if lyr is not None and lyr.isValid():
            classes_raw = self.parameterAsString(p, self.P_BAR_CLASSES, context) or "motorway,trunk,primary,secondary"
            classes = {c.strip().lower() for c in classes_raw.split(",") if c.strip()}
            fld_list = self.parameterAsFields(p, self.P_BAR_FIELD, context)
            fld = fld_list[0] if fld_list else None
            by_lower = {n.lower(): n for n in lyr.fields().names()}
            if not fld or fld not in lyr.fields().names():
                fld = by_lower.get("fclass") or by_lower.get("highway")
            if not fld:
                feedback.pushWarning(
                    "Growth: road layer has no usable class field (fclass/highway) — "
                    "all roads used for access check, none as barriers."
                )

            def _is_restricted(val):
                if not fld or val in (None, ""):
                    return False
                v = str(val).strip().lower()
                return any(v == c or v.startswith(c + "_") for c in classes)

            xform = _xform_for(lyr)
            for f in lyr.getFeatures(_request_for(lyr, 1000.0)):
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                g = QgsGeometry(g)
                if xform is not None:
                    try:
                        g.transform(xform)
                    except Exception:
                        continue
                if need_all_roads:
                    _add(road_geoms, road_index, rid, g)
                    rid += 1
                if _is_restricted(f[fld] if fld else None):
                    _add(barrier_geoms, barrier_index, bid, g)
                    bid += 1
        else:
            feedback.pushInfo(self.tr("Growth: no road layer supplied — road rules disabled."))

        # ---- extra barrier layers: railway / river / airport zone (all features) ----
        try:
            extra_layers = self.parameterAsLayerList(p, self.P_BAR_EXTRA, context) or []
        except Exception:
            extra_layers = []
        for xl in extra_layers:
            if xl is None or not xl.isValid():
                continue
            xform = _xform_for(xl)
            n0 = bid
            for f in xl.getFeatures(_request_for(xl, 1000.0)):
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                g = QgsGeometry(g)
                if xform is not None:
                    try:
                        g.transform(xform)
                    except Exception:
                        continue
                if QgsWkbTypes.geometryType(g.wkbType()) == QgsWkbTypes.PolygonGeometry:
                    b = g.constGet().boundary()
                    if b is None:
                        continue
                    g = QgsGeometry(b)
                _add(barrier_geoms, barrier_index, bid, g)
                bid += 1
            feedback.pushInfo(f"Growth: barrier layer '{xl.name()}' contributed {bid - n0} features.")

        if barrier_geoms:
            feedback.pushInfo(f"Growth: {len(barrier_geoms)} barrier features indexed "
                              f"(restricted roads + extra barrier layers).")
        return (barrier_geoms, barrier_index if barrier_geoms else None,
                road_geoms, road_index if road_geoms else None)

    def _parts_containing_members(self, geom, member_pts):
        """Keep only the polygon parts that contain at least one member premise."""
        if geom is None or geom.isEmpty():
            return QgsGeometry()
        parts = geom.asGeometryCollection() if geom.isMultipart() else [geom]
        kept = []
        for part in parts:
            if part is None or part.isEmpty():
                continue
            eng = QgsGeometry.createGeometryEngine(part.constGet())
            eng.prepareGeometry()
            for pt in member_pts:
                _pg = QgsGeometry.fromPointXY(pt)
                if eng.intersects(_pg.constGet()):
                    kept.append(part)
                    break
        if not kept:
            return QgsGeometry()
        u = kept[0]
        for k in kept[1:]:
            u = u.combine(k)
        return u

    # ---- straight-sided geometry helpers (proper polygons, no curves) ----

    def _square(self, pt, half):
        """Axis-aligned square footprint around a point — a straight-sided seed."""
        return QgsGeometry.fromRect(
            QgsRectangle(pt.x() - half, pt.y() - half, pt.x() + half, pt.y() + half)
        )

    def _straight_cap(self, member_pts, margin):
        """
        Minimal straight-sided cap for a cluster: convex hull of small building
        squares. Always polygonal (never a curve), contains every member with a
        `margin` of breathing room, and works for 1, 2 or many buildings.
        """
        if not member_pts:
            return QgsGeometry()
        u = QgsGeometry.unaryUnion([self._square(p, margin) for p in member_pts])
        if u is None or u.isEmpty():
            return QgsGeometry()
        hull = u.convexHull()
        return hull if (hull is not None and not hull.isEmpty()) else u

    def _straight_buffer(self, geom, dist):
        """Buffer a barrier line into a straight corridor (square caps, miter joins)."""
        try:
            return geom.buffer(dist, 1, 3, 2, 2.0)  # endCap=square(3), join=miter(2)
        except Exception:
            return geom.buffer(dist, 8)

    def _voronoi_by_cluster(self, owner, pts, crs, feedback):
        """
        Straight, gap-free, non-overlapping tiling: Voronoi (Thiessen) polygons
        of every building, dissolved by cluster id. Returns {cid: QgsGeometry}
        or None if the Voronoi step is unavailable (caller falls back to caps).
        Boundaries between adjacent clusters become straight midlines.

        Uses Shapely's voronoi_polygons to avoid the SIGSEGV in QGIS 3.44's
        qgis:voronoipolygons processing algorithm.
        """
        if not owner or len(owner) < 2:
            return None

        # Primary: Shapely-based Voronoi (avoids QGIS 3.44 SIGSEGV)
        try:
            from shapely import voronoi_polygons
            from shapely.geometry import MultiPoint
            from shapely.ops import unary_union

            ordered_fids = list(pts.keys())
            cids = [owner[fid] for fid in ordered_fids]
            coords = [(pts[fid].x(), pts[fid].y()) for fid in ordered_fids]
            mp = MultiPoint(coords)

            # Build an explicit envelope matching QGIS's BUFFER=100.0 behavior
            # so edge cells extend 100 m beyond the points' bounding box.
            from shapely.geometry import box as shapely_box
            minx = min(c[0] for c in coords)
            miny = min(c[1] for c in coords)
            maxx = max(c[0] for c in coords)
            maxy = max(c[1] for c in coords)
            envelope = shapely_box(minx - 100.0, miny - 100.0,
                                   maxx + 100.0, maxy + 100.0)
            vor = voronoi_polygons(mp, envelope=envelope)

            cluster_cells = {}
            for i, fid in enumerate(ordered_fids):
                if i >= len(vor.geoms):
                    break
                cid = cids[i]
                cell = vor.geoms[i]
                if cell is None or cell.is_empty:
                    continue
                cluster_cells.setdefault(cid, []).append(cell)

            out = {}
            for cid, cells in cluster_cells.items():
                dissolved = unary_union(cells)
                out[cid] = QgsGeometry.fromWkt(dissolved.wkt)

            if out:
                return out
        except Exception as exc:
            feedback.pushWarning(f"Growth: Shapely Voronoi failed ({exc}); trying QGIS fallback.")

        # Fallback: QGIS native voronoipolygons (may crash in QGIS 3.44)
        try:
            vlyr = QgsVectorLayer(f"Point?crs={crs.authid()}", "vpts", "memory")
            pr = vlyr.dataProvider()
            pr.addAttributes([QgsField("cid", QMetaType.Type.Int)])
            vlyr.updateFields()
            feats = []
            for fid, ci in owner.items():
                f = QgsFeature(vlyr.fields())
                f["cid"] = int(ci)
                f.setGeometry(QgsGeometry.fromPointXY(pts[fid]))
                feats.append(f)
            pr.addFeatures(feats)
            vlyr.updateExtents()

            vor = processing.run("qgis:voronoipolygons",
                                 {"INPUT": vlyr, "BUFFER": 100.0, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT})["OUTPUT"]
            diss = processing.run("native:dissolve",
                                  {"INPUT": vor, "FIELD": ["cid"], "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT})["OUTPUT"]
            out = {}
            for f in diss.getFeatures():
                cid = f["cid"]
                if cid is None:
                    continue
                out[int(cid)] = QgsGeometry(f.geometry())
            return out or None
        except Exception as exc:
            feedback.pushWarning(f"Growth: Voronoi tiling unavailable ({exc}); using convex caps only.")
            return None

    def _process_growth(self, p, context, feedback, src, sink, fields_out,
                        buf_m, clip_lyr):
        """
        Constrained agglomerative (Ward-linkage) region building under the polygon
        creation rules: Distance (neighbour ≤ NEIGHBOR_DIST graph), Home Count
        (MIN..MAX homes), Barrier (restricted roads / railway / river / airport),
        Service Radius (member ≤ SERVICE_RADIUS from the cluster centroid ≈ future
        FDP), Road Connectivity (access check), No Overlap, ≥3 buildings.
        Every building starts as its own cluster; the globally nearest rule-
        compliant pair is merged repeatedly, so nearby buildings group first and
        none lands in a far cluster while a nearer one is open. Clusters that stay
        below the minimum band with no legal merge are flagged REVIEW=1.
        Membership is the source of truth: homes / building counts are exact sums
        over members, never spatial recounts. Splitter size (1:8…1:64) is computed
        per polygon targeting 60–90 % utilization.
        """
        neigh_dist = float(self.parameterAsDouble(p, self.P_NEIGH_DIST, context) or 150.0)
        seedbuf = float(self.parameterAsDouble(p, self.P_SEEDBUF, context) or 0.0)
        min_hh = max(1, self.parameterAsInt(p, self.P_MIN_HH, context) or 32)
        max_hh = self.parameterAsInt(p, self.P_MAX_HH, context) or 128
        if max_hh < min_hh:
            max_hh = min_hh
        service_radius = float(self.parameterAsDouble(p, self.P_SERVICE_RADIUS, context) or 300.0)
        access_dist = float(self.parameterAsDouble(p, self.P_ACCESS_DIST, context) or 0.0)
        feedback.pushInfo(
            f"Growth rules: {min_hh}–{max_hh} homes/polygon, neighbour ≤ {neigh_dist:.0f} m, "
            f"service radius ≤ {service_radius:.0f} m, road access ≤ {access_dist:.0f} m."
        )

        # ---- collect premises (membership units) ----
        hh_name = self._detect_hh_field(src.fields())
        if hh_name:
            feedback.pushInfo(f"Growth: homes read from INPUT field '{hh_name}'.")
        else:
            feedback.pushWarning("Growth: no HH field found on INPUT — each premise counts as 1 home.")

        addr_name = self._detect_addr_field(src.fields())
        if addr_name:
            feedback.pushInfo(f"Growth: buildings identified by ADDR_ID field '{addr_name}'.")
        else:
            feedback.pushWarning("Growth: no ADDR_ID field on INPUT — using feature id for the 1-building/1-polygon check.")

        pts = {}
        hh_of = {}
        addr_of = {}                                   # fid -> ADDR_ID (building identity)
        for f in src.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            pos = g.pointOnSurface()                   # locate the building by its X/Y
            if pos is None or pos.isEmpty():
                continue
            pt = pos.asPoint()
            pts[f.id()] = QgsPointXY(pt)
            hh = 1
            if hh_name:
                try:
                    v = f[hh_name]
                    if v is not None:
                        hh = max(0, int(float(v)))
                except Exception:
                    hh = 1
            hh_of[f.id()] = hh
            addr_of[f.id()] = f[addr_name] if addr_name else f.id()

        if not pts:
            raise QgsProcessingException("Growth: INPUT contains no usable premise geometries.")

        # ---- neighbour graph within NEIGHBOR_DIST (Distance Rule, computed once) ----
        pt_index = QgsSpatialIndex()
        for fid, pt in pts.items():
            tf = QgsFeature(fid)
            tf.setGeometry(QgsGeometry.fromPointXY(pt))
            pt_index.addFeature(tf)

        neighbors = {}
        for fid, pt in pts.items():
            rect = QgsRectangle(pt.x() - neigh_dist, pt.y() - neigh_dist,
                                pt.x() + neigh_dist, pt.y() + neigh_dist)
            lst = []
            for nid in pt_index.intersects(rect):
                if nid == fid:
                    continue
                npt = pts.get(nid)
                if npt is None:
                    continue
                d = math.hypot(npt.x() - pt.x(), npt.y() - pt.y())
                if d <= neigh_dist:
                    lst.append((d, nid))
            lst.sort()
            neighbors[fid] = lst

        # ---- barriers (Barrier Rule) + roads of any class (Road Connectivity) ----
        barrier_geoms, barrier_index, road_geoms, road_index = self._load_barriers(
            p, context, feedback, src.sourceCrs(), src.sourceExtent(),
            need_all_roads=(access_dist > 0)
        )
        barrier_engines = {}

        def crosses_barrier(a, b):
            if barrier_index is None:
                return False
            seg = QgsGeometry.fromPolylineXY([a, b])
            for xid in barrier_index.intersects(seg.boundingBox()):
                eng = barrier_engines.get(xid)
                if eng is None:
                    eng = QgsGeometry.createGeometryEngine(barrier_geoms[xid].constGet())
                    eng.prepareGeometry()
                    barrier_engines[xid] = eng
                if eng.intersects(seg.constGet()):
                    return True
            return False

        # ---- Road Connectivity Rule: every building should have road access ----
        has_access = None
        if road_index is not None and access_dist > 0:
            has_access = {}
            for fid, pt in pts.items():
                rect = QgsRectangle(pt.x() - access_dist, pt.y() - access_dist,
                                    pt.x() + access_dist, pt.y() + access_dist)
                pg = QgsGeometry.fromPointXY(pt)
                ok = False
                for rd in road_index.intersects(rect):
                    if road_geoms[rd].distance(pg) <= access_dist:
                        ok = True
                        break
                has_access[fid] = ok
            n_no = sum(1 for v in has_access.values() if not v)
            if n_no:
                feedback.pushWarning(
                    f"Growth: {n_no} building(s) have no road within {access_dist:.0f} m "
                    "(Road Connectivity Rule) — counted per polygon in NO_ACCESS."
                )

        # ---- Phase A/B: CONSTRAINED AGGLOMERATIVE CLUSTERING (Ward linkage) ----
        # Bottom-up region building: every building starts as its own cluster and
        # the globally nearest (Ward-cost) adjacent pair is merged whenever the
        # merge keeps all rules — Home Count (combined ≤ max_hh), Service Radius
        # (all members ≤ SERVICE_RADIUS from the joint centroid) and Barrier (a
        # barrier-free neighbour edge must join them). Merging nearest-first yields
        # compact, capacity-bounded serving areas: a building never lands in a far
        # cluster while a nearer rule-compliant one is still open. This replaces the
        # old seed+grow accretion and the undersized / adjacency / singleton merge
        # heuristics with a single, globally consistent linkage.
        total_pts = len(pts)
        clusters = []                                  # {"members","hh","review","sx","sy"}
        assigned = {}                                  # fid -> cluster index
        for fid in pts:
            ci = len(clusters)
            clusters.append({"members": [fid], "hh": hh_of[fid], "review": False,
                             "sx": pts[fid].x(), "sy": pts[fid].y()})
            assigned[fid] = ci

        merged_into = {}                               # child ci -> parent ci (see _resolve)

        def _resolve(ci):
            while ci in merged_into:
                ci = merged_into[ci]
            return ci

        def _undersized(ci):
            cl = clusters[ci]
            return cl["hh"] < min_hh or len(cl["members"]) < 3

        def _radius_ok(members_a, members_b):
            allm = members_a + members_b
            cx = sum(pts[m].x() for m in allm) / len(allm)
            cy = sum(pts[m].y() for m in allm) / len(allm)
            return all(
                math.hypot(pts[m].x() - cx, pts[m].y() - cy) <= service_radius
                for m in allm
            )

        def _ward_cost(a, b):
            ca, cb = clusters[a], clusters[b]
            na, nb = len(ca["members"]), len(cb["members"])
            dx = ca["sx"] / na - cb["sx"] / nb
            dy = ca["sy"] / na - cb["sy"] / nb
            return (na * nb / float(na + nb)) * (dx * dx + dy * dy)

        # candidate merges = barrier-free neighbour-graph edges between clusters
        cl_adj = defaultdict(set)
        for fid, nb in neighbors.items():
            a = assigned[fid]
            for _d, nid in nb:
                if crosses_barrier(pts[fid], pts[nid]):
                    continue
                b = assigned[nid]
                if a != b:
                    cl_adj[a].add(b)
                    cl_adj[b].add(a)

        def _barrier_free_link(a, b):
            for m in clusters[a]["members"]:
                for _d, nid in neighbors.get(m, ()):
                    if _resolve(assigned.get(nid, -1)) == b and not crosses_barrier(pts[m], pts[nid]):
                        return True
            return False

        heap = []
        for a in cl_adj:
            for b in cl_adj[a]:
                if a < b:
                    heapq.heappush(heap, (_ward_cost(a, b), a, b))

        n_merges = 0
        while heap and not feedback.isCanceled():
            cost, a, b = heapq.heappop(heap)
            a, b = _resolve(a), _resolve(b)
            if a == b or not clusters[a]["members"] or not clusters[b]["members"]:
                continue
            # lazy revalidation: earlier merges move centroids → recompute the cost
            cur = _ward_cost(a, b)
            if cur > cost + 1e-6:
                heapq.heappush(heap, (cur, a, b))
                continue
            # rule gates (each is monotonic — a failed pair can never later pass)
            if clusters[a]["hh"] + clusters[b]["hh"] > max_hh:      # Home Count (max)
                continue
            if not _radius_ok(clusters[a]["members"], clusters[b]["members"]):  # Service Radius
                continue
            if not _barrier_free_link(a, b):                        # Barrier
                continue
            # merge the smaller cluster into the larger (stable parent id)
            if len(clusters[a]["members"]) < len(clusters[b]["members"]):
                a, b = b, a
            clusters[a]["members"].extend(clusters[b]["members"])
            clusters[a]["hh"] += clusters[b]["hh"]
            clusters[a]["sx"] += clusters[b]["sx"]
            clusters[a]["sy"] += clusters[b]["sy"]
            for mm in clusters[b]["members"]:
                assigned[mm] = a
            merged_into[b] = a
            # a inherits b's (barrier-free) adjacency
            for nb in list(cl_adj[b]):
                rnb = _resolve(nb)
                if rnb != a and clusters[rnb]["members"]:
                    cl_adj[a].add(rnb)
                    cl_adj[rnb].add(a)
            n_merges += 1
            for nb in list(cl_adj[a]):
                rnb = _resolve(nb)
                if rnb != a and clusters[rnb]["members"]:
                    lo, hi = (a, rnb) if a < rnb else (rnb, a)
                    heapq.heappush(heap, (_ward_cost(lo, hi), lo, hi))
            if n_merges % 25 == 0:
                try:
                    feedback.setProgress(int(50 * n_merges / max(1, total_pts)))
                except Exception:
                    pass

        n_clusters = sum(1 for ci in range(len(clusters))
                         if ci not in merged_into and clusters[ci]["members"])
        feedback.pushInfo(
            f"Growth: agglomerative (Ward) clustering — {n_merges} merges → {n_clusters} clusters."
        )

        # HOME COUNT (min) + ≥3-buildings: flag survivors that stay undersized
        n_under = 0
        for ci in range(len(clusters)):
            if ci in merged_into or not clusters[ci]["members"]:
                continue
            if _undersized(ci):
                clusters[ci]["review"] = True
                n_under += 1
        if n_under:
            feedback.pushWarning(
                f"Growth: {n_under} cluster(s) stay below the minimum band "
                f"({min_hh} homes / ≥3 buildings) — no rule-compliant merge available; REVIEW=1."
            )

        # ---- Phase B.3: CAPACITY CONSOLIDATION (compactness-gated) ----
        # Ward links buildings within NEIGHBOR_DIST, so two small clusters separated
        # by a slightly larger gap are never offered for merging even when they would
        # fit under the cap. This pass looks at CLUSTER proximity: it merges the
        # nearest pair of clusters (smallest Ward cost) whenever they (a) together
        # stay within the cap, (b) join barrier-free and (c) stay COMPACT — the merged
        # cluster's reach may grow by at most one neighbour-hop and never past the
        # service radius. Closest-first + the reach gate keep clusters round, so this
        # only clubs genuinely-nearby under-cap clusters — a no-op once the area is
        # already packed to the cap (as dense urban input usually is).
        def _joint_max_radius(members):
            cx = sum(pts[m].x() for m in members) / len(members)
            cy = sum(pts[m].y() for m in members) / len(members)
            return max(math.hypot(pts[m].x() - cx, pts[m].y() - cy) for m in members)

        def _nearest_link(a, b):
            best = None
            for i in clusters[a]["members"]:
                for j in clusters[b]["members"]:
                    d = math.hypot(pts[i].x() - pts[j].x(), pts[i].y() - pts[j].y())
                    if best is None or d < best[0]:
                        best = (d, i, j)
            return best

        surv = [ci for ci in range(len(clusters))
                if ci not in merged_into and clusters[ci]["members"]]
        ccx = {ci: clusters[ci]["sx"] / len(clusters[ci]["members"]) for ci in surv}
        ccy = {ci: clusters[ci]["sy"] / len(clusters[ci]["members"]) for ci in surv}
        crad = {ci: _joint_max_radius(clusters[ci]["members"]) for ci in surv}
        cidx = QgsSpatialIndex()
        for ci in surv:
            tf = QgsFeature(ci)
            tf.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(ccx[ci], ccy[ci])))
            cidx.addFeature(tf)

        cheap = []

        def _push_consol_pairs(ci):
            rect = QgsRectangle(ccx[ci] - service_radius, ccy[ci] - service_radius,
                                ccx[ci] + service_radius, ccy[ci] + service_radius)
            for nj in cidx.intersects(rect):
                rnj = _resolve(nj)
                if rnj != ci and clusters[rnj]["members"]:
                    heapq.heappush(cheap, (_ward_cost(ci, rnj), min(ci, rnj), max(ci, rnj)))

        for ci in surv:
            _push_consol_pairs(ci)

        consolidated = 0
        while cheap and not feedback.isCanceled():
            cost, a, b = heapq.heappop(cheap)
            a, b = _resolve(a), _resolve(b)
            if a == b or not clusters[a]["members"] or not clusters[b]["members"]:
                continue
            cur = _ward_cost(a, b)
            if cur > cost + 1e-6:                                       # lazy revalidation
                heapq.heappush(cheap, (cur, min(a, b), max(a, b)))
                continue
            if clusters[a]["hh"] + clusters[b]["hh"] > max_hh:          # capacity
                continue
            new_rad = _joint_max_radius(clusters[a]["members"] + clusters[b]["members"])
            if new_rad > service_radius:                               # Service Radius
                continue
            if new_rad > max(crad[a], crad[b]) + neigh_dist:           # compactness gate
                continue
            link = _nearest_link(a, b)                                 # Barrier
            if link is None or crosses_barrier(pts[link[1]], pts[link[2]]):
                continue
            if len(clusters[a]["members"]) < len(clusters[b]["members"]):
                a, b = b, a
            clusters[a]["members"].extend(clusters[b]["members"])
            clusters[a]["hh"] += clusters[b]["hh"]
            clusters[a]["sx"] += clusters[b]["sx"]
            clusters[a]["sy"] += clusters[b]["sy"]
            for mm in clusters[b]["members"]:
                assigned[mm] = a
            merged_into[b] = a
            clusters[a]["review"] = _undersized(a)
            n = len(clusters[a]["members"])
            ccx[a], ccy[a] = clusters[a]["sx"] / n, clusters[a]["sy"] / n
            crad[a] = _joint_max_radius(clusters[a]["members"])
            consolidated += 1
            _push_consol_pairs(a)
        if consolidated:
            feedback.pushInfo(
                f"Growth: capacity consolidation merged {consolidated} nearby under-cap cluster(s) "
                "→ fewer FDPs (compactness preserved)."
            )
        else:
            feedback.pushInfo(
                "Growth: capacity consolidation — no nearby under-cap clusters to merge "
                "(already packed to the cap)."
            )

        # ---- Phase B.4: NO-SINGLETON guarantee ----
        # A polygon must never hold just one building (one ADDR_ID). Any surviving
        # single-building cluster is merged into the nearest barrier-free
        # neighbour, chosen by capacity: prefer a neighbour that stays within the
        # cap, then within soft-cap overflow, else the nearest neighbour outright
        # (flagged REVIEW). Only a building with NO barrier-free neighbour at all
        # can remain alone — and that is a genuinely isolated premise.
        singleton_soft = int(round(max_hh * GROWTH_OVERFLOW))
        singleton_merges = 0
        for _sround in range(4):
            singles = [ci for ci in range(len(clusters))
                       if ci not in merged_into and len(clusters[ci]["members"]) == 1]
            if not singles:
                break
            did = 0
            for ci in singles:
                ci = _resolve(ci)
                if ci in merged_into or len(clusters[ci]["members"]) != 1:
                    continue
                m = clusters[ci]["members"][0]
                # candidate neighbour clusters via the graph, nearest edge first
                cand = []
                for d, nid in neighbors.get(m, ()):
                    na = assigned.get(nid)
                    nci = _resolve(na) if na is not None else None
                    if nci is None or nci == ci or not clusters[nci]["members"]:
                        continue
                    if crosses_barrier(pts[m], pts[nid]):
                        continue
                    cand.append((d, nci))
                if not cand:
                    continue                                   # genuinely isolated
                cand.sort(key=lambda t: t[0])
                # pick best by capacity tier: in-cap → soft-cap → nearest
                target = None
                for _tier_cap in (max_hh, singleton_soft, None):
                    for d, nci in cand:
                        if _tier_cap is None or clusters[nci]["hh"] + clusters[ci]["hh"] <= _tier_cap:
                            target = nci
                            break
                    if target is not None:
                        break
                if target is None:
                    continue
                clusters[target]["members"].extend(clusters[ci]["members"])
                clusters[target]["hh"] += clusters[ci]["hh"]
                clusters[target]["sx"] += clusters[ci]["sx"]
                clusters[target]["sy"] += clusters[ci]["sy"]
                for mm in clusters[ci]["members"]:
                    assigned[mm] = target
                if clusters[target]["hh"] > max_hh:
                    clusters[target]["review"] = True
                merged_into[ci] = target
                did += 1
            singleton_merges += did
            if did == 0:
                break
        if singleton_merges:
            feedback.pushInfo(
                f"Growth: merged {singleton_merges} single-building polygon(s) into nearby polygons "
                "(capacity-based; over-cap merges flagged REVIEW)."
            )
        left_singletons = sum(1 for ci in range(len(clusters))
                              if ci not in merged_into and len(clusters[ci]["members"]) == 1)
        if left_singletons:
            feedback.pushWarning(
                f"Growth: {left_singletons} building(s) remain alone — no barrier-free neighbour "
                "to merge into (isolated premises)."
            )

        # ---- Phase B.5: COMPACTION (Lloyd relaxation) ----
        # Rule-based growth/merges leave clusters spatially interleaved (buildings
        # of one cluster reaching into another), which forces jagged, overlapping
        # boundaries. Here each building is pulled toward the cluster centroid it
        # is actually closest to — but ONLY along a barrier-free 150 m neighbour
        # edge, only if capacity/min-band/service-radius still hold. This rounds
        # the clusters so their polygons come out as clean, non-overlapping shapes
        # while every polygon-creation rule stays satisfied.
        def _centroid(ci):
            ms = clusters[ci]["members"]
            n = max(1, len(ms))
            return (clusters[ci]["sx"] / n, clusters[ci]["sy"] / n)

        compact_moves = 0
        for _it in range(8):
            cents = {ci: _centroid(ci) for ci in range(len(clusters))
                     if ci not in merged_into and clusters[ci]["members"]}
            moved = 0
            for fid in list(assigned):
                ci = _resolve(assigned[fid])
                if ci not in cents or len(clusters[ci]["members"]) <= 3:
                    continue
                if clusters[ci]["hh"] - hh_of[fid] < min_hh:
                    continue                                   # keep donor in-band
                cx, cy = cents[ci]
                d_own = math.hypot(pts[fid].x() - cx, pts[fid].y() - cy)
                best = None
                seen = set()
                for _d, nid in neighbors.get(fid, ()):
                    na = assigned.get(nid)
                    nci = _resolve(na) if na is not None else None
                    if nci is None or nci == ci or nci in seen or nci not in cents:
                        continue
                    seen.add(nci)
                    ncx, ncy = cents[nci]
                    d_new = math.hypot(pts[fid].x() - ncx, pts[fid].y() - ncy)
                    if d_new >= d_own:
                        continue                               # only moves that increase compactness
                    if clusters[nci]["hh"] + hh_of[fid] > max_hh:
                        continue                               # Home Count Rule
                    if d_new > service_radius:
                        continue                               # Service Radius Rule
                    if crosses_barrier(pts[fid], pts[nid]):
                        continue                               # Barrier Rule
                    if best is None or d_new < best[1]:
                        best = (nci, d_new)
                if best is not None:
                    nci = best[0]
                    clusters[ci]["members"].remove(fid)
                    clusters[ci]["hh"] -= hh_of[fid]
                    clusters[ci]["sx"] -= pts[fid].x()
                    clusters[ci]["sy"] -= pts[fid].y()
                    clusters[nci]["members"].append(fid)
                    clusters[nci]["hh"] += hh_of[fid]
                    clusters[nci]["sx"] += pts[fid].x()
                    clusters[nci]["sy"] += pts[fid].y()
                    assigned[fid] = nci
                    moved += 1
            compact_moves += moved
            if moved == 0:
                break
        if compact_moves:
            feedback.pushInfo(f"Growth: compaction relocated {compact_moves} building(s) to rounder clusters.")

        clip_union = None
        if clip_lyr is not None:
            try:
                clip_union = self._collect_union_geom(clip_lyr)
                if clip_union is None or clip_union.isEmpty():
                    feedback.pushWarning("Growth: clip layer is empty — output not clipped.")
                    clip_union = None
            except Exception as exc:
                feedback.pushWarning(f"Growth: clip layer unusable ({exc}) — output not clipped.")
                clip_union = None

        # ---- Phase C: FINALIZE — organic, separated polygons ----
        names = fields_out.names()
        emit_order = [ci for ci in range(len(clusters))
                      if ci not in merged_into and clusters[ci]["members"]]
        # Emit larger clusters first so they claim territory before smaller neighbours
        # when convex caps overlap and get trimmed by the emitted_geom guard.
        emit_order.sort(key=lambda ci: clusters[ci]["hh"], reverse=True)
        owner = {}
        for _fid, _ci in assigned.items():
            _r = _resolve(_ci)
            if clusters[_r]["members"]:
                owner[_fid] = _r
        margin = min(25.0, max(8.0, neigh_dist * 0.15))
        # Visible gap between adjacent polygons — large enough to read at map scale.
        visual_gap = max(2.0, margin * 0.4)

        # Voronoi (Thiessen) partition of every building, dissolved per cluster:
        # a TRUE spatial partition — cells are disjoint and each building lies in
        # its OWN cluster's cell, so containment (1 building → 1 polygon) and
        # non-overlap are guaranteed by construction, even for buildings the
        # capacity cap kept out of their nearest cluster. The COMPACTION above
        # rounds the clusters so this partition comes out with far fewer sides
        # than it would on the raw interleaved clusters. The convex cap trims each
        # cell to its buildings so the polygon stays minimal.
        vmap = self._voronoi_by_cluster(owner, pts, src.sourceCrs(), feedback)
        if not vmap:
            feedback.pushWarning(
                "Growth: Voronoi partition unavailable — falling back to convex caps with "
                "sequential overlap subtraction (still non-overlapping, but shapes may be angular)."
            )

        # ---- DEFRAGMENTATION: reassign only the buildings whose Voronoi cell is
        # stranded away from its cluster's main body (these are exactly the "lone
        # square inside a neighbour" fragments). A building is stranded if its
        # point is not in the LARGEST connected part of its cluster's cell; move
        # it to the neighbour that owns the surrounding ground (capacity / barrier
        # / service-radius enforced). Rebuild the partition once afterwards. This
        # targets the few fragmenting buildings without smearing cluster borders.
        if vmap:
            soft_cap = int(round(max_hh * GROWTH_OVERFLOW))
            overflow_used = 0
            # Rounds 0–2: strict cap. Rounds 3–5: allow the surrounding cluster to
            # go up to soft_cap so a capacity-locked stray never stays a lone
            # square (the absorbing polygon is flagged REVIEW).
            for _round in range(6):
                cap = max_hh if _round < 3 else soft_cap
                stranded = []
                for ci in emit_order:
                    cell = vmap.get(ci)
                    if cell is None or cell.isEmpty() or not cell.isMultipart():
                        continue
                    parts = cell.asGeometryCollection()
                    if len(parts) < 2:
                        continue
                    main = max(parts, key=lambda g: g.area())
                    eng = QgsGeometry.createGeometryEngine(main.constGet())
                    eng.prepareGeometry()
                    for m in clusters[ci]["members"]:
                        _pg = QgsGeometry.fromPointXY(pts[m])
                        if not eng.intersects(_pg.constGet()):
                            stranded.append((ci, m))
                if not stranded:
                    break
                moved = 0
                for ci, m in stranded:
                    ci = _resolve(ci)
                    if m not in clusters[ci]["members"] or len(clusters[ci]["members"]) <= 3:
                        continue
                    votes = Counter()
                    for _d, nid in neighbors.get(m, ()):
                        na = assigned.get(nid)
                        nci = _resolve(na) if na is not None else None
                        if nci is None or nci == ci or not clusters[nci]["members"]:
                            continue
                        if clusters[nci]["hh"] + hh_of[m] > cap:
                            continue
                        if crosses_barrier(pts[m], pts[nid]):
                            continue
                        ncx = clusters[nci]["sx"] / len(clusters[nci]["members"])
                        ncy = clusters[nci]["sy"] / len(clusters[nci]["members"])
                        if math.hypot(pts[m].x() - ncx, pts[m].y() - ncy) > service_radius:
                            continue
                        votes[nci] += 1
                    if not votes:
                        continue
                    nci = votes.most_common(1)[0][0]
                    clusters[ci]["members"].remove(m)
                    clusters[ci]["hh"] -= hh_of[m]
                    clusters[ci]["sx"] -= pts[m].x()
                    clusters[ci]["sy"] -= pts[m].y()
                    clusters[nci]["members"].append(m)
                    clusters[nci]["hh"] += hh_of[m]
                    clusters[nci]["sx"] += pts[m].x()
                    clusters[nci]["sy"] += pts[m].y()
                    assigned[m] = nci
                    if clusters[nci]["hh"] > max_hh:
                        clusters[nci]["review"] = True     # over the cap by design → flag
                        overflow_used += 1
                    moved += 1
                if not moved:
                    break
                feedback.pushInfo(f"Growth: defragmentation moved {moved} stranded building(s) into surrounding polygons.")
                emit_order = [ci for ci in range(len(clusters))
                              if ci not in merged_into and clusters[ci]["members"]]
                emit_order.sort(key=lambda ci: clusters[ci]["hh"], reverse=True)
                owner = {}
                for _fid, _ci in assigned.items():
                    _r = _resolve(_ci)
                    if clusters[_r]["members"]:
                        owner[_fid] = _r
                vmap = self._voronoi_by_cluster(owner, pts, src.sourceCrs(), feedback)
                if not vmap:
                    break
            if overflow_used:
                feedback.pushInfo(
                    f"Growth: {overflow_used} stray building(s) absorbed with capacity overflow "
                    f"(up to {soft_cap} homes) to avoid lone squares — those polygons flagged REVIEW."
                )

        # ---- Phase B.6: DE-ISLAND — force single-part serving areas ----
        # A serving polygon must be ONE contiguous boundary. Any building still
        # stranded in a detached Voronoi fragment (its point is not in the largest
        # connected part of its cluster's cell) is absorbed into the neighbouring
        # cluster that geographically surrounds it. The stray sits inside that
        # neighbour's territory, so it is served from the SAME side of any barrier:
        # a barrier-free neighbour is preferred, but when none exists the stray is
        # absorbed across the barrier anyway (one clean polygon is worth more than
        # keeping a single embedded building on its far-side cluster). This is what
        # eliminates the multipart "island" polygons. Over-cap absorbers → REVIEW.
        if vmap:
            deisland_moved = 0
            for _round in range(4):
                stranded = []
                for ci in emit_order:
                    cell = vmap.get(ci)
                    if cell is None or cell.isEmpty() or not cell.isMultipart():
                        continue
                    parts = cell.asGeometryCollection()
                    if len(parts) < 2:
                        continue
                    main = max(parts, key=lambda g: g.area())
                    eng = QgsGeometry.createGeometryEngine(main.constGet())
                    eng.prepareGeometry()
                    for m in clusters[ci]["members"]:
                        _pg = QgsGeometry.fromPointXY(pts[m])
                        if not eng.intersects(_pg.constGet()):
                            stranded.append((ci, m))
                if not stranded:
                    break
                moved = 0
                for ci, m in stranded:
                    ci = _resolve(ci)
                    if m not in clusters[ci]["members"] or len(clusters[ci]["members"]) <= 3:
                        continue                       # keep the donor a valid (≥3) cluster
                    votes_free, votes_any = Counter(), Counter()
                    for _d, nid in neighbors.get(m, ()):
                        na = assigned.get(nid)
                        nci = _resolve(na) if na is not None else None
                        if nci is None or nci == ci or not clusters[nci]["members"]:
                            continue
                        votes_any[nci] += 1
                        if not crosses_barrier(pts[m], pts[nid]):
                            votes_free[nci] += 1
                    votes = votes_free or votes_any    # prefer a barrier-free absorber
                    if votes:
                        nci = votes.most_common(1)[0][0]
                    else:
                        # No 150 m graph-neighbour in another cluster, but the stray
                        # still sits inside SOMEONE's territory — absorb into the
                        # cluster whose Voronoi cell is nearest (the ground that
                        # actually surrounds it), so the island still disappears.
                        pgeom = QgsGeometry.fromPointXY(pts[m])
                        best_d, nci = None, None
                        for oci in emit_order:
                            if oci == ci or not clusters[oci]["members"]:
                                continue
                            ocell = vmap.get(oci)
                            if ocell is None or ocell.isEmpty():
                                continue
                            d = ocell.distance(pgeom)
                            if best_d is None or d < best_d:
                                best_d, nci = d, oci
                        if nci is None:
                            continue                    # truly nothing to absorb into
                    clusters[ci]["members"].remove(m)
                    clusters[ci]["hh"] -= hh_of[m]
                    clusters[ci]["sx"] -= pts[m].x()
                    clusters[ci]["sy"] -= pts[m].y()
                    clusters[nci]["members"].append(m)
                    clusters[nci]["hh"] += hh_of[m]
                    clusters[nci]["sx"] += pts[m].x()
                    clusters[nci]["sy"] += pts[m].y()
                    assigned[m] = nci
                    if clusters[nci]["hh"] > max_hh:
                        clusters[nci]["review"] = True
                    moved += 1
                if not moved:
                    break
                deisland_moved += moved
                emit_order = [ci for ci in range(len(clusters))
                              if ci not in merged_into and clusters[ci]["members"]]
                emit_order.sort(key=lambda ci: clusters[ci]["hh"], reverse=True)
                owner = {}
                for _fid, _ci in assigned.items():
                    _r = _resolve(_ci)
                    if clusters[_r]["members"]:
                        owner[_fid] = _r
                vmap = self._voronoi_by_cluster(owner, pts, src.sourceCrs(), feedback)
                if not vmap:
                    break
            if deisland_moved:
                feedback.pushInfo(
                    f"Growth: de-island absorbed {deisland_moved} stranded building(s) into the "
                    "surrounding polygon so every serving area is a single contiguous boundary."
                )

        poly_id_of_cluster = {}
        geom_of_cluster = {}                  # ci -> emitted polygon (for the ADDR_ID containment check)
        serial = 1
        emitted_geom = QgsGeometry()          # cumulative union of all polygons emitted so far

        # Small per-building footprint radius, used by the fallback / gap-patch
        # to guarantee a member is never left outside its own polygon.
        ownsq_r = min(GROWTH_MEMBER_PROTECT * 0.6, max(3.0, margin * 0.3))

        review_of = {}
        for n_done, ci in enumerate(emit_order):
            if feedback.isCanceled():
                break
            cl = clusters[ci]
            member_pts = [pts[m] for m in cl["members"]]
            review = bool(cl["review"]) or len(cl["members"]) < 3 or cl["hh"] < min_hh

            # straight convex cap (union of building squares → convex hull)
            geom = self._straight_cap(member_pts, margin)
            # PRIMARY boundary: trim the cap to this cluster's Voronoi cell so the
            # polygon can never reach into a neighbour's territory. This is what
            # keeps 1 building → 1 polygon geographically (no overlaps).
            vgeom = vmap.get(ci) if vmap else None
            if vgeom is not None and not vgeom.isEmpty():
                trimmed = geom.intersection(vgeom)
                if trimmed is not None and not trimmed.isEmpty():
                    geom = trimmed
            # Apply seedbuf and post-buffer.
            if seedbuf > 0 and not geom.isEmpty():
                geom = self._straight_buffer(geom, seedbuf)
            if abs(buf_m) > 0.0 and not geom.isEmpty():
                geom = self._straight_buffer(geom, buf_m)

            # Barrier Rule visible in geometry: cut a thin straight corridor,
            # protecting a square around each own member so a building geocoded
            # right on a barrier stays inside its polygon.
            if barrier_index is not None and not geom.isEmpty():
                bids = barrier_index.intersects(geom.boundingBox())
                if bids:
                    corridor = QgsGeometry.unaryUnion(
                        [self._straight_buffer(barrier_geoms[x], GROWTH_ROAD_HALF_WIDTH) for x in bids]
                    )
                    own_shield = QgsGeometry.unaryUnion(
                        [self._square(q, GROWTH_MEMBER_PROTECT) for q in member_pts]
                    )
                    geom = geom.difference(corridor.difference(own_shield))

            # ---- strict non-overlap safety net: subtract all previously emitted
            # geometry (Voronoi cells are already disjoint, so this is a no-op
            # unless a cell was unavailable / a buffer pushed past the cell). ----
            if not emitted_geom.isEmpty() and not geom.isEmpty():
                geom = geom.difference(emitted_geom)

            geom = self._parts_containing_members(geom, member_pts)
            if geom is None or geom.isEmpty():
                feedback.pushWarning(
                    f"Growth: polygon for cluster {ci} lost its member-bearing parts — "
                    "falling back to member footprints (REVIEW=1)."
                )
                geom = QgsGeometry.unaryUnion([self._square(q, ownsq_r) for q in member_pts])
                if not emitted_geom.isEmpty() and geom is not None and not geom.isEmpty():
                    geom = geom.difference(emitted_geom)
                review = True

            # Tiny inward buffer so adjacent polygons read as separate at map
            # scale — but never push a member outside its own polygon (patch any
            # lost member back, bounded to the pre-gap geom so it can't re-enter a
            # neighbour's territory).
            if visual_gap > 0 and geom is not None and not geom.isEmpty():
                pre_gap = QgsGeometry(geom)
                gapped = geom.buffer(-visual_gap, 8)
                if gapped is not None and not gapped.isEmpty():
                    geom = gapped
                    lost = [q for q in member_pts
                            if not geom.intersects(QgsGeometry.fromPointXY(q))]
                    if lost:
                        patch = QgsGeometry.unaryUnion(
                            [self._square(q, ownsq_r) for q in lost]
                        ).intersection(pre_gap)
                        if not emitted_geom.isEmpty() and patch is not None and not patch.isEmpty():
                            patch = patch.difference(emitted_geom)
                        if patch is not None and not patch.isEmpty():
                            geom = geom.combine(patch)

            if clip_union is not None:
                clipped = geom.intersection(clip_union)
                if clipped is None or clipped.isEmpty():
                    feedback.pushWarning(
                        f"Growth: clip would remove polygon of cluster {ci} entirely — kept unclipped (REVIEW=1)."
                    )
                    review = True
                else:
                    geom = clipped

            geom = self._safe_polygon(geom)
            if geom.isEmpty():
                feedback.pushWarning(f"Growth: cluster {ci} produced no valid polygon — skipped (members stay unpolygonized).")
                continue
            # Clean the boundary: drop the many short Voronoi micro-segments so the
            # polygon reads as a proper few-sided shape. Douglas–Peucker keeps
            # ring endpoints, so it never pushes a contained building outside. If a
            # simplify would drop a member out, keep the un-simplified geom.
            if not geom.isEmpty():
                simp = geom.simplify(max(3.0, margin * 0.5))
                if (simp is not None and not simp.isEmpty()
                        and all(simp.intersects(QgsGeometry.fromPointXY(q)) for q in member_pts)):
                    simp = simp.difference(emitted_geom) if not emitted_geom.isEmpty() else simp
                    if simp is not None and not simp.isEmpty() and \
                            all(simp.intersects(QgsGeometry.fromPointXY(q)) for q in member_pts):
                        geom = simp
            # SINGLE-BOUNDARY guard: a serving polygon must be one contiguous piece.
            # First drop any member-free part the visual gap / simplify pinched off;
            # if genuine member-bearing parts still remain (a thin neck eroded by the
            # gap buffer, or a slice carved by an already-emitted neighbour), keep the
            # LARGEST part. Cross-territory strays were already re-homed by the Phase
            # B.6 de-island pass, so a residual small part is an adjacent geometry
            # sliver holding at most a building or two right at the edge — those stay
            # tagged to this cluster (reported in the ADDR_ID "outside" count).
            if geom is not None and not geom.isEmpty() and geom.isMultipart():
                filtered = self._parts_containing_members(geom, member_pts)
                if filtered is not None and not filtered.isEmpty():
                    geom = filtered
                if geom.isMultipart():
                    parts = [q for q in geom.asGeometryCollection()
                             if q is not None and not q.isEmpty()]
                    if len(parts) > 1:
                        largest = max(parts, key=lambda q: q.area())
                        geom = self._safe_polygon(QgsGeometry(largest))
            geom_of_cluster[ci] = geom
            review_of[ci] = review
            try:
                emitted_geom = emitted_geom.combine(geom)
            except Exception as exc:
                feedback.pushWarning(
                    f"Growth: could not union polygon for cluster {ci} into overlap guard ({exc})."
                )
            try:
                feedback.setProgress(60 + int(35 * (n_done + 1) / max(1, len(emit_order))))
            except Exception:
                pass

        # ---- Phase D: COVERAGE GUARANTEE — leave no premise outside a polygon ----
        # Every house must sit inside a polygon. After the single-boundary trim,
        # the visual gap and the barrier cut, a few members can land just outside
        # their own polygon. Loop until none are left outside:
        #   (1) if the point already sits inside another polygon → retag it there;
        #   (2) else attach it to the nearest polygon WITH AVAILABILITY (capacity
        #       within the cap + service radius) and grow that polygon to cover it;
        #   (3) if no neighbouring polygon is reachable at all → the point keeps its
        #       own cluster as a fresh serving area (flagged REVIEW).
        # A neighbour taken over its cap purely to guarantee coverage is REVIEW-flagged.
        def _availability(oci, fid):
            if clusters[oci]["hh"] + hh_of[fid] > max_hh:
                return False
            n = max(1, len(clusters[oci]["members"]))
            ncx, ncy = clusters[oci]["sx"] / n, clusters[oci]["sy"] / n
            return math.hypot(pts[fid].x() - ncx, pts[fid].y() - ncy) <= service_radius

        rehomed = 0
        for _cov_round in range(6):
            engines = {}
            for oci, g in geom_of_cluster.items():
                if g is not None and not g.isEmpty() and clusters[oci]["members"]:
                    e = QgsGeometry.createGeometryEngine(g.constGet())
                    e.prepareGeometry()
                    engines[oci] = e
            uncovered = []
            for fid in list(assigned):
                ci = _resolve(assigned[fid])
                if ci not in geom_of_cluster:
                    continue
                _pg_tmp = QgsGeometry.fromPointXY(pts[fid])
                pgc = _pg_tmp.constGet()
                if ci in engines and engines[ci].intersects(pgc):
                    continue                                  # inside its own polygon
                inside_other = next((oci for oci, e in engines.items()
                                     if oci != ci and e.intersects(pgc)), None)
                uncovered.append((fid, ci, inside_other))
            if not uncovered:
                break
            changed = 0
            for fid, ci, inside_other in uncovered:
                pg = QgsGeometry.fromPointXY(pts[fid])
                if inside_other is not None:
                    target = inside_other                     # already covered here → retag
                else:
                    cands = sorted(
                        (g.distance(pg), oci) for oci, g in geom_of_cluster.items()
                        if g is not None and not g.isEmpty() and clusters[oci]["members"])
                    target = None
                    for _avail_only in (True, False):         # prefer an available absorber
                        for d, oci in cands:
                            if d > neigh_dist:
                                break
                            if _avail_only and not _availability(oci, fid):
                                continue
                            target = oci
                            break
                        if target is not None:
                            break
                    if target is None:
                        target = ci                           # no reachable neighbour → own region
                        clusters[ci]["review"] = True
                        review_of[ci] = True
                    # grow the target polygon to cover the point with a SOLID
                    # connector (footprint + straight corridor to the nearest edge)
                    # so the result stays a single connected piece.
                    tg = geom_of_cluster.get(target)
                    pieces = [self._square(pts[fid], max(ownsq_r, 4.0))]
                    if tg is not None and not tg.isEmpty():
                        pieces.append(QgsGeometry(tg))
                        try:
                            near = tg.nearestPoint(pg)
                            if near is not None and not near.isEmpty():
                                seg = QgsGeometry.fromPolylineXY([pts[fid], near.asPoint()])
                                pieces.append(self._straight_buffer(seg, max(2.0, margin * 0.4)))
                        except Exception:
                            pass
                    newg = self._safe_polygon(QgsGeometry.unaryUnion(
                        [x for x in pieces if x is not None and not x.isEmpty()]))
                    if newg is None or newg.isEmpty():
                        continue
                    geom_of_cluster[target] = newg
                    # keep polygons disjoint: carve the (small) extension out of any
                    # neighbour it now overlaps — a neighbour only loses a sliver.
                    for _oc in list(geom_of_cluster):
                        if _oc == target:
                            continue
                        _gg = geom_of_cluster[_oc]
                        if _gg is None or _gg.isEmpty() or not clusters[_oc]["members"]:
                            continue
                        if not _gg.intersects(newg):
                            continue
                        _cut = _gg.difference(newg)
                        if _cut is not None and not _cut.isEmpty():
                            _cut = self._parts_containing_members(
                                _cut, [pts[m] for m in clusters[_oc]["members"]])
                            if _cut is not None and not _cut.isEmpty():
                                geom_of_cluster[_oc] = self._safe_polygon(_cut)
                if target != ci:
                    if fid in clusters[ci]["members"]:
                        clusters[ci]["members"].remove(fid)
                        clusters[ci]["hh"] -= hh_of[fid]
                        clusters[ci]["sx"] -= pts[fid].x()
                        clusters[ci]["sy"] -= pts[fid].y()
                    clusters[target]["members"].append(fid)
                    clusters[target]["hh"] += hh_of[fid]
                    clusters[target]["sx"] += pts[fid].x()
                    clusters[target]["sy"] += pts[fid].y()
                    assigned[fid] = target
                    if clusters[target]["hh"] > max_hh:
                        clusters[target]["review"] = True
                        review_of[target] = True
                changed += 1
                rehomed += 1
            if not changed:
                break
        if rehomed:
            feedback.pushInfo(
                f"Growth: coverage pass re-homed {rehomed} premise(s) so every house sits inside a polygon."
            )

        # ---- EMIT the finalized polygons ----
        for ci in emit_order:
            geom = geom_of_cluster.get(ci)
            if geom is None or geom.isEmpty() or not clusters[ci]["members"]:
                continue
            cl = clusters[ci]
            member_pts = [pts[m] for m in cl["members"]]
            review = review_of.get(ci, False) or len(cl["members"]) < 3 or cl["hh"] < min_hh

            out = QgsFeature(fields_out)
            out.setGeometry(geom)
            poly_id = f"POLY{serial:05d}"
            serial += 1
            poly_id_of_cluster[ci] = poly_id

            cps = geom.pointOnSurface()
            cpt = cps.asPoint() if (cps and not cps.isEmpty()) else member_pts[0]
            plan = plan_splitters(cl["hh"])
            counts = plan["counts"]
            area = geom.area()
            density = round(cl["hh"] / (area / 10000.0), 2) if area > 0 else 0.0
            no_access = 0
            if has_access is not None:
                no_access = sum(1 for m in cl["members"] if not has_access.get(m, True))

            for k, v in [
                ("stg", "stage2_builder"),
                (COMMON_FIELDS.STAGE, "polygon"),
                ("POLYGON_ID", poly_id),
                (COMMON_FIELDS.SRC_ID, poly_id),
                ("area_m2", area),
                ("HH", cl["hh"]),
                ("SUM_HOMES", cl["hh"]),
                ("SUM_OBJECT", len(cl["members"])),
                ("CENTR_X", float(cpt.x())),
                ("CENTR_Y", float(cpt.y())),
                ("DENSITY", density),
                ("SPLIT_SIZE", f"1:{plan['primary']}" if plan["primary"] else "-"),
                ("SPLIT_CNT", plan["total"]),
                ("SPLIT_UTIL", plan["util"]),
                ("SPLIT_OK", plan["ok"]),
                ("SPL_PLAN", plan["label"]),
                ("SPL_PORTS", plan["ports"]),
                ("SPL_4", counts.get(4, 0)),
                ("SPL_8", counts.get(8, 0)),
                ("SPL_16", counts.get(16, 0)),
                ("SPL_32", counts.get(32, 0)),
                ("SPL_64", counts.get(64, 0)),
                ("NO_ACCESS", no_access),
                ("REVIEW", 1 if review else 0),
            ]:
                if k in names:
                    out[k] = v
            sink.addFeature(out, QgsFeatureSink.FastInsert)

        hh_total = sum(clusters[ci]["hh"] for ci in emit_order)
        in_band = sum(1 for ci in emit_order
                      if min_hh <= clusters[ci]["hh"] <= max_hh
                      and len(clusters[ci]["members"]) >= 3)
        floor = int(math.ceil(hh_total / float(max_hh))) if max_hh > 0 else 0
        feedback.pushInfo(
            f"Growth: {serial - 1} polygons from {total_pts} buildings / {hh_total} homes; "
            f"{in_band} within the {min_hh}–{max_hh} homes band, "
            f"{len(emit_order) - in_band} flagged REVIEW."
        )
        feedback.pushInfo(
            f"Growth: polygon count {serial - 1} vs theoretical minimum {floor} "
            f"(= ceil({hh_total} homes / {max_hh} cap); raise MAX_HH_PER_POLYGON to merge further)."
        )

        # ---- ADDR_ID validation: one building → exactly one polygon (attribute
        # membership) AND the building sits geographically inside that polygon. ----
        polys_per_addr = defaultdict(set)
        outside = 0
        for fid, ci in assigned.items():
            rci = _resolve(ci)
            pid = poly_id_of_cluster.get(rci)
            if not pid:
                continue
            polys_per_addr[addr_of.get(fid)].add(pid)
            g = geom_of_cluster.get(rci)
            if g is not None and not g.isEmpty() and not g.intersects(QgsGeometry.fromPointXY(pts[fid])):
                outside += 1
        multi = {a for a, pids in polys_per_addr.items() if len(pids) > 1}
        if multi:
            feedback.pushWarning(
                f"Growth: {len(multi)} ADDR_ID(s) map to more than one polygon — unexpected, please report."
            )
        else:
            feedback.pushInfo(
                f"Growth: ADDR_ID check OK — each of {len(polys_per_addr)} buildings belongs to exactly one polygon."
            )
        if outside:
            feedback.pushWarning(
                f"Growth: {outside} building(s) fall outside their own polygon boundary "
                "(edge margin / barrier cut) — attribute membership is still exact."
            )

        # ---- best-effort POLYGON_ID write-back onto the INPUT layer ----
        try:
            in_lyr = self.parameterAsVectorLayer(p, self.P_INPUT, context)
            if in_lyr is not None and in_lyr.isValid():
                pr = in_lyr.dataProvider()
                fidx = in_lyr.fields().indexOf("POLYGON_ID")
                if fidx == -1:
                    pr.addAttributes([QgsField("POLYGON_ID", QMetaType.Type.QString)])
                    in_lyr.updateFields()
                    fidx = in_lyr.fields().indexOf("POLYGON_ID")
                if fidx != -1:
                    changes = {}
                    for fid, ci in assigned.items():
                        pid = poly_id_of_cluster.get(_resolve(ci))
                        if pid:
                            changes[fid] = {fidx: pid}
                    if changes:
                        pr.changeAttributeValues(changes)
                        feedback.pushInfo(f"Growth: POLYGON_ID written back onto {len(changes)} input premises.")
            else:
                feedback.pushInfo("Growth: INPUT is not an editable layer — POLYGON_ID write-back skipped (stage 03 will sync).")
        except Exception as exc:
            feedback.pushWarning(f"Growth: POLYGON_ID write-back failed: {exc}")
