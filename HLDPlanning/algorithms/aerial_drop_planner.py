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
    QgsProject,
)
from qgis.PyQt.QtCore import QMetaType

from ..utils.fields import COMMON_FIELDS, build_fields, first_field_case_insensitive


class AerialDropLayerAlgorithm(QgsProcessingAlgorithm):

    # ── Parameter keys ────────────────────────────────────────────────────────

    P_PREMISES = "INPUT_PREMISES"
    P_POLES = "INPUT_POLES"
    P_AERIAL_ZONES = "INPUT_AERIAL_ZONES"
    P_LEGS = "INPUT_AERIAL_LEGS"
    P_ROADS = "INPUT_ROADS"
    P_BF_POLES = "INPUT_BF_POLES"
    P_SPACING = "POLE_SPACING_M"

    OUT_AERIAL_TRENCH = "OUT_AERIAL_TRENCH"
    OUT_AERIAL_CABLE = "OUT_AERIAL_CABLE"

    # ── Constants ─────────────────────────────────────────────────────────────

    MAX_DROP_DISTANCE_M = 70.0       # max aerial drop span
    MAX_POLE_SEARCH_M = 100.0        # search radius for nearest pole
    DEFAULT_SPACING = 50.0           # pole spacing fallback
    MIN_SPAN_M = 1.0                 # a pole standing on the premise is not a
                                     # drop anchor — it yields a 0.0 m span
    BUILDING_ANCHOR_M = 5.0          # how close a leg's start must be to another
                                     # premise before it is a house-to-house span
    LEG_MATCH_M = 5.0                # how close a premise must sit to the
                                     # classified aerial leg it belongs to
    RESERVED_SPARE_FIBERS = 2
    DROP_FIBER_MIN = 12

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
            "Premises without aerial_required are ignored.  A premise is "
            "connected when the trench stage **classified its leg aerial** "
            "(supplied as INPUT_AERIAL_LEGS), or, failing that, when it sits "
            "inside an aerial zone.  The zone polygon cannot be the only "
            "gate: the trench stage also classifies a leg aerial by the "
            "chain/length rules, and those legs have no zone to test against."
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
            self.P_LEGS, self.tr(
                "Aerial legs [lines] (classified by the trench stage; "
                "authoritative over the zone test)"),
            [QgsProcessing.TypeVectorLine], optional=True,
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
        legs = self.parameterAsVectorLayer(parameters, self.P_LEGS, context)
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
            "HH_COUNT",
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
            "HH_COUNT",
            "RESERVED_SPARE_FIBERS",
            "ACTIVE_FIBERS",
            "AVAILABLE_FIBERS",
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

        # The legs the trench stage already classified as aerial.  This is the
        # authoritative classification: the trench stage fires on the zone rule
        # **and** on the chain/length rules, so a 'chain' leg is aerial with no
        # zone anywhere near it.  Testing such a premise against zone polygons
        # alone is what published Aerial_Drops = 3 but Aerial_Cable = 0 (Berlin
        # AD-00001/02 zone, AD-00003 chain — all three counted 'outside zones').
        leg_addr = set()
        leg_geoms = []
        leg_reason = {}
        leg_start = {}          # addr -> the leg's network/anchor end
        leg_coords = {}         # addr -> the leg's own vertices (anchor -> premise)
        building_index = []     # (addr, point) — the premise each leg ends at

        def _polyline_coords(g):
            """First part's vertices as [(x, y), ...] (empty when not a line)."""
            try:
                if g.isMultipart():
                    parts = g.asMultiPolyline()
                    return [(p.x(), p.y()) for p in parts[0]] if parts else []
                return [(p.x(), p.y()) for p in g.asPolyline()]
            except Exception:
                return []

        if legs is not None and legs.isValid() and legs.featureCount() > 0:
            _xform = None
            try:
                if legs.crs() and legs.crs().isValid() and legs.crs() != crs:
                    _xform = QgsCoordinateTransform(
                        legs.crs(), crs, QgsProject.instance())
            except Exception:
                _xform = None
            leg_addr_field = first_field_case_insensitive(
                legs, ["addr_id", "ADDR_ID", "TO_PREMISE"])
            leg_reason_field = first_field_case_insensitive(
                legs, ["AERIAL_REASON", "aerial_reason"])
            for lf in legs.getFeatures():
                key = ""
                if leg_addr_field:
                    v = lf[leg_addr_field]
                    if v is not None and str(v).strip():
                        key = str(v).strip()
                        leg_addr.add(key)
                        if leg_reason_field:
                            rv = lf[leg_reason_field]
                            if rv is not None and str(rv).strip():
                                leg_reason[key] = str(rv).strip()
                g = lf.geometry()
                if g is None or g.isEmpty():
                    continue
                if _xform is not None:
                    g = QgsGeometry(g)
                    g.transform(_xform)
                leg_geoms.append(g)
                coords = _polyline_coords(g)
                if not coords or not key:
                    continue
                leg_start[key] = QgsPointXY(coords[0][0], coords[0][1])
                leg_coords[key] = coords
                # Every leg ENDS at the premise it serves, so those points are
                # where the buildings are.
                building_index.append(
                    (key, QgsPointXY(coords[-1][0], coords[-1][1])))

        # ── Helper: find nearest eligible pole ─────────────────────────────────
        # The aerial planning contract requires adequate height/clearance and
        # spare loading.  Generated poles carry these fields; older brownfield
        # pole layers may omit them, in which case they are not safe enough to
        # anchor a new aerial span and the drop is reported as unresolved.
        def _pole_eligible(feat):
            names = feat.fields().names()
            field_names = feat.fields().names()
            lower_names = {name.lower(): name for name in field_names}
            def _field(candidates):
                for candidate in candidates:
                    if candidate.lower() in lower_names:
                        return lower_names[candidate.lower()]
                return None
            height_f = _field(["HEIGHT_M", "height_m", "HEIGHT", "height"])
            used_f = _field(["CAPACITY_USED", "capacity_used", "CABLE_CNT", "cable_count"])
            total_f = _field(["CAPACITY_TOTAL", "capacity_total", "CAPACITY", "capacity"])
            if not height_f or not used_f or not total_f:
                return False, "missing pole height/loading fields"
            try:
                height = float(feat[height_f] or 0)
                used = float(feat[used_f] or 0)
                total = float(feat[total_f] or 0)
            except (TypeError, ValueError):
                return False, "invalid pole height/loading fields"
            if height < 5.5:
                return False, "ground clearance below 5.5 m"
            if total <= 0 or used / total >= 0.80:
                return False, "pole loading is at or above 80%"
            return True, ""

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
                eligible, _reason = _pole_eligible(feat)
                if not eligible:
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
                eligible, _reason = _pole_eligible(feat)
                if not eligible:
                    continue
                d = float(g.distance(QgsGeometry.fromPointXY(pt)))
                candidates.append((d, feat, g))

            if not candidates:
                return None
            candidates.sort(key=lambda x: x[0])
            # Never anchor a drop to a pole that stands on the premise itself:
            # that is a 0.0 m "span" and it is how aerial trenches were
            # published with no length at all.  Fall through to the next pole.
            usable = [c for c in candidates if c[0] > self.MIN_SPAN_M]
            if not usable:
                return None
            d, feat, g = usable[0]
            pid = str(feat[COMMON_FIELDS.POLE_ID] or feat["POLE_ID"] or feat["pole_id"] or "")
            return pid, g, d

        def _approx_meters(p1, p2):
            """Distance between two points in **metres**.

            The planner runs in whatever CRS the premises arrive in, and Berlin
            arrives in EPSG:25833 — where the old equirectangular formula
            (degrees × 111 320) turned a 20 m drop into 1.7 million metres.  The
            max-span guard then skipped **every** aerial drop, which is why this
            stage could classify legs and still publish nothing.  A projected
            CRS is already metric, so use it directly; only a geographic CRS
            needs the degree formula.
            """
            if crs is not None and crs.isValid() and not crs.isGeographic():
                dx = p2.x() - p1.x()
                dy = p2.y() - p1.y()
                return (dx * dx + dy * dy) ** 0.5
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

        def _on_aerial_leg(pt, addr):
            """Was this premise's leg classified aerial by the trench stage?"""
            if addr and addr in leg_addr:
                return True
            if leg_geoms:
                pg = QgsGeometry.fromPointXY(pt)
                for lg in leg_geoms:
                    if float(lg.distance(pg)) <= self.LEG_MATCH_M:
                        return True
            return False

        def _building_at(pt, own_addr):
            """The premise this point stands on, or '' when it stands free.

            A 'chain' leg leaves the house it feeds from, so its start sits on
            a building — and a pole planted there would stand on that
            customer's roof.  The leg endpoints are the evidence.
            """
            for addr, bp in building_index:
                if not addr or addr == own_addr:
                    continue
                if _approx_meters(bp, pt) <= self.BUILDING_ANCHOR_M:
                    return addr
            return ""

        def _path_len_m(points):
            """Length of a vertex path in metres (a leg can be bent)."""
            return sum(_approx_meters(points[i], points[i + 1])
                       for i in range(len(points) - 1))

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
        from_leg = 0
        building_anchored = 0

        aerial_field = first_field_case_insensitive(
            premises, ["aerial_required", "AERIAL_REQUIRED", "aerial", "AERIAL"]
        )
        addr_field = first_field_case_insensitive(
            premises, ["ADDR_ID", "addr_id", "SRC_ID"]
        )
        hh_field = first_field_case_insensitive(
            premises, ["HH", "hhs", "HH_COUNT"]
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

            addr = ""
            if addr_field:
                v = f[addr_field]
                if v is not None:
                    addr = str(v).strip()

            # The trench stage's classification wins.  A 'chain' or 'length'
            # leg is aerial with no zone polygon to satisfy, so the zone test
            # only applies to premises the trench stage did not classify.
            classified = _on_aerial_leg(pt, addr)
            if classified:
                from_leg += 1
            elif not _point_in_zone(QgsGeometry.fromPointXY(pt)):
                skipped_no_zone += 1
                continue

            # Anchor the drop.  When the leg starts on ANOTHER premise it is a
            # house-to-house span ('chain'), so the anchor is that building and
            # the leg geometry is the span — routing from the nearest pole
            # instead published a 38.6 m span where the field builds ~18.6 m.
            anchor_id, anchor_pt, anchor_dist, path = "", None, 0.0, None
            start_pt = leg_start.get(addr) if addr else None
            if start_pt is not None:
                host = _building_at(start_pt, addr)
                if host:
                    anchor_id = "BLDG-%s" % host
                    anchor_pt = start_pt
                    path = leg_coords.get(addr)
                    building_anchored += 1
            if not anchor_id:
                nearest = _nearest_pole(pt)
                if nearest is None:
                    skipped_no_pole += 1
                    continue
                anchor_id, anchor_geom, anchor_dist = nearest
                anchor_pt = (
                    anchor_geom.centroid().asPoint()
                    if anchor_geom.wkbType() == QgsWkbTypes.PointGeometry
                    else anchor_geom.asPoint())

            try:
                hh_count = max(1, int(float(f[hh_field] or 1))) if hh_field else 1
            except (TypeError, ValueError):
                hh_count = 1
            aerial_premises.append({
                "f": f, "pt": pt, "addr": addr, "hh_count": hh_count,
                "anchor_id": anchor_id, "anchor_pt": anchor_pt,
                "anchor_dist": anchor_dist, "path": path,
            })

        feedback.pushInfo(self.tr(
            f"Aerial drop planner: {len(aerial_premises)} premises flagged "
            f"({from_leg} from the trench stage's aerial classification, "
            f"{building_anchored} anchored on a building — house to house), "
            f"{skipped_no_flag} not flagged, {skipped_no_zone} outside zones, "
            f"{skipped_no_pole} no reachable pole."
        ))

        if not aerial_premises:
            # Return empty outputs
            return {
                self.OUT_AERIAL_TRENCH: id_t,
                self.OUT_AERIAL_CABLE: id_c,
            }

        # ── Route aerial drops ──────────────────────────────────────────────────

        import math
        counter = {"trench": 0, "cable": 0, "pole_spans": 0, "skipped": 0}
        pole_span_written = set()

        def _pole_id(feat):
            for name in (COMMON_FIELDS.POLE_ID, "POLE_ID", "pole_id", "id", "feature_id"):
                if name in feat.fields().names() and feat[name] not in (None, ""):
                    return str(feat[name])
            return str(feat.id())

        def _poles_on_path(path):
            """Return eligible poles ordered along an aerial path."""
            if len(path) < 2 or poles is None or not poles.isValid():
                return []
            line = QgsGeometry.fromPolylineXY(path)
            total = line.length()
            found = []
            for pf in poles.getFeatures():
                pg = pf.geometry()
                if pg is None or pg.isEmpty():
                    continue
                try:
                    if pg.distance(line) > 8.0:
                        continue
                    at = line.lineLocatePoint(pg)
                    if at < -0.01 or at > total + 0.01:
                        continue
                    found.append((at, _pole_id(pf), pg.asPoint()))
                except Exception:
                    continue
            found.sort(key=lambda row: row[0])
            return found

        def _write_pole_span(a, b, p1, p2):
            """Write one deduplicated pole-to-pole aerial trench and cable."""
            if not a or not b or a == b:
                return
            key = tuple(sorted((str(a), str(b))))
            if key in pole_span_written:
                return
            length = _path_len_m([p1, p2])
            if length <= self.MIN_SPAN_M or length > self.MAX_DROP_DISTANCE_M:
                return
            pole_span_written.add(key)
            tprops = {
                COMMON_FIELDS.AERIAL_TRENCH_ID: f"AT-P{counter['pole_spans'] + 1:04d}",
                COMMON_FIELDS.POLE_ID: str(a), COMMON_FIELDS.FROM_POLE: str(a),
                COMMON_FIELDS.TO_PREMISE: "POLE:%s" % b,
                COMMON_FIELDS.TRENCH_TYPE: "Aerial_Drop",
                COMMON_FIELDS.CONSTRUCTION_METHOD: "Overhead",
                COMMON_FIELDS.CABLE_TYPE: "Aerial", COMMON_FIELDS.FIBER_COUNT: 12,
                COMMON_FIELDS.LENGTH_M: round(length, 1), COMMON_FIELDS.POLE_SPACING_M: spacing,
                COMMON_FIELDS.CROSSINGS: 0, COMMON_FIELDS.PERMIT_REQUIRED: False,
                COMMON_FIELDS.AERIAL_REASON: "pole_to_pole",
                COMMON_FIELDS.INFRA_STATUS: "Proposed", COMMON_FIELDS.VERIFY_STATUS: "Assumed",
                COMMON_FIELDS.STAGE: "HLD",
            }
            cprops = {
                COMMON_FIELDS.CABLE_TYPE: "Aerial", COMMON_FIELDS.FIBER_COUNT: 12,
                COMMON_FIELDS.LENGTH_M: round(length, 1),
                COMMON_FIELDS.SOURCE_NODE: "%s->%s" % (a, b), COMMON_FIELDS.UTIL_PCT: 100.0,
                COMMON_FIELDS.INFRA_STATUS: "Proposed", COMMON_FIELDS.VERIFY_STATUS: "Assumed",
                COMMON_FIELDS.STAGE: "HLD",
            }
            geom = QgsGeometry.fromPolylineXY([p1, p2])
            tf = QgsFeature(trench_fields); tf.setGeometry(geom)
            for k, v in tprops.items(): tf[k] = v
            sink_t.addFeature(tf)
            cf = QgsFeature(cable_fields); cf.setGeometry(geom)
            for k, v in cprops.items(): cf[k] = v
            sink_c.addFeature(cf)
            counter["pole_spans"] += 1
            counter["trench"] += 1; counter["cable"] += 1

        for item in aerial_premises:
            f, pt = item["f"], item["pt"]
            hh_count = item["hh_count"]
            fiber_count = max(self.DROP_FIBER_MIN, hh_count + self.RESERVED_SPARE_FIBERS)
            anchor_id = item["anchor_id"]

            if item["path"]:
                # House-to-house: the leg IS the span, so keep its designed
                # geometry from the building it hangs off.  No road snapping —
                # an overhead span does not follow the carriageway.
                path = [QgsPointXY(x, y) for x, y in item["path"]]
            else:
                if item["anchor_dist"] > self.MAX_POLE_SEARCH_M:
                    counter["skipped"] += 1
                    continue
                pole_pt = item["anchor_pt"]
                # Snap both endpoints to roads for practical routing
                snapped_pole = _snap_to_road(pole_pt, max_snap_m=20.0)
                snapped_premise = _snap_to_road(pt, max_snap_m=20.0)
                # Both values are QgsPointXY — comparing them against a
                # QgsGeometry raises TypeError.
                if snapped_pole.distance(pole_pt) < 0.1 and \
                   snapped_premise.distance(pt) < 0.1:
                    path = [pole_pt, pt]
                else:
                    path = [snapped_pole, snapped_premise]

            # Materialize the overhead backbone between every consecutive
            # eligible pole found on this aerial route. The premise span below
            # remains separate; shared pole pairs are written only once.
            pole_seq = _poles_on_path(path)
            for (_at0, pid0, p0), (_at1, pid1, p1) in zip(pole_seq, pole_seq[1:]):
                _write_pole_span(pid0, pid1, p0, p1)

            length_m = _path_len_m(path) if len(path) > 1 else 0.0
            if length_m > self.MAX_DROP_DISTANCE_M:
                feedback.pushWarning(
                    self.tr(f"Aerial drop {length_m:.0f}m exceeds the "
                            f"{self.MAX_DROP_DISTANCE_M:.0f}m maximum span — "
                            "no compliant aerial route was found.")
                )
                counter["skipped"] += 1
                continue

            addr_val = str(f["ADDR_ID"] if "ADDR_ID" in f.fields().names() else
                          f["addr_id"] if "addr_id" in f.fields().names() else
                          f.id())

            # Common properties
            trench_props = {
                COMMON_FIELDS.AERIAL_TRENCH_ID: f"AT-{counter['trench'] + 1:04d}",
                # The anchor is a pole id, or `BLDG-<addr>` when the span leaves
                # another building ('chain' leg) — the field names the anchor,
                # whatever anchors it.
                COMMON_FIELDS.POLE_ID: str(anchor_id or ""),
                COMMON_FIELDS.FROM_POLE: str(anchor_id or ""),
                COMMON_FIELDS.TO_PREMISE: addr_val,
                COMMON_FIELDS.TRENCH_TYPE: "Aerial_Drop",
                COMMON_FIELDS.CONSTRUCTION_METHOD: "Overhead",
                COMMON_FIELDS.CABLE_TYPE: "Aerial",
                COMMON_FIELDS.FIBER_COUNT: fiber_count,
                "HH_COUNT": hh_count,
                COMMON_FIELDS.LENGTH_M: round(length_m, 1),
                COMMON_FIELDS.POLE_SPACING_M: spacing,
                COMMON_FIELDS.CROSSINGS: 0,
                COMMON_FIELDS.PERMIT_REQUIRED: False,
                # Carry the trench stage's own reason (zone / chain / length)
                # so the aerial drop traces back to the rule that made it
                # aerial rather than to a generic label.
                COMMON_FIELDS.AERIAL_REASON: (
                    leg_reason.get(addr_val)
                    or next((leg_reason[k] for k in leg_reason
                             if k and addr_val and k in str(addr_val)), "")
                    or "hlv_evaluation"),
                COMMON_FIELDS.INFRA_STATUS: "Proposed",
                COMMON_FIELDS.VERIFY_STATUS: "Assumed",
                COMMON_FIELDS.STAGE: "HLD",
            }

            cable_props = {
                COMMON_FIELDS.CABLE_TYPE: "Aerial",
                COMMON_FIELDS.FIBER_COUNT: fiber_count,
                "HH_COUNT": hh_count,
                "RESERVED_SPARE_FIBERS": 2,
                "ACTIVE_FIBERS": hh_count,
                "AVAILABLE_FIBERS": max(0, fiber_count - hh_count - 2),
                COMMON_FIELDS.LENGTH_M: round(length_m, 1),
                COMMON_FIELDS.SOURCE_NODE: str(anchor_id or ""),
                COMMON_FIELDS.UTIL_PCT: round((hh_count / float(fiber_count)) * 100.0, 1),
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
            f"{counter['cable']} cables ({counter['pole_spans']} pole-to-pole), "
            f"{counter['skipped']} skipped."
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
