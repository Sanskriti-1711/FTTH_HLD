# -*- coding: utf-8 -*-
"""
Aerial Drop Layer — route aerial drop trenches from poles to premises
where the HLD trench evaluation flagged aerial_required=True.

This is Stage 08b of the one-click pipeline.  It runs after the Pole Layer
so that poles exist as anchor points.  Only premises that were evaluated
as infeasible for buried drop (distance, barrier, terrain, cost) are
connected aerially; all other premises are ignored here.
"""

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterNumber,
    QgsProcessingException,
    QgsFields,
    QgsField,
    QgsWkbTypes,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsSpatialIndex,
    QgsCoordinateTransform,
)
from qgis.PyQt.QtCore import QMetaType

from ..utils.fields import COMMON_FIELDS, build_fields, first_field_case_insensitive


class AerialDropLayerAlgorithm(QgsProcessingAlgorithm):

    # ── Parameter keys ────────────────────────────────────────────────────────

    P_PREMISES = "INPUT_PREMISES"
    P_POLES = "INPUT_POLES"
    P_AERIAL_ZONES = "INPUT_AERIAL_ZONES"
    P_ROADS = "INPUT_ROADS"
    P_BF_POLES = "INPUT_BF_POLES"
    P_SPACING = "POLE_SPACING_M"

    OUT_AERIAL_TRENCH = "OUT_AERIAL_TRENCH"
    OUT_AERIAL_CABLE = "OUT_AERIAL_CABLE"

    # ── Constants ─────────────────────────────────────────────────────────────

    MAX_DROP_DISTANCE_M = 70.0       # max aerial drop span
    MAX_POLE_SEARCH_M = 100.0        # search radius for nearest pole
    DEFAULT_SPACING = 50.0           # pole spacing fallback

    # ── QGIS Processing boilerplate ──────────────────────────────────────────

    def tr(self, s):
        return QCoreApplication.translate("AerialDropLayerAlgorithm", s)

    def name(self):
        return "09_aerial_drop_layer"

    def displayName(self):
        return self.tr("Aerial Drop Layer (pole-to-premise)")

    def group(self):
        return self.tr("08 Civil")

    def groupId(self):
        return "08_civil"

    def createInstance(self):
        return AerialDropLayerAlgorithm()

    def shortHelpString(self):
        return self.tr(
            "Routes aerial drop trenches from poles to premises that were "
            "flagged as aerial_required=True by the trench evaluation stage. "
            "Produces Aerial_Drop_Trenches and Aerial_Cable layers.\n\n"
            "Premises without aerial_required are ignored.  Only premises "
            "inside aerial zones (or explicitly flagged) are connected."
        )

    # ── Parameter definitions ─────────────────────────────────────────────────

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_PREMISES,
            self.tr("Premises (objects with aerial_required flag)"),
            [QgsProcessing.TypeVectorPoint],
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_POLES,
            self.tr("Poles (from Stage 08) [points]"),
            [QgsProcessing.TypeVectorPoint],
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_AERIAL_ZONES,
            self.tr("Aerial Zones [polygons] (blank = no restriction)"),
            [QgsProcessing.TypeVectorPolygon],
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_ROADS,
            self.tr("Roads [lines] (for snapping)"),
            [QgsProcessing.TypeVectorLine],
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BF_POLES,
            self.tr("Brownfield Poles [points] (existing poles)"),
            [QgsProcessing.TypeVectorPoint],
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_SPACING,
            self.tr("Pole spacing [m]"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=self.DEFAULT_SPACING,
            minValue=10.0,
        ))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_AERIAL_TRENCH,
            self.tr("Aerial Drop Trenches"),
            QgsProcessing.TypeVectorLine,
            optional=True,
            createByDefault=True,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_AERIAL_CABLE,
            self.tr("Aerial Cable"),
            QgsProcessing.TypeVectorLine,
            optional=True,
            createByDefault=True,
        ))

    # ── Main execution ────────────────────────────────────────────────────────

    def processAlgorithm(self, parameters, context, feedback):
        premises = self.parameterAsVectorLayer(parameters, self.P_PREMISES, context)
        poles = self.parameterAsVectorLayer(parameters, self.P_POLES, context)
        zones = self.parameterAsVectorLayer(parameters, self.P_AERIAL_ZONES, context)
        roads = self.parameterAsVectorLayer(parameters, self.P_ROADS, context)
        bf_poles = self.parameterAsVectorLayer(parameters, self.P_BF_POLES, context)
        spacing = self.parameterAsDouble(parameters, self.P_SPACING, context)

        if premises is None or not premises.isValid():
            raise QgsProcessingException(self.tr("Premises layer is required."))

        crs = premises.crs()
        if crs is None or not crs.isValid():
            crs = poles.crs() if poles and poles.isValid() else None
        if crs is None:
            raise QgsProcessingException(self.tr("No valid CRS found on input layers."))

        # ── Output schemas ─────────────────────────────────────────────────────

        trench_fields = build_fields([
            COMMON_FIELDS.AERIAL_TRENCH_ID,
            COMMON_FIELDS.POLE_ID,
            COMMON_FIELDS.FROM_POLE,
            COMMON_FIELDS.TO_PREMISE,
            COMMON_FIELDS.TRENCH_TYPE,
            COMMON_FIELDS.CONSTRUCTION_METHOD,
            COMMON_FIELDS.CABLE_TYPE,
            COMMON_FIELDS.FIBER_COUNT,
            COMMON_FIELDS.LENGTH_M,
            COMMON_FIELDS.POLE_SPACING_M,
            COMMON_FIELDS.CROSSINGS,
            COMMON_FIELDS.PERMIT_REQUIRED,
            COMMON_FIELDS.AERIAL_REASON,
            COMMON_FIELDS.INFRA_STATUS,
            COMMON_FIELDS.VERIFY_STATUS,
            COMMON_FIELDS.STAGE,
        ])
        cable_fields = build_fields([
            COMMON_FIELDS.CABLE_TYPE,
            COMMON_FIELDS.FIBER_COUNT,
            COMMON_FIELDS.LENGTH_M,
            COMMON_FIELDS.SOURCE_NODE,
            COMMON_FIELDS.UTIL_PCT,
            COMMON_FIELDS.INFRA_STATUS,
            COMMON_FIELDS.VERIFY_STATUS,
            COMMON_FIELDS.STAGE,
        ])

        sink_t, id_t = self.parameterAsSink(
            parameters, self.OUT_AERIAL_TRENCH, context,
            trench_fields, QgsWkbTypes.LineString, crs,
        )
        sink_c, id_c = self.parameterAsSink(
            parameters, self.OUT_AERIAL_CABLE, context,
            cable_fields, QgsWkbTypes.LineString, crs,
        )

        if sink_t is None or sink_c is None:
            raise QgsProcessingException(self.tr("Failed to create output sinks."))

        # ── Build spatial indexes ───────────────────────────────────────────────

        pole_idx = QgsSpatialIndex(poles.getFeatures()) if poles and poles.isValid() else QgsSpatialIndex()
        bf_pole_idx = QgsSpatialIndex(bf_poles.getFeatures()) if bf_poles and bf_poles.isValid() else QgsSpatialIndex()

        # Road index for snapping aerial routes to road centerlines
        road_idx = QgsSpatialIndex(roads.getFeatures()) if roads and roads.isValid() else None
        road_features = {}
        if road_idx:
            for fid in road_idx.intersects(roads.extent()):
                feat = roads.getFeature(fid)
                if feat.isValid():
                    road_features[fid] = feat

        # Aerial zone polygons (point-in-polygon test)
        zone_geoms = []
        if zones and zones.isValid() and zones.featureCount() > 0:
            for f in zones.getFeatures():
                g = f.geometry()
                if g and not g.isEmpty():
                    zone_geoms.append(g)

        # ── Helper: find nearest pole ───────────────────────────────────────────

        def _nearest_pole(pt: QgsPointXY):
            """Return (pole_id, pole_geom, distance_m) or None."""
            candidates = []
            for fid in pole_idx.nearestNeighbor(pt, 5):
                feat = poles.getFeature(fid)
                if not feat.isValid():
                    continue
                g = feat.geometry()
                if not g or g.isEmpty():
                    continue
                d = float(g.distance(QgsGeometry.fromPointXY(pt)))
                candidates.append((d, feat, g))

            for fid in bf_pole_idx.nearestNeighbor(pt, 5):
                feat = bf_poles.getFeature(fid)
                if not feat.isValid():
                    continue
                g = feat.geometry()
                if not g or g.isEmpty():
                    continue
                d = float(g.distance(QgsGeometry.fromPointXY(pt)))
                candidates.append((d, feat, g))

            if not candidates:
                return None
            candidates.sort(key=lambda x: x[0])
            d, feat, g = candidates[0]
            pid = str(feat[COMMON_FIELDS.POLE_ID] or feat["POLE_ID"] or feat["pole_id"] or "")
            return pid, g, d

        def _approx_meters(p1, p2):
            """Equirectangular approximation in metres."""
            dx = (p2.x() - p1.x()) * 111_320.0 * max(0.1, math.cos(math.radians((p1.y() + p2.y()) / 2)))
            dy = (p2.y() - p1.y()) * 111_320.0
            return (dx * dx + dy * dy) ** 0.5

        def _point_in_zone(pt):
            if not zone_geoms:
                return True  # no zones = unrestricted
            for zg in zone_geoms:
                if zg.contains(pt):
                    return True
            return False

        def _snap_to_road(pt, max_snap_m=15.0):
            """Snap point to nearest road within max_snap_m, return snapped point."""
            if road_idx is None:
                return pt
            best_d = float("inf")
            best_pt = pt
            for fid in road_idx.nearestNeighbor(pt, 3):
                feat = road_features.get(fid)
                if not feat or not feat.isValid():
                    continue
                g = feat.geometry()
                if not g or g.isEmpty():
                    continue
                np = g.nearestPoint(QgsGeometry.fromPointXY(pt))
                if np.isMultipart():
                    np = np.asMultiPoint()[0]
                d = float(np.distance(QgsGeometry.fromPointXY(pt)))
                if d < best_d and d <= max_snap_m:
                    best_d = d
                    best_pt = QgsPointXY(np)
            return best_pt

        # ── Filter premises: aerial_required=True AND in aerial zone ────────────

        aerial_premises = []
        skipped_no_flag = 0
        skipped_no_zone = 0
        skipped_no_pole = 0

        aerial_field = first_field_case_insensitive(
            premises, ["aerial_required", "AERIAL_REQUIRED", "aerial", "AERIAL"]
        )

        for f in premises.getFeatures():
            g = f.geometry()
            if not g or g.isEmpty():
                continue
            pt = g.centroid().asPoint() if g.wkbType() == QgsWkbTypes.Polygon else _point_of(g)

            # Check aerial_required flag
            is_aerial = False
            if aerial_field:
                val = f[aerial_field]
                is_aerial = str(val).lower() in ("true", "1", "yes", "y") if val is not None else False
            else:
                # No flag field — infer from distance to network (future: use
                # the evaluation function from trench_layer.py).  For now,
                # require explicit flag.
                feedback.pushWarning(
                    self.tr("No aerial_required field found on premises layer — "
                            "only explicitly flagged premises will get aerial drops.")
                )
                continue

            if not is_aerial:
                skipped_no_flag += 1
                continue

            if not _point_in_zone(QgsGeometry.fromPointXY(pt)):
                skipped_no_zone += 1
                continue

            nearest = _nearest_pole(pt)
            if nearest is None:
                skipped_no_pole += 1
                continue

            pole_id, pole_geom, pole_dist = nearest
            aerial_premises.append((f, pt, pole_id, pole_geom, pole_dist))

        feedback.pushInfo(self.tr(
            f"Aerial drop planner: {len(aerial_premises)} premises flagged, "
            f"{skipped_no_flag} not flagged, {skipped_no_zone} outside zones, "
            f"{skipped_no_pole} no pole within {self.MAX_POLE_SEARCH_M}m."
        ))

        if not aerial_premises:
            # Return empty outputs
            return {
                self.OUT_AERIAL_TRENCH: id_t,
                self.OUT_AERIAL_CABLE: id_c,
            }

        # ── Route aerial drops ──────────────────────────────────────────────────

        import math
        counter = {"trench": 0, "cable": 0, "skipped": 0}

        for f, pt, pole_id, pole_geom, pole_dist in aerial_premises:
            if pole_dist > self.MAX_POLE_SEARCH_M:
                counter["skipped"] += 1
                continue

            pole_pt = pole_geom.centroid().asPoint() if pole_geom.wkbType() == QgsWkbTypes.PointGeometry else pole_geom.asPoint()

            # Snap both endpoints to roads for practical routing
            snapped_pole = _snap_to_road(pole_pt, max_snap_m=20.0)
            snapped_premise = _snap_to_road(pt, max_snap_m=20.0)

            # Build path: pole → premise (straight if both snapped, otherwise
            # fall back to direct line)
            if snapped_pole.distance(QgsGeometry.fromPointXY(pole_pt)) < 0.1 and \
               snapped_premise.distance(QgsGeometry.fromPointXY(pt)) < 0.1:
                path = [pole_pt, pt]
            else:
                path = [snapped_pole, snapped_premise]

            length_m = _approx_meters(path[0], path[-1])
            if length_m > self.MAX_DROP_DISTANCE_M * 2:
                feedback.pushWarning(
                    self.tr(f"Aerial drop {length_m:.0f}m exceeds 2x max span "
                            f"({self.MAX_DROP_DISTANCE_M}m) — skipping.")
                )
                counter["skipped"] += 1
                continue

            addr_val = str(f["ADDR_ID"] if "ADDR_ID" in f.fields().names() else
                          f["addr_id"] if "addr_id" in f.fields().names() else
                          f.id())

            # Common properties
            trench_props = {
                COMMON_FIELDS.AERIAL_TRENCH_ID: f"AT-{counter['trench'] + 1:04d}",
                COMMON_FIELDS.POLE_ID: str(pole_id or ""),
                COMMON_FIELDS.FROM_POLE: str(pole_id or ""),
                COMMON_FIELDS.TO_PREMISE: addr_val,
                COMMON_FIELDS.TRENCH_TYPE: "Aerial_Drop",
                COMMON_FIELDS.CONSTRUCTION_METHOD: "Overhead",
                COMMON_FIELDS.CABLE_TYPE: "Aerial",
                COMMON_FIELDS.FIBER_COUNT: 12,
                COMMON_FIELDS.LENGTH_M: round(length_m, 1),
                COMMON_FIELDS.POLE_SPACING_M: spacing,
                COMMON_FIELDS.CROSSINGS: 0,
                COMMON_FIELDS.PERMIT_REQUIRED: False,
                COMMON_FIELDS.AERIAL_REASON: "hlv_evaluation",
                COMMON_FIELDS.INFRA_STATUS: "Proposed",
                COMMON_FIELDS.VERIFY_STATUS: "Assumed",
                COMMON_FIELDS.STAGE: "HLD",
            }

            cable_props = {
                COMMON_FIELDS.CABLE_TYPE: "Aerial",
                COMMON_FIELDS.FIBER_COUNT: 12,
                COMMON_FIELDS.LENGTH_M: round(length_m, 1),
                COMMON_FIELDS.SOURCE_NODE: str(pole_id or ""),
                COMMON_FIELDS.UTIL_PCT: 100.0,
                COMMON_FIELDS.INFRA_STATUS: "Proposed",
                COMMON_FIELDS.VERIFY_STATUS: "Assumed",
                COMMON_FIELDS.STAGE: "HLD",
            }

            # Write aerial trench
            tf = QgsFeature(trench_fields)
            tf.setGeometry(QgsGeometry.fromPolylineXY(path))
            for k, v in trench_props.items():
                tf[k] = v
            sink_t.addFeature(tf)
            counter["trench"] += 1

            # Write aerial cable (same geometry)
            cf = QgsFeature(cable_fields)
            cf.setGeometry(QgsGeometry.fromPolylineXY(path))
            for k, v in cable_props.items():
                cf[k] = v
            sink_c.addFeature(cf)
            counter["cable"] += 1

        feedback.pushInfo(self.tr(
            f"Aerial drop planner complete: {counter['trench']} trenches, "
            f"{counter['cable']} cables, {counter['skipped']} skipped."
        ))

        return {
            self.OUT_AERIAL_TRENCH: id_t,
            self.OUT_AERIAL_CABLE: id_c,
        }


def _point_of(geom):
    """Extract a single point from a geometry."""
    if geom.wkbType() == QgsWkbTypes.Point:
        return geom.asPoint()
    if geom.wkbType() == QgsWkbTypes.MultiPoint:
        pts = geom.asMultiPoint()
        return QgsPointXY(pts[0]) if pts else None
    if geom.isMultipart():
        parts = geom.asMultiPoint()
        return QgsPointXY(parts[0]) if parts else None
    return geom.centroid().asPoint()
