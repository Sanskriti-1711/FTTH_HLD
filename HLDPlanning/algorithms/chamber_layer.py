# -*- coding: utf-8 -*-
"""
Chamber Layer — generate planned civil chambers for the HLD.

Implements the 'Simple Rule for HLD' from HLD_attr.docx:

    Manhole   → Feeder network            (large duct banks, backbone access)
    Chamber   → Feeder + Distribution     (splicing, branching, cable pulling)
    Handhole  → Distribution + Garden     (FAT access, garden cable connections)

Placement rules:
  - Chamber   : one at every PDP (feeder and distribution meet there)
  - Manhole   : at every used tangent drill crossing + every feeder-duct
                junction vertex (>= 2 feeder ducts within 1.5 m)
  - Handhole  : at every distribution-duct junction vertex
                (>= 2 distribution ducts within 1.5 m)

Candidates within 2 m are collapsed (Chamber > Manhole > Handhole).
"""
from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterFeatureSink,
    QgsProcessingException, QgsWkbTypes, QgsFeature, QgsFeatureSink,
    QgsGeometry, QgsPointXY, QgsRectangle, QgsSpatialIndex,
)

from ..utils.fields import COMMON_FIELDS, THIN_PROFILES, build_fields
from ..utils.brownfield import InfraStatus, VerifyStatus


class ChamberLayerAlgorithm(QgsProcessingAlgorithm):

    P_FEEDER_DUCTS = "INPUT_FEEDER_DUCTS"
    P_DIST_DUCTS = "INPUT_DIST_DUCTS"
    P_PDP = "INPUT_PDP"
    P_TANGENTS = "INPUT_TANGENT_CROSSINGS"
    P_TRENCHES = "INPUT_TRENCHES"
    OUT_CHAMBERS = "OUT_CHAMBERS"

    JUNCTION_RADIUS_M = 1.5     # how close two DISTINCT ducts must pass to count as a junction
    JUNCTION_SPACING_M = 80.0   # min spacing between junction-derived Manholes/Handholes
    HANDHOLE_SPACING_M = 100.0  # wider spacing for handholes (Distribution-level)
    CHAMBER_SPACING_M = 2.0     # collapse chamber candidates closer than this
    CONN_RADIUS_M = 3.0         # ducts within this radius count as 'connected'
    TRENCH_JOIN_M = 3.0         # parent trench join tolerance

    def tr(self, s):
        return QCoreApplication.translate("ChamberLayerAlgorithm", s)

    def name(self):
        return "07_chamber_layer"

    def displayName(self):
        return self.tr("Generate Chamber / Manhole / Handhole Layer")

    def group(self):
        return self.tr("07 Civil")

    def groupId(self):
        return "07_civil"

    def createInstance(self):
        return ChamberLayerAlgorithm()

    def shortHelpString(self):
        return self.tr(
            "Plans civil chambers from the designed network: a Chamber at every "
            "PDP, Manholes at feeder junctions and drill crossings, Handholes at "
            "distribution junctions.  Implements the HLD_attr.docx Simple Rule "
            "(Manhole = Feeder, Chamber = Feeder+Distribution, "
            "Handhole = Distribution+Garden)."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_FEEDER_DUCTS, self.tr("Feeder Ducts [lines]"),
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_DIST_DUCTS, self.tr("Distribution Ducts [lines]"),
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_PDP, self.tr("PDP points"),
            [QgsProcessing.TypeVectorPoint], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_TANGENTS, self.tr("Used drill crossings [points] (optional)"),
            [QgsProcessing.TypeVectorPoint], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_TRENCHES, self.tr("Final Trenches [lines] (optional; for parent id)"),
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_CHAMBERS, self.tr("Chambers (planned)"),
            optional=True, createByDefault=True,
        ))

    # ── helpers ──────────────────────────────────────────────────────────

    def _layer(self, params, key, context):
        try:
            return self.parameterAsVectorLayer(params, key, context)
        except Exception:
            return None

    @staticmethod
    def _geom_vertices(g):
        """Yield (x, y) for every vertex of a geometry (single or multi line).

        NOTE: `g.constGet()` returns the abstract QgsMultiLineString /
        QgsLineString — those do NOT have an `isMultipart()` method (that
        lives on QgsGeometry).  We must test the WKB type instead.
        """
        if g is None or g.isEmpty():
            return
        parts = g.constGet()
        try:
            if QgsWkbTypes.isMultiType(parts.wkbType()):
                for part in parts.parts():
                    for i in range(part.numPoints()):
                        p = part.pointN(i)
                        yield p.x(), p.y()
            else:
                for i in range(parts.numPoints()):
                    p = parts.pointN(i)
                    yield p.x(), p.y()
        except Exception:
            return

    def _line_vertices(self, lyr):
        """Yield (x, y) for every vertex of every line feature."""
        if lyr is None:
            return
        for f in lyr.getFeatures():
            for x, y in self._geom_vertices(f.geometry()):
                yield x, y

    def _junction_points(self, lyr, radius):
        """Points where >= 2 DISTINCT ducts pass within `radius`.

        Every duct vertex is a seed.  A seed is a junction only when at least
        two *different* duct features pass within `radius` of it — so a single
        duct's own dense vertices never count.  Returns (x, y, weight) where
        weight = number of distinct ducts at that point (denser = stronger).
        """
        if lyr is None:
            return []
        index = QgsSpatialIndex()
        geoms = {}
        verts = []
        for f in lyr.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            fid = f.id()
            # NOTE: addGeometry() was removed from QgsSpatialIndex in modern
            # QGIS — the only supported way is addFeature(feature).
            index.addFeature(f)
            geoms[fid] = g
            for x, y in self._geom_vertices(g):
                verts.append((x, y, fid))

        r2 = radius * radius
        out = []
        seen = set()
        for x, y, fid in verts:
            rect = QgsRectangle(x - radius, y - radius, x + radius, y + radius)
            hits = index.intersects(rect)
            distinct = set()
            qpt = QgsPointXY(x, y)
            for hfid in hits:
                g = geoms.get(hfid)
                if g is None:
                    continue
                try:
                    sqd = g.closestSegmentWithContext(qpt)[0]
                except Exception:
                    continue
                if sqd <= r2:
                    distinct.add(hfid)
            if len(distinct) >= 2:
                key = (round(x, 1), round(y, 1))
                if key not in seen:
                    seen.add(key)
                    out.append((x, y, len(distinct)))
        return out

    def _place_structures(self, candidates):
        """Greedy placement: highest priority first, densest junctions first.

        A candidate is kept unless a previously kept structure of any type sits
        within its type-specific spacing (2 m for chambers, JUNCTION_SPACING
        for junction-derived Manholes/Handholes).  Returns the kept list.
        """
        spacing = {
            "Chamber": self.CHAMBER_SPACING_M,
            "Manhole": self.JUNCTION_SPACING_M,
            "Handhole": self.HANDHOLE_SPACING_M,
        }
        ordered = sorted(candidates, key=lambda c: (-c[2], -c[5]))
        kept = []
        index = QgsSpatialIndex()
        for x, y, prio, ctype, equip, weight in ordered:
            sp = spacing.get(ctype, self.CHAMBER_SPACING_M)
            rect = QgsRectangle(x - sp, y - sp, x + sp, y + sp)
            if index.intersects(rect):
                continue
            fid = len(kept)
            kept.append((x, y, prio, ctype, equip, weight))
            feat = QgsFeature()
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
            feat.setId(fid)
            index.addFeature(feat)
        return kept

    def _count_ducts_near(self, index, feature_geoms, x, y, radius):
        """Count duct features whose geometry passes within radius of (x, y)."""
        pt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
        buf = pt.buffer(radius, 8)
        ids = index.intersects(buf.boundingBox())
        n = 0
        for fid in ids:
            g = feature_geoms.get(fid)
            if g is not None and g.intersects(buf):
                n += 1
        return n

    def _nearest_trench(self, trench_lyr, x, y, tol):
        if trench_lyr is None:
            return ""
        pt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
        best = ""
        best_d = tol
        for f in trench_lyr.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            d = g.distance(pt)
            if d <= best_d:
                best_d = d
                for cand in ("id", "SRC_ID", "POLYGON_ID"):
                    if f.fields().indexOf(cand) >= 0:
                        v = f[cand]
                        if v not in (None, ""):
                            best = str(v)
                            break
        return best

    # ── main ─────────────────────────────────────────────────────────────

    def processAlgorithm(self, params, context, feedback):
        feeder = self._layer(params, self.P_FEEDER_DUCTS, context)
        dist = self._layer(params, self.P_DIST_DUCTS, context)
        pdp_lyr = self._layer(params, self.P_PDP, context)
        tangents = self._layer(params, self.P_TANGENTS, context)
        trenches = self._layer(params, self.P_TRENCHES, context)

        crs = None
        for lyr in (feeder, dist, pdp_lyr, tangents, trenches):
            if lyr is not None and lyr.isValid():
                crs = lyr.crs()
                break
        if crs is None:
            raise QgsProcessingException(
                self.tr("At least one input layer is required."))

        # ── gather raw candidates ────────────────────────────────────────
        candidates = []  # (x, y, priority, type)

        # Chamber at every PDP (feeder + distribution meet)
        pdp_ids = {}
        if pdp_lyr is not None:
            for f in pdp_lyr.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                try:
                    pt = g.asPoint()
                except Exception:
                    continue
                pid = ""
                if f.fields().indexOf("PDP_ID") >= 0:
                    pid = str(f["PDP_ID"] or "")
                candidates.append((pt.x(), pt.y(), 3, "Chamber", pid, 999))

        # Manhole at used drill crossings (feeder access)
        # Filter: skip crossings within 100m of an already-placed candidate
        DRILL_DEDUP_M = 100.0
        seen_drill = []
        if tangents is not None:
            for f in tangents.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                try:
                    pt = g.asPoint()
                except Exception:
                    continue
                px, py = pt.x(), pt.y()
                too_close = False
                for sx, sy in seen_drill:
                    if ((px - sx) ** 2 + (py - sy) ** 2) ** 0.5 < DRILL_DEDUP_M:
                        too_close = True
                        break
                if not too_close:
                    candidates.append((px, py, 2, "Manhole", "", 999))
                    seen_drill.append((px, py))

        # Manhole at feeder-duct junctions (≥3 distinct ducts required)
        for x, y, w in self._junction_points(feeder, self.JUNCTION_RADIUS_M):
            if w >= 3:
                candidates.append((x, y, 2, "Manhole", "", w))

        # Handhole at distribution-duct junctions (>= 3 distinct ducts required)
        for x, y, w in self._junction_points(dist, self.JUNCTION_RADIUS_M):
            if w >= 3:
                candidates.append((x, y, 1, "Handhole", "", w))

        # ── collapse duplicates (highest priority, densest first) ────────
        kept = self._place_structures(candidates)

        # ── build spatial index of all ducts for CONN_DUCTS ─────────────
        duct_index = QgsSpatialIndex()
        duct_geoms = {}
        for lyr in (feeder, dist):
            if lyr is None:
                continue
            for f in lyr.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                fid = f.id()
                duct_index.addFeature(f)
                duct_geoms[fid] = g

        # ── output ───────────────────────────────────────────────────────
        out_fields = build_fields(THIN_PROFILES["CHAMBER"])
        sink, out_id = self.parameterAsSink(
            params, self.OUT_CHAMBERS, context,
            out_fields, QgsWkbTypes.Point, crs,
        )

        counters = {"Manhole": 0, "Chamber": 0, "Handhole": 0}
        written = 0
        for x, y, prio, ctype, equip, _w in kept:
            counters[ctype] += 1
            struct_id = f"{ctype[0:2].upper()}-{counters[ctype]:04d}"
            conn = self._count_ducts_near(duct_index, duct_geoms, x, y, self.CONN_RADIUS_M)
            if conn <= 2:
                size = "Small (600×450 mm)"
            elif conn <= 4:
                size = "Medium (1000×750 mm)"
            else:
                size = "Large (1500×1200 mm)"

            feat = QgsFeature(out_fields)
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
            feat[COMMON_FIELDS.STRUCT_ID] = struct_id
            feat[COMMON_FIELDS.CHAMBER_TYPE] = ctype
            feat[COMMON_FIELDS.PARENT_TRENCH] = self._nearest_trench(
                trenches, x, y, self.TRENCH_JOIN_M)
            feat[COMMON_FIELDS.CONN_DUCTS] = conn
            feat[COMMON_FIELDS.SIZE] = size
            feat[COMMON_FIELDS.EQUIPMENT] = equip or ""
            feat[COMMON_FIELDS.CAPACITY_USED] = 0
            feat[COMMON_FIELDS.CAPACITY_TOTAL] = conn
            feat[COMMON_FIELDS.INFRA_STATUS] = InfraStatus.PROPOSED
            feat[COMMON_FIELDS.VERIFY_STATUS] = VerifyStatus.VERIFIED
            feat[COMMON_FIELDS.STAGE] = "Civil"
            if sink is not None:
                sink.addFeature(feat, QgsFeatureSink.FastInsert)
                written += 1

        feedback.pushInfo(self.tr(
            f"Chamber layer: {written} planned structures "
            f"(Manhole: {counters['Manhole']}, Chamber: {counters['Chamber']}, "
            f"Handhole: {counters['Handhole']})."))

        result = {}
        if out_id:
            result[self.OUT_CHAMBERS] = out_id
        return result
