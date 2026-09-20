# -*- coding: utf-8 -*-
"""
Pole Layer — generate planned aerial poles (HLD_attr.docx).

Poles are planned ONLY inside user-supplied 'aerial zones' — polygons where
underground construction is not feasible (protected areas, road/rail/river
crossings, etc.).  Rules:

  - One pole every POLE_SPACING metres along every garden trench whose
    midpoint falls inside an aerial zone.
  - Height: 7 m when the pole carries <= 2 cables, 9 m for 3-4 cables.
  - Type: 'Telecom' for garden/access lines; 'Utility' when a feeder trench
    passes within 10 m.
  - Material: Concrete.
  - Capacity: used = cable count; spare = max cables for the height - used.

If no aerial zones are supplied the algorithm produces no poles.
"""
from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSink,
    QgsProcessingException, QgsWkbTypes, QgsFeature, QgsFeatureSink,
    QgsGeometry, QgsPointXY, QgsSpatialIndex,
)

from ..utils.fields import COMMON_FIELDS, THIN_PROFILES, build_fields
from ..utils.brownfield import InfraStatus, VerifyStatus


class PoleLayerAlgorithm(QgsProcessingAlgorithm):

    P_GARDEN = "INPUT_GARDEN_TRENCHES"
    P_ZONES = "INPUT_AERIAL_ZONES"
    P_FEEDER = "INPUT_FEEDER_TRENCHES"
    P_PDP = "INPUT_PDP"
    P_LEGS = "INPUT_AERIAL_LEGS"
    P_SPACING = "POLE_SPACING_M"
    OUT_POLES = "OUT_POLES"

    # How far back from the premise the nearest pole on an aerial leg stands,
    # so the last run into the building is a real drop span.  Why: the leg's
    # far station *is* the premise, so a pole placed there is planted on the
    # customer's house and pole → premise measures 0.0 m.  In the field the
    # drop from the last pole to the building is a normal short span; a leg
    # shorter than this standoff gets a single pole at its network end and the
    # whole leg is the drop.
    DROP_STANDOFF_M = 20.0

    # A pole may not stand on a building.  Leg stations are tested against the
    # premises the aerial legs serve, so a chained leg's start (the house it
    # feeds off) never receives one.
    ON_PREMISE_M = 5.0

    CABLE_RADIUS_M = 10.0
    FEEDER_RADIUS_M = 10.0
    EQUIP_SEARCH_M = 150.0
    MAX_CABLES_7M = 2
    MAX_CABLES_9M = 4

    def tr(self, s):
        return QCoreApplication.translate("PoleLayerAlgorithm", s)

    def name(self):
        return "08_pole_layer"

    def displayName(self):
        return self.tr("Generate Pole Layer (aerial zones)")

    def group(self):
        return self.tr("07 Civil")

    def groupId(self):
        return "07_civil"

    def createInstance(self):
        return PoleLayerAlgorithm()

    def shortHelpString(self):
        return self.tr(
            "Plans aerial poles inside the supplied aerial-zone polygons "
            "(where underground construction is not feasible).  Poles are "
            "placed along garden trenches every POLE_SPACING metres.  Height "
            "7 m (<= 2 cables) or 9 m (3-4 cables); Concrete material.  "
            "Supply no aerial zones to skip pole planning entirely."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_GARDEN, self.tr("Garden Trenches [lines]"),
            [QgsProcessing.TypeVectorLine],
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_LEGS, self.tr(
                "Aerial legs [lines] (optional; classified by the trench stage)"),
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_ZONES, self.tr("Aerial Zones [polygons] — where poles are allowed"),
            [QgsProcessing.TypeVectorPolygon], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_FEEDER, self.tr("Feeder Trenches [lines] (optional; for Utility type)"),
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_PDP, self.tr("PDP points [optional]"),
            [QgsProcessing.TypeVectorPoint], optional=True,
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_SPACING, self.tr("Pole spacing [m]"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=50.0, minValue=10.0,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_POLES, self.tr("Poles (planned)"),
            optional=True, createByDefault=True,
        ))

    def _layer(self, params, key, context):
        try:
            return self.parameterAsVectorLayer(params, key, context)
        except Exception:
            return None

    def _nearest_equip(self, pdp_index, pdp_geoms, pdp_ids, x, y, tol):
        if pdp_index is None:
            return ""
        pt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
        buf = pt.buffer(tol, 8)
        ids = pdp_index.intersects(buf.boundingBox())
        best = ""
        best_d = tol
        for fid in ids:
            g = pdp_geoms.get(fid)
            if g is not None:
                d = g.distance(pt)
                if d <= best_d:
                    best_d = d
                    best = pdp_ids.get(fid, "")
        return best

    def processAlgorithm(self, params, context, feedback):
        garden = self._layer(params, self.P_GARDEN, context)
        zones = self._layer(params, self.P_ZONES, context)
        feeder = self._layer(params, self.P_FEEDER, context)
        pdp_lyr = self._layer(params, self.P_PDP, context)
        legs = self._layer(params, self.P_LEGS, context)
        spacing = self.parameterAsDouble(params, self.P_SPACING, context) or 50.0

        crs = None
        for lyr in (garden, zones, feeder, pdp_lyr, legs):
            if lyr is not None and lyr.isValid():
                crs = lyr.crs()
                break
        if crs is None:
            raise QgsProcessingException(
                self.tr("Garden trenches (and ideally aerial zones) are required."))

        out_fields = build_fields(THIN_PROFILES["POLE"])
        sink, out_id = self.parameterAsSink(
            params, self.OUT_POLES, context,
            out_fields, QgsWkbTypes.Point, crs,
        )

        if zones is None or zones.featureCount() == 0:
            feedback.pushInfo(self.tr(
                "Pole layer: no aerial zones supplied — no poles planned. "
                "Provide an 'Aerial Zones' polygon layer to enable pole design."))
            result = {}
            if out_id:
                result[self.OUT_POLES] = out_id
            return result

        # zone polygons for point-in-polygon tests
        zone_geoms = []
        for f in zones.getFeatures():
            g = f.geometry()
            if g is not None and not g.isEmpty():
                zone_geoms.append(g)
        if not zone_geoms:
            feedback.pushInfo(self.tr(
                "Pole layer: aerial zones layer is empty — no poles planned."))
            result = {}
            if out_id:
                result[self.OUT_POLES] = out_id
            return result

        # spatial index of garden trenches for cable counting
        garden_index = QgsSpatialIndex()
        garden_geoms = {}
        if garden is not None:
            for f in garden.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                fid = f.id()
                garden_index.addFeature(f)
                garden_geoms[fid] = g

        feeder_index = QgsSpatialIndex()
        feeder_geoms = {}
        if feeder is not None:
            for f in feeder.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                fid = f.id()
                feeder_index.addFeature(f)
                feeder_geoms[fid] = g

        pdp_index = None
        pdp_geoms = {}
        pdp_ids = {}
        if pdp_lyr is not None:
            pdp_index = QgsSpatialIndex()
            for f in pdp_lyr.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                fid = f.id()
                pdp_index.addFeature(f)
                pdp_geoms[fid] = g
                pid = ""
                if f.fields().indexOf("PDP_ID") >= 0:
                    pid = str(f["PDP_ID"] or "")
                pdp_ids[fid] = pid

        def cables_near(x, y):
            pt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
            buf = pt.buffer(self.CABLE_RADIUS_M, 8)
            ids = garden_index.intersects(buf.boundingBox())
            n = 0
            for fid in ids:
                g = garden_geoms.get(fid)
                if g is not None and g.intersects(buf):
                    n += 1
            return n

        def near_feeder(x, y):
            if feeder_index is None:
                return False
            pt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
            buf = pt.buffer(self.FEEDER_RADIUS_M, 8)
            ids = feeder_index.intersects(buf.boundingBox())
            for fid in ids:
                g = feeder_geoms.get(fid)
                if g is not None and g.intersects(buf):
                    return True
            return False

        def in_zone(x, y):
            pt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
            for zg in zone_geoms:
                if zg.contains(pt):
                    return True
            return False

        counters = {"Pole": 0, "h7m": 0, "h9m": 0}
        placed_xy = []

        def _emit(x, y, cable_cnt, near_feed):
            """One pole at (x, y); refused when a pole is already within 1 m."""
            for px, py in placed_xy:
                if ((px - x) ** 2 + (py - y) ** 2) ** 0.5 < 1.0:
                    return False
            placed_xy.append((x, y))
            height = 7 if cable_cnt <= self.MAX_CABLES_7M else 9
            ptype = "Utility" if near_feed else "Telecom"
            max_cab = (self.MAX_CABLES_7M if height == 7 else self.MAX_CABLES_9M)
            counters["Pole"] += 1
            counters[f"h{height}m"] += 1
            pid = f"PL-{counters['Pole']:04d}"
            feat = QgsFeature(out_fields)
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
            feat[COMMON_FIELDS.POLE_ID] = pid
            feat[COMMON_FIELDS.POLE_TYPE] = ptype
            feat[COMMON_FIELDS.MATERIAL] = "Concrete"
            feat[COMMON_FIELDS.HEIGHT_M] = height
            feat[COMMON_FIELDS.CABLE_CNT] = cable_cnt
            feat[COMMON_FIELDS.EQUIPMENT] = self._nearest_equip(
                pdp_index, pdp_geoms, pdp_ids, x, y, self.EQUIP_SEARCH_M)
            feat[COMMON_FIELDS.CAPACITY_USED] = cable_cnt
            feat[COMMON_FIELDS.CAPACITY_TOTAL] = max_cab
            feat[COMMON_FIELDS.INFRA_STATUS] = InfraStatus.PROPOSED
            feat[COMMON_FIELDS.VERIFY_STATUS] = VerifyStatus.VERIFIED
            feat[COMMON_FIELDS.STAGE] = "Civil"
            if sink is not None and sink.addFeature(
                    feat, QgsFeatureSink.FastInsert):
                return True
            return False

        if garden is not None:
            for f in garden.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                length = g.length()
                if length <= 0:
                    continue
                # only trenches whose midpoint lies in an aerial zone
                mid_pt = g.interpolate(length / 2.0)
                if mid_pt is None or mid_pt.isEmpty():
                    continue
                mp = mid_pt.asPoint()
                if not in_zone(mp.x(), mp.y()):
                    continue

                step = max(10.0, spacing)
                dist = step / 2.0
                while dist < length:
                    q = g.interpolate(dist)
                    if q is None or q.isEmpty():
                        break
                    p = q.asPoint()
                    if in_zone(p.x(), p.y()):
                        _emit(p.x(), p.y(), cables_near(p.x(), p.y()),
                              near_feeder(p.x(), p.y()))
                    dist += step

        # ── The legs the trench stage already classified as aerial ────────
        # A leg is aerial because the zone / length / chain rule fired on it;
        # its midpoint need not land in a zone, and a leg with no pole has
        # nothing to hang from — which is why a run could classify legs and
        # still publish 0 poles, 0 aerial drop trenches and 0 aerial cable.
        # Poles go on the leg itself: one at the network end (the UG→aerial
        # transition) plus the spacing in between, stopping short of the
        # premise by DROP_STANDOFF_M so the last span is a real drop.
        if legs is not None:
            leg_poles = 0
            on_premise = 0

            def _far_point(g):
                """The leg's premise end (its last vertex)."""
                try:
                    if g.isMultipart():
                        parts = g.asMultiPolyline()
                        return parts[-1][-1] if parts and parts[-1] else None
                    line = g.asPolyline()
                    return line[-1] if line else None
                except Exception:
                    return None

            # Every leg ends at the premise it serves, so the set of those
            # points is where the buildings are.  A chained leg (AERIAL_REASON
            # 'chain') *starts* at the house it feeds off — planting a pole
            # there stands it on that customer's roof, and the drop for that
            # very house then measures 0.0 m.
            buildings = []
            leg_list = list(legs.getFeatures())
            for lf in leg_list:
                far = _far_point(lf.geometry()) if lf.geometry() else None
                if far is not None:
                    buildings.append(far)

            for lf in leg_list:
                lg = lf.geometry()
                if lg is None or lg.isEmpty():
                    continue
                length = lg.length()
                if length <= 0:
                    continue
                step = max(10.0, spacing)
                reach = length - self.DROP_STANDOFF_M
                stations = [0.0]
                if reach > step:
                    d = step
                    while d < reach:
                        stations.append(d)
                        d += step
                    stations.append(reach)
                elif reach > 0:
                    stations.append(reach)
                for s in stations:
                    q = lg.interpolate(s)
                    if q is None or q.isEmpty():
                        continue
                    p = q.asPoint()
                    if any(((b.x() - p.x()) ** 2 + (b.y() - p.y()) ** 2) ** 0.5
                           <= self.ON_PREMISE_M for b in buildings):
                        on_premise += 1
                        continue
                    if _emit(p.x(), p.y(), cables_near(p.x(), p.y()),
                             near_feeder(p.x(), p.y())):
                        leg_poles += 1
            if on_premise:
                feedback.pushInfo(self.tr(
                    f"Pole layer: {on_premise} leg station(s) skipped — a pole "
                    f"there would stand on a premise (chained aerial legs "
                    f"start at the house they feed off)."))
            feedback.pushInfo(self.tr(
                f"Pole layer: {leg_poles} pole(s) placed along the aerial legs "
                f"the trench stage classified "
                f"({legs.featureCount()} leg(s) supplied; nearest pole "
                f"{self.DROP_STANDOFF_M:.0f} m back from the premise so the "
                f"final drop is a real span)."))

        feedback.pushInfo(self.tr(
            f"Pole layer: {counters['Pole']} poles planned in aerial zones "
            f"(7 m: {counters['h7m']}, 9 m: {counters['h9m']})."))

        result = {}
        if out_id:
            result[self.OUT_POLES] = out_id
        return result
