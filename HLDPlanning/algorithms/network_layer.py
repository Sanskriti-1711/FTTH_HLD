# -*- coding: utf-8 -*-
import heapq
import math
import os
from collections import defaultdict

from qgis.PyQt.QtCore import QCoreApplication, QMetaType
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSink, QgsProcessingParameterBoolean,
    QgsProcessingParameterField, QgsProcessingParameterString,
    QgsProcessingParameterFile,
    QgsProcessingException, QgsProcessingUtils, QgsProject, QgsVectorLayer,
    QgsFeatureSink, QgsFields, QgsField, QgsGeometry,
    QgsWkbTypes, QgsFeature, QgsApplication,QgsProcessingParameterDefinition,
    QgsSpatialIndex, QgsPointXY, QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
)
from qgis import processing
from ..utils.layer_io import as_layer
from ..utils.network_manager import NetworkManager
from ..utils.fields import COMMON_FIELDS, THIN_PROFILES, build_fields, first_field_case_insensitive
from ..utils.splitters import plan_splitters
from ..utils.mfg_partition import partition_mfg_service_areas

try:
    from osgeo import gdal
except Exception:
    gdal = None

def _has(alg_id: str) -> bool:
    try:
        return QgsApplication.processingRegistry().algorithmById(alg_id) is not None
    except Exception:
        return False

def _geom_covers(a: QgsGeometry, b: QgsGeometry) -> bool:
    # Prefer native covers when present
    if hasattr(a, "covers"):
        try:
            return a.covers(b)
        except Exception:
            pass
    # Fallback that treats boundary as covered
    return a.contains(b) or a.touches(b)


def _thin_feature(fields: QgsFields, geom: QgsGeometry, attrs: dict) -> QgsFeature:
    """Create a feature with a strict/thin schema and safe attribute assignment."""
    f = QgsFeature(fields)
    f.setGeometry(geom)
    for name, value in attrs.items():
        if fields.indexOf(name) >= 0:
            f[name] = value
    return f

# prefer makevalid; fallback to fixgeometries

def _valid_layer(input_layer, context, feedback):
    if _has("native:makevalid"):
        return as_layer(processing.run(
            "native:makevalid",
            {"INPUT": input_layer, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context)
    return as_layer(processing.run(
        "native:fixgeometries",
        {"INPUT": input_layer, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
        is_child_algorithm=True, context=context, feedback=feedback
    )["OUTPUT"], context)


def _mfg_road_distance_matrix(polygon_geometries, roads_layer, feedback=None,
                              max_road_m=3000.0):
    """Shortest road-network distances between premise-polygon representatives.

    Each representative is projected to its nearest eligible road segment.
    Its projection is inserted as a real graph node by splitting that segment;
    this matters because snapping only to a segment's endpoints would either
    distort service distances or create a shortcut through an unsplit edge.
    """
    line_geometries = []
    for feature in roads_layer.getFeatures():
        geom = feature.geometry()
        if geom is not None and not geom.isEmpty():
            line_geometries.append(geom)
    if not line_geometries:
        return {}, {}

    network = QgsGeometry.unaryUnion(line_geometries)
    if network is None or network.isEmpty():
        return {}, {}
    parts = []

    def collect_lines(geometry):
        """Extract every line part, including lines nested in collections."""
        if geometry is None or geometry.isEmpty():
            return
        geometry_type = QgsWkbTypes.geometryType(geometry.wkbType())
        if geometry_type == QgsWkbTypes.LineGeometry:
            if geometry.isMultipart():
                multiline = geometry.asMultiPolyline()
                if multiline:
                    parts.extend(multiline)
            else:
                line = geometry.asPolyline()
                if line:
                    parts.append(line)
            return
        try:
            for child in geometry.asGeometryCollection():
                collect_lines(child)
        except Exception:
            return

    collect_lines(network)

    node_xy = {}
    segments = []

    def key_for(point):
        key = (round(float(point.x()), 3), round(float(point.y()), 3))
        node_xy.setdefault(key, QgsPointXY(point))
        return key

    for line in parts:
        if len(line) < 2:
            continue
        previous = line[0]
        previous_key = key_for(previous)
        for point in line[1:]:
            point_key = key_for(point)
            if point_key != previous_key:
                segments.append((QgsPointXY(previous), QgsPointXY(point),
                                 previous_key, point_key))
            previous, previous_key = point, point_key

    if not segments:
        return {}, {}

    projections = {}
    segment_cuts = defaultdict(list)
    for polygon_id, geometry in polygon_geometries.items():
        representative = geometry.pointOnSurface()
        if representative is None or representative.isEmpty():
            representative = geometry.centroid()
        if representative is None or representative.isEmpty():
            continue
        point = representative.asPoint()
        best = None
        for segment_index, (a, b, ka, kb) in enumerate(segments):
            dx, dy = b.x() - a.x(), b.y() - a.y()
            length2 = dx * dx + dy * dy
            t = 0.0 if length2 <= 1e-12 else (
                (point.x() - a.x()) * dx + (point.y() - a.y()) * dy
            ) / length2
            t = min(1.0, max(0.0, t))
            x, y = a.x() + t * dx, a.y() + t * dy
            off_road = math.hypot(point.x() - x, point.y() - y)
            if best is None or off_road < best[0]:
                best = (off_road, segment_index, t, QgsPointXY(x, y))
        if best is None:
            continue
        off_road, segment_index, t, snapped = best
        road_key = key_for(snapped)
        projections[polygon_id] = (road_key, off_road)
        segment_cuts[segment_index].append((t, road_key))

    adjacency = defaultdict(dict)

    def add_edge(left, right):
        if left == right:
            return
        a, b = node_xy[left], node_xy[right]
        length = math.hypot(b.x() - a.x(), b.y() - a.y())
        if length <= 1e-6:
            return
        previous = adjacency[left].get(right)
        if previous is None or length < previous:
            adjacency[left][right] = length
            adjacency[right][left] = length

    for segment_index, (a, b, ka, kb) in enumerate(segments):
        cuts = [(0.0, ka), *segment_cuts.get(segment_index, ()), (1.0, kb)]
        cuts.sort(key=lambda item: (item[0], item[1]))
        ordered = []
        for cut in cuts:
            if ordered and cut[1] == ordered[-1][1]:
                continue
            ordered.append(cut)
        for (_ta, left), (_tb, right) in zip(ordered, ordered[1:]):
            add_edge(left, right)

    distances = {}
    locations = {polygon_id: node for polygon_id, (node, _off) in projections.items()}
    offsets = {polygon_id: off for polygon_id, (_node, off) in projections.items()}
    for source_index, (polygon_id, source) in enumerate(locations.items(), 1):
        best = {source: 0.0}
        queue = [(0.0, source)]
        while queue:
            cost, node = heapq.heappop(queue)
            if cost > best.get(node, math.inf):
                continue
            for neighbour, edge_cost in adjacency.get(node, {}).items():
                next_cost = cost + edge_cost
                if next_cost <= max_road_m and next_cost < best.get(neighbour, math.inf):
                    best[neighbour] = next_cost
                    heapq.heappush(queue, (next_cost, neighbour))
        for other_id, other_node in locations.items():
            if other_node in best:
                # MFG is placed at the seed's road projection, so its service
                # distance includes only the served premise's off-road access
                # leg (not the seed polygon's access leg).
                route_distance = best[other_node] + offsets[other_id]
                if route_distance <= max_road_m:
                    distances[(polygon_id, other_id)] = route_distance
        if feedback and source_index % 20 == 0:
            feedback.pushInfo(
                f"MFG road reach: routed {source_index}/{len(locations)} polygon representatives."
            )
    return locations, distances




def _snap_points_to_lines(points_layer, lines_layer, tolerance_m, context, feedback):
    """
    Snap points to nearest location on lines.

    Order:
      1) Try native:snappointstolines
      2) Try qgis:snappointstolines
      3) PyQGIS fallback: spatial index + closestSegmentWithContext

    If tolerance_m > 0, only accept snaps within that distance (meters in layer CRS).
    Returns a memory point layer (same fields/CRS as INPUT points).
    """
    tol = max(0.0, float(tolerance_m or 0.0))

    # --- 1) native:snappointstolines -----------------------------------------
    if _has("native:snappointstolines"):
        return as_layer(processing.run(
            "native:snappointstolines",
            {
                "INPUT": points_layer,
                "REFERENCE_LAYER": lines_layer,
                "TOLERANCE": tol,
                "BEHAVIOR": 0,
                "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
            },
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context)

    # --- 2) qgis:snappointstolines (older provider id on some installs) -------
    if _has("qgis:snappointstolines"):
        return as_layer(processing.run(
            "qgis:snappointstolines",
            {
                "INPUT": points_layer,
                "REFERENCE_LAYER": lines_layer,
                "TOLERANCE": tol,
                "BEHAVIOR": 0,
                "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
            },
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context)

    # --- 3) PyQGIS fallback (no Processing dependency) ------------------------
    try:
        # Guard: lines available?
        line_feats = list(lines_layer.getFeatures())
        if not line_feats:
            feedback.reportError("Walkable/lines layer is empty; cannot snap. Returning originals.")
            return as_layer(points_layer, context)

        # Build spatial index correctly (empty -> addFeature in a loop)
        sindex = QgsSpatialIndex()
        id_to_feat = {}
        for lf in line_feats:
            if feedback.isCanceled():
                raise QgsProcessingException("Canceled by user")
            id_to_feat[lf.id()] = lf
            sindex.addFeature(lf)

        # Prepare output memory layer with same schema/CRS as input points
        out = QgsVectorLayer(f"Point?crs={points_layer.crs().authid()}", "snapped_pts", "memory")
        out_pr = out.dataProvider()
        out_pr.addAttributes(points_layer.fields())
        out.updateFields()

        # Snap each point
        total = points_layer.featureCount() if points_layer is not None else 0
        cnt = 0
        for p in points_layer.getFeatures():
            if feedback.isCanceled():
                raise QgsProcessingException("Canceled by user")
            cnt += 1
            if cnt % 200 == 0:
                feedback.pushInfo(f"Snapping points: processed {cnt}/{total}")

            g = p.geometry()
            if not g or g.isEmpty():
                continue

            # Get input point coordinate
            pt = None
            try:
                pt = g.asPoint()
            except Exception:
                try:
                    mpts = g.asMultiPoint()
                    if mpts:
                        pt = mpts[0]
                except Exception:
                    pt = None

            best_geom = g
            if pt is not None:
                qpt = QgsPointXY(pt)
                # examine a modest number of nearest lines but guard empty index
                try:
                    nearest_ids = sindex.nearestNeighbor(qpt, 8)
                except Exception:
                    nearest_ids = []
                best_dist = None
                best_point = None

                for lid in nearest_ids:
                    if feedback.isCanceled():
                        raise QgsProcessingException("Canceled by user")
                    lf = id_to_feat.get(lid)
                    if not lf:
                        continue
                    lg = lf.geometry()
                    if not lg or lg.isEmpty():
                        continue

                    d, minPt, _, _ = lg.closestSegmentWithContext(qpt)
                    if best_dist is None or d < best_dist:
                        best_dist = d
                        best_point = minPt

                if best_point is not None and best_dist is not None and (tol <= 0.0 or best_dist <= tol):
                    best_geom = QgsGeometry.fromPointXY(QgsPointXY(best_point))
                # else: outside tolerance -> keep original

            nf = QgsFeature(out.fields())
            nf.setGeometry(best_geom)
            nf.setAttributes(p.attributes())  # copy attributes in order
            out_pr.addFeature(nf)

        out.updateExtents()
        return as_layer(out, context)

    except QgsProcessingException:
        raise
    except Exception as e:
        feedback.pushWarning(f"PyQGIS snapping fallback failed: {e}. Returning original points.")
        return as_layer(points_layer, context)



class NetworkLayerAlgorithm(QgsProcessingAlgorithm):
    # Inputs — surface slimmed 2026-07-03: only the four layer inputs remain.
    # POLY_ID is auto-detected (POLYGON_ID candidates); the road filter, PDP
    # spacing geometry and MFG placement run on the fixed defaults below
    # (the old MFG_R_* radius params were declared but never used).
    INPUT_POLY = "INPUT_POLY"
    INPUT_ROADS = "INPUT_ROADS"
    INPUT_OSM_PBF = "INPUT_OSM_PBF"
    INPUT_OBJECTS = "INPUT_OBJECTS"

    DEFAULT_FILTER_CAND = (
        "\"fclass\" IN ('residential','living_street','unclassified',"
        "'tertiary','secondary','primary','service')"
    )
    # Sidewalk ribbon offset (m): the distance from the road CENTRELINE at which
    # PDP candidates are sampled, so it IS where the cabinet ends up. It was
    # 8.0 m, which is past the kerb and at the building line — measured on the
    # reference run, 24 of 31 PDPs sat exactly 8.00 m off their street, i.e.
    # inside the residential block, while the trench that serves them is offset
    # (`trench_layer.run()` sw_off). A splitter is a street cabinet: it
    # belongs on the sidewalk, so this now matches the trench's own sidewalk
    # offset (`trench_layer.SIDEWALK_OFFSET_M`) instead of contradicting it; a
    # test asserts the two are equal so they cannot drift apart again.
    #
    # The ribbon is laid PER CLASS at the band rule (half the carriageway + a
    # footway inset — trench_design.kerb_offset_for, the same rule the derived
    # pavement and the designer's kerb band follow); this value is the BASE
    # band and the fallback for a class the width table does not name.
    DEFAULT_SIDEWALK = 4.5    # base sidewalk ribbon offset (m)
    DEFAULT_SPACING = 30.0    # candidate PDP spacing along sidewalks (m)
    DEFAULT_INTER_BUF = 20.0  # intersection guard radius (m)
    DEFAULT_CENTROID_R = 10.0  # centroid catch radius (m)
    DEFAULT_INNER_BUF = 3.0   # polygon interior buffer (m)
    DEFAULT_DO_MFG = True     # always place the MFG point

    # Outputs (Stage 1–3)
    OUT_EDGES = "OUT_EDGES"
    OUT_CAND = "OUT_CAND"
    OUT_REMOVED = "OUT_REMOVED"
    OUT_CLEAN = "OUT_CLEAN"
    OUT_ASSIGNED = "OUT_ASSIGNED"
    OUT_CLIPPED_ROADS = "OUT_CLIPPED_ROADS"

    # Outputs (derived MFG service network)
    OUT_MFG = "OUT_MFG_POINT"
    OUT_MFG_AREAS = "OUT_MFG_AREAS"

    # Final object layer: objects + POLYGON_ID/PDP_ID/MFG_ID from the registry
    OUT_FINAL_OBJECTS = "OUT_FINAL_OBJECTS"

    # -------------- UI / Parameter definitions --------------
    def initAlgorithm(self, config=None):
        # Core inputs
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_POLY, "Polygons (POLYGON_ID auto-detected)", [QgsProcessing.TypeVectorPolygon]
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_ROADS,
            "Roads (full or clipped; optional when OSM PBF is supplied)",
            [QgsProcessing.TypeVectorLine],
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_OSM_PBF,
            "Berlin/Germany OSM PBF (optional alternative to Roads)",
            extension="pbf",
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_OBJECTS,
            "Object points (optional, used to score PDPs per polygon)",
            [QgsProcessing.TypeVectorPoint],
            optional=True,
        ))

        # Outputs
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUT_EDGES,   "buffer_edges"))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUT_CAND,    "candidate_pdps"))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUT_REMOVED, "pdps_to_remove"))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUT_CLEAN,   "clean_pdps"))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUT_ASSIGNED,"Final_PDPs_per_polygon"))
        clipped_param = QgsProcessingParameterFeatureSink(
            self.OUT_CLIPPED_ROADS, "Clipped roads (optional)"
        )
        clipped_param.setFlags(
            clipped_param.flags() | QgsProcessingParameterDefinition.FlagOptional
        )
        self.addParameter(clipped_param)
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUT_MFG, "Final_MFG_Point"))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_MFG_AREAS, "MFG serving-area boundaries",
            QgsProcessing.TypeVectorPolygon, optional=True,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_FINAL_OBJECTS, "Final_Object_Layer", optional=True, createByDefault=True
        ))
    
    def tr(self, s): return QCoreApplication.translate("NetworkLayerAlgorithm", s)
    def name(self): return "03_network_layer"
    def displayName(self): return self.tr("Build Network Layer")
    def group(self):return self.tr("03 Network Layer")
    def groupId(self): return "03_network_layer"
    def flags(self):
        # Python GDAL PBF extraction and nested geoprocessing are not safe in
        # QGIS's background QgsProcessingAlgRunnerTask on Windows.
        return super().flags() | QgsProcessingAlgorithm.Flag.FlagNoThreading
    def shortHelpString(self):
        return self.tr(
            "Generates the Network Layer from polygons and either a road line "
            "layer or an OSM PBF. Full roads are clipped automatically, with "
            "optional MFG placement."
        )
    def createInstance(self): return NetworkLayerAlgorithm()

    # -------------- Processing logic --------------

    # --- Drop-in for network_layer.py: NetworkLayerAlgorithm.processAlgorithm ---
    def processAlgorithm(self, params, context, feedback):
        polys = self.parameterAsVectorLayer(params, self.INPUT_POLY, context)
        roads = self.parameterAsVectorLayer(params, self.INPUT_ROADS, context)
        pbf_path = self.parameterAsFile(params, self.INPUT_OSM_PBF, context)
        objects = self.parameterAsVectorLayer(params, self.INPUT_OBJECTS, context)
        if polys is None:
            raise QgsProcessingException("Select the planning polygon layer.")

        def _load_vector_layer_from_path(path):
            if not path:
                return None
            if not os.path.exists(path):
                return None

            def _is_line_layer(vl):
                return vl.isValid() and QgsWkbTypes.geometryType(vl.wkbType()) == QgsWkbTypes.LineGeometry

            candidates = [path]
            if path.lower().endswith(".pbf"):
                candidates = [
                    f"{path}|layername=lines",
                    f"{path}|layername=roads",
                    f"{path}|layername=road",
                    f"{path}|layername=transport_lines",
                    path,
                ]

            for src in candidates:
                try:
                    vl = QgsVectorLayer(src, "roads", "ogr")
                    if vl.isValid() and vl.featureCount() > 0 and _is_line_layer(vl):
                        return vl
                except Exception:
                    continue

            for src in candidates:
                try:
                    vl = QgsVectorLayer(src, "roads", "ogr")
                    if vl.isValid() and vl.featureCount() > 0:
                        return vl
                except Exception:
                    continue
            return None

        if roads is None:
            raw_roads = params.get(self.INPUT_ROADS)
            if isinstance(raw_roads, str) and raw_roads:
                if raw_roads.lower().endswith(".pbf"):
                    pbf_path = raw_roads
                else:
                    roads = _load_vector_layer_from_path(raw_roads)
            elif pbf_path:
                roads = None

        if roads is None and pbf_path:
            if not os.path.exists(pbf_path):
                raise QgsProcessingException(f"OSM PBF does not exist: {pbf_path}")
            if gdal is not None:
                pbf_roads = QgsProcessingUtils.generateTempFilename("network_osm_roads.gpkg")
                try:
                    to_wgs84 = QgsCoordinateTransform(
                        polys.crs(),
                        QgsCoordinateReferenceSystem("EPSG:4326"),
                        context.transformContext(),
                    )
                    bbox = to_wgs84.transformBoundingBox(polys.extent())
                    margin = 0.003
                    options = [
                        "-sql", "SELECT * FROM lines WHERE highway IS NOT NULL",
                        "-nln", "roads",
                        "-spat",
                        str(bbox.xMinimum() - margin),
                        str(bbox.yMinimum() - margin),
                        str(bbox.xMaximum() + margin),
                        str(bbox.yMaximum() + margin),
                    ]
                    dataset = gdal.VectorTranslate(pbf_roads, pbf_path, options=options)
                    if dataset is None:
                        raise RuntimeError("GDAL VectorTranslate returned no dataset")
                    dataset = None
                except Exception as exc:
                    feedback.pushWarning(f"GDAL PBF extraction failed: {exc}. Trying direct OGR load instead.")
                    roads = _load_vector_layer_from_path(pbf_path)
                else:
                    roads = QgsVectorLayer(f"{pbf_roads}|layername=roads", "OSM roads", "ogr")
            else:
                roads = _load_vector_layer_from_path(pbf_path)

            if roads is not None and (not roads.isValid() or roads.featureCount() == 0):
                roads = None

            if roads is not None:
                feedback.pushInfo(
                    f"Extracted {roads.featureCount()} nearby road features from OSM PBF."
                )

        if roads is None:
            raise QgsProcessingException(
                "Provide either a road line layer or an OSM .pbf file."
            )

        # Basic fields/CRS — polygon ID auto-detected from canonical candidates
        pid_field = first_field_case_insensitive(
            polys, ["POLYGON_ID", "POLY_ID", "polygon_id", "poly_id", "id", "fid"]
        )
        if not pid_field:
            raise QgsProcessingException(
                "Polygon layer needs a POLYGON_ID (or POLY_ID/id) field — run stage 02 first."
            )

        # Schema alignment guidance in processing panel.
        poly_field_names = polys.fields().names() if polys is not None else []
        obj_field_names = objects.fields().names() if objects is not None else []
        road_field_names = roads.fields().names() if roads is not None else []
        feedback.pushInfo("Schema alignment guide:")
        feedback.pushInfo(f"- Polygon layer: unique id field required (selected: '{pid_field}').")
        feedback.pushInfo("- Object layer (optional): ADDR_ID or stable feature ids; HH recommended.")
        feedback.pushInfo("- Roads layer: 'fclass' or 'highway' recommended for filtering.")
        feedback.pushInfo(f"Detected polygon fields: {poly_field_names}")
        if objects is not None:
            feedback.pushInfo(f"Detected object fields: {obj_field_names}")
        else:
            feedback.pushInfo("Detected object fields: <none> (INPUT_OBJECTS not provided)")
        feedback.pushInfo(f"Detected road fields: {road_field_names}")

        # Fixed geometry defaults (parameter surface slimmed)
        expr_cand = self.DEFAULT_FILTER_CAND
        off       = self.DEFAULT_SIDEWALK
        space     = self.DEFAULT_SPACING
        interbuf  = self.DEFAULT_INTER_BUF
        centr     = self.DEFAULT_CENTROID_R
        inner_b   = self.DEFAULT_INNER_BUF

        do_mfg    = self.DEFAULT_DO_MFG
        pdp_src = None  # ensure defined even if correction branch is skipped
        
        # Helper: bridge single-geometry snap -> layer-based snapper used elsewhere
        def _snap_geom_to_lines(pt_geom, lines_layer, tol_m, crs_authid):
            """Wrap the layer-based _snap_points_to_lines to accept a single QgsGeometry point."""
            try:
                tmp_pt = QgsVectorLayer(f"Point?crs={crs_authid}", "tmp_pt", "memory")
                pr = tmp_pt.dataProvider()
                pr.addAttributes([QgsField("id", QMetaType.Type.Int)])
                tmp_pt.updateFields()
                f = QgsFeature(tmp_pt.fields())
                f.setGeometry(pt_geom)
                f["id"] = 1
                pr.addFeature(f)
                tmp_pt.updateExtents()

                snapped = _snap_points_to_lines(
                    points_layer=tmp_pt,
                    lines_layer=lines_layer,
                    tolerance_m=float(tol_m),
                    context=context, feedback=feedback
                )
                for sf in snapped.getFeatures():
                    return sf.geometry()
            except Exception:
                pass
            return None  # caller should fallback to original geom

        def _make_point_layer(crs_authid, geom):
            """Create a memory point layer with one feature."""
            vl = QgsVectorLayer(f"Point?crs={crs_authid}", "tmp_pt", "memory")
            pr = vl.dataProvider()
            pr.addAttributes([QgsField("id", QMetaType.Type.Int)])
            vl.updateFields()
            f = QgsFeature(vl.fields())
            f.setGeometry(geom)
            f["id"] = 1
            pr.addFeature(f)
            vl.updateExtents()
            return vl

        def _nearest_street_point(pt_geom, lines_layer):
            """Nearest point ON a street line to ``pt_geom``, and its distance.

            A PDP is a street cabinet, so when no sidewalk candidate exists this
            is where it goes — never at a polygon's interior. Returns
            ``(None, None)`` when the area carries no street at all.
            """
            if lines_layer is None or pt_geom is None or pt_geom.isEmpty():
                return None, None
            best_d, best_pt = None, None
            try:
                for f in lines_layer.getFeatures():
                    g = f.geometry()
                    if g is None or g.isEmpty():
                        continue
                    d = g.distance(pt_geom)
                    if best_d is None or d < best_d:
                        best_d, best_pt = d, QgsGeometry(g.nearestPoint(pt_geom))
            except Exception:
                return None, None
            if best_pt is None or best_pt.isEmpty():
                return None, None
            return best_pt, best_d

        def _polygon_reference_points(poly_geom, object_layer, polygon_crs):
            """Return a list of representative points for a polygon, preferring object points."""
            refs = []
            if object_layer is not None and object_layer.featureCount() > 0:
                for obj in object_layer.getFeatures():
                    if feedback.isCanceled():
                        raise QgsProcessingException("Canceled by user")
                    g = obj.geometry()
                    if not g or g.isEmpty():
                        continue
                    try:
                        if _geom_covers(poly_geom, g) or poly_geom.intersects(g) or poly_geom.distance(g) <= 5.0:
                            pt = g.asPoint()
                            if pt is not None:
                                refs.append(QgsPointXY(pt))
                    except Exception:
                        continue
                if refs:
                    return refs

            centroid = poly_geom.centroid()
            if centroid and not centroid.isEmpty():
                try:
                    refs.append(QgsPointXY(centroid.asPoint()))
                except Exception:
                    pass
            pos = poly_geom.pointOnSurface()
            if pos and not pos.isEmpty():
                try:
                    refs.append(QgsPointXY(pos.asPoint()))
                except Exception:
                    pass
            try:
                for ring in poly_geom.asPolygon():
                    for p in ring:
                        refs.append(QgsPointXY(p))
            except Exception:
                pass
            try:
                if hasattr(poly_geom, "asMultiPolygon"):
                    for part in poly_geom.asMultiPolygon():
                        for ring in part:
                            for p in ring:
                                refs.append(QgsPointXY(p))
            except Exception:
                pass
            return refs

        # --- Safety: align CRS, clip full roads, then make valid ---
        feedback.pushInfo("Stage 1/6: validating polygons and clipping roads")
        feedback.setProgress(5)
        polys_valid = _valid_layer(polys, context, feedback)

        if objects is not None and objects.crs() != polys_valid.crs():
            objects = as_layer(processing.run(
                "native:reprojectlayer",
                {"INPUT": objects, "TARGET_CRS": polys_valid.crs(), "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context)

        # Ensure same CRS (use polygon CRS as canonical)
        roads_aligned = roads
        if roads_aligned.crs() != polys_valid.crs():
            roads_aligned = as_layer(processing.run(
                "native:reprojectlayer",
                {"INPUT": roads_aligned, "TARGET_CRS": polys_valid.crs(), "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context)

        clip_area = as_layer(processing.run(
            "native:buffer",
            {
                "INPUT": polys_valid,
                "DISTANCE": 100.0,
                "SEGMENTS": 8,
                "DISSOLVE": True,
                "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
            },
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context)
        roads_clipped = as_layer(processing.run(
            "native:clip",
            {
                "INPUT": roads_aligned,
                "OVERLAY": clip_area,
                "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
            },
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context)
        if roads_clipped is None or roads_clipped.featureCount() == 0:
            raise QgsProcessingException(
                "Road clipping produced no lines. Check that roads and polygons overlap."
            )
        roads_valid = _valid_layer(roads_clipped, context, feedback)
        if QgsWkbTypes.geometryType(roads_valid.wkbType()) != QgsWkbTypes.LineGeometry:
            feedback.pushWarning(
                "Clipped roads were not line features. Converting them to line geometry."
            )
            roads_valid = as_layer(processing.run(
                "native:convertgeometrytype",
                {"INPUT": roads_valid, "TYPE": 1, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context)
        elif roads_valid.wkbType() == QgsWkbTypes.Unknown:
            roads_valid = as_layer(processing.run(
                "native:convertgeometrytype",
                {"INPUT": roads_valid, "TYPE": 1, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context)
        feedback.pushInfo(
            f"Clipped roads to polygon area: {roads_valid.featureCount()} features."
        )

        # Force the sink to be declared as MultiLineString regardless of what
        # wkbType() reports on the memory layer (it can return Unknown/Point after
        # a convert step, causing QGIS to display the output as a point layer).
        roads_out_fields = build_fields(THIN_PROFILES["INTERMEDIATE_LINE"])
        # Carry road classification through so downstream stages (trench layer)
        # can distinguish vehicular from walkable roads on this output.
        _carry_fields = ("fclass", "bridge", "tunnel")
        _src_road_names = {n.lower(): n for n in roads_valid.fields().names()}
        _cls_src = _src_road_names.get("fclass") or _src_road_names.get("highway")
        for _cf in _carry_fields:
            if roads_out_fields.indexOf(_cf) == -1:
                roads_out_fields.append(QgsField(_cf, QMetaType.Type.QString))
        sinkRoads, idRoads = self.parameterAsSink(
            params,
            self.OUT_CLIPPED_ROADS,
            context,
            roads_out_fields,
            QgsWkbTypes.MultiLineString,
            polys_valid.crs(),
        )
        if sinkRoads is not None:
            rid = 1
            for f in roads_valid.getFeatures():
                geom = f.geometry()
                if geom is None or geom.isEmpty():
                    continue
                # Ensure each feature written into the sink is a (Multi)LineString
                gtype = QgsWkbTypes.geometryType(geom.wkbType())
                if gtype != QgsWkbTypes.LineGeometry:
                    continue
                attrs = {
                    COMMON_FIELDS.SRC_ID: str(rid),
                    COMMON_FIELDS.STAGE: "clipped_roads",
                }
                if _cls_src is not None:
                    attrs["fclass"] = f[_cls_src]
                for _cf in ("bridge", "tunnel"):
                    if _cf in _src_road_names:
                        attrs[_cf] = f[_src_road_names[_cf]]
                nf = _thin_feature(roads_out_fields, geom, attrs)
                sinkRoads.addFeature(nf, QgsFeatureSink.FastInsert)
                rid += 1

        # Normalize roads to singleparts (cleaner intersections) + index
        roads_single = as_layer(processing.run(
            "native:multiparttosingleparts",
            {"INPUT": roads_valid, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context)

        processing.run("native:createspatialindex",
            {"INPUT": roads_single}, is_child_algorithm=True, context=context, feedback=feedback
        )

        # --- Stage 1: filter roads, extract road boundary lines, generate candidate PDPs ---
        feedback.pushInfo("Stage 2/6: filtering clipped roads and extracting road-boundary lines")
        feedback.setProgress(20)
        road_fields = {n.lower() for n in roads_single.fields().names()}
        feedback.pushInfo(f"Road layer fields: {sorted(list(road_fields))}")

        def _rebuild_expr(expr: str) -> str:
            if not expr:
                return expr
            e = expr
            if "fclass" in e.lower() and "fclass" not in road_fields and "highway" in road_fields:
                e = e.replace('"fclass"', '"highway"').replace("'fclass'", "'highway'")
            if "highway" in e.lower() and "highway" not in road_fields and "fclass" in road_fields:
                e = e.replace('"highway"', '"fclass"').replace("'highway'", "'fclass'")
            return e

        expr_try = _rebuild_expr(expr_cand) if expr_cand else ""
        filtered = None
        if expr_try:
            filtered = as_layer(processing.run(
                "native:extractbyexpression",
                {"INPUT": roads_single, "EXPRESSION": expr_try, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context)

        # Fallbacks
        def _fallback_filter(expr_text: str):
            return as_layer(processing.run(
                "native:extractbyexpression",
                {"INPUT": roads_single, "EXPRESSION": expr_text, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context)

        if filtered is None or filtered.featureCount() == 0:
            if "highway" in road_fields:
                filtered = _fallback_filter('"highway" IN (\'residential\',\'tertiary\',\'secondary\',\'unclassified\',\'living_street\',\'service\')')
            elif "fclass" in road_fields:
                filtered = _fallback_filter('"fclass" IN (\'residential\',\'tertiary\',\'secondary\',\'unclassified\',\'living_street\',\'service\')')

        if filtered is None or filtered.featureCount() == 0:
            coarse = []
            for name in ("name", "ref", "maxspeed", "layer", "bridge", "tunnel"):
                if name in road_fields:
                    coarse.append(f'"{name}" IS NOT NULL')
            coarse_expr = " OR ".join(coarse) if coarse else ""
            if coarse_expr:
                filtered = _fallback_filter(coarse_expr)

        if filtered is None or filtered.featureCount() == 0:
            # As a last-resort fallback, use all clipped roads so processing can continue.
            # This avoids hard failure when the user-supplied FILTER_CAND doesn't match attributes.
            if roads_single is not None and roads_single.featureCount() > 0:
                feedback.pushWarning(
                    "Road filter produced no candidates. Falling back to all clipped roads."
                )
                filtered = roads_single
            else:
                raise QgsProcessingException("Road filter produced no candidates. Check FILTER_CAND and road attributes.")

        # --- Exclude non-diggable road types (motorways, trunks, parking aisles, etc.) ---
        # These roads are not accessible for underground ducting, so any PDP candidates
        # that fall along them must be removed.
        _no_dig_types = (
            "'motorway'",
            "'motorway_link'",
            "'trunk'",
            "'trunk_link'",
            "'primary_link'",
            "'raceway'",
            "'road'",
            "'parking_aisle'",
            "'busway'",
            "'rest_area'",
            "'services'",
        )
        _no_dig_vals = ", ".join(_no_dig_types)
        _fld = None
        for _candidate_fld in ("fclass", "highway"):
            if _candidate_fld in road_fields:
                _fld = _candidate_fld
                break
        if _fld is not None:
            _no_dig_expr = f'"{_fld}" NOT IN ({_no_dig_vals})'
            _diggable = as_layer(processing.run(
                "native:extractbyexpression",
                {"INPUT": filtered, "EXPRESSION": _no_dig_expr, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context)
            if _diggable is not None and _diggable.featureCount() > 0:
                feedback.pushInfo(
                    f"Excluded non-diggable roads: {filtered.featureCount() - _diggable.featureCount()} removed. "
                    f"{_diggable.featureCount()} diggable roads remain."
                )
                filtered = _diggable
            else:
                feedback.pushWarning("Non-diggable exclusion removed all roads; skipping exclusion step.")
        else:
            feedback.pushWarning("No 'fclass'/'highway' field found; skipping non-diggable road exclusion.")

        # Sidewalk/road-boundary ribbon from the filtered clipped roads.
        # PER CLASS at the band rule (trench_design.kerb_offset_for: half the
        # carriageway + a footway inset), not one flat distance — a flat 3.0 m
        # left the cabinets on the road edge of every street wider than 6 m,
        # the same defect the derived pavement had.
        try:
            from ..design.trench_design import kerb_offset_for as _band_for
        except Exception:   # pragma: no cover - designer present under QGIS
            _band_for = None
        ribbon_parts = []
        by_class = {}
        if _fld is not None:
            for f in filtered.getFeatures():
                val = f[_fld]
                key = str(val).strip().lower() if val is not None else ""
                by_class.setdefault(key, "" if val is None else str(val))
        for key in sorted(by_class):
            dist = _band_for(key) if _band_for else off
            expr_val = by_class[key].replace("'", "''")
            subset = as_layer(processing.run(
                "native:extractbyexpression",
                {"INPUT": filtered, "EXPRESSION": f'"{_fld}" = \'{expr_val}\'',
                 "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context)
            if subset is None or subset.featureCount() == 0:
                continue
            ribbon_parts.append(as_layer(processing.run(
                "native:buffer",
                {"INPUT": subset, "DISTANCE": dist, "SEGMENTS": 8, "DISSOLVE": False,
                 "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context))
        if not ribbon_parts:
            ribbon_parts = [as_layer(processing.run(
                "native:buffer",
                {"INPUT": filtered, "DISTANCE": off, "SEGMENTS": 8, "DISSOLVE": False,
                 "END_CAP_STYLE": 0, "JOIN_STYLE": 0, "MITER_LIMIT": 2, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context)]
        if len(ribbon_parts) > 1:
            road_boundary_polys = as_layer(processing.run(
                "native:mergevectorlayers",
                {"LAYERS": ribbon_parts, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context)
        else:
            road_boundary_polys = ribbon_parts[0]

        road_boundary_lines = as_layer(processing.run(
            "native:polygonstolines",
            {"INPUT": road_boundary_polys, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context)

        cand = as_layer(processing.run(
            "native:pointsalonglines",
            {"INPUT": road_boundary_lines, "DISTANCE": space, "START_OFFSET": 0, "END_OFFSET": 0,
             "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context)

        cand = as_layer(processing.run(
            "native:deleteduplicategeometries",
            {"INPUT": cand, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context)

        processing.run("native:createspatialindex",
            {"INPUT": cand}, is_child_algorithm=True, context=context, feedback=feedback
        )

        # Sinks (stage 1)
        edge_fields = build_fields(THIN_PROFILES["INTERMEDIATE_POLYGON"])
        sinkE, idE = self.parameterAsSink(params, self.OUT_EDGES, context,
                                          edge_fields, road_boundary_polys.wkbType(), polys_valid.crs())
        if sinkE is not None:
            eid = 1
            for f in road_boundary_polys.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                sinkE.addFeature(_thin_feature(edge_fields, g, {
                    COMMON_FIELDS.SRC_ID: str(eid),
                    COMMON_FIELDS.STAGE: "edges",
                }))
                eid += 1

        cand_fields = build_fields(THIN_PROFILES["INTERMEDIATE_POINT"])
        sinkC, idC = self.parameterAsSink(params, self.OUT_CAND, context,
                                          cand_fields, cand.wkbType(), polys_valid.crs())
        if sinkC is not None:
            cid = 1
            for f in cand.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                sinkC.addFeature(_thin_feature(cand_fields, g, {
                    COMMON_FIELDS.SRC_ID: str(cid),
                    COMMON_FIELDS.STAGE: "candidate_pdps",
                }))
                cid += 1

        # --- Stage 2: remove points around intersections ---
        feedback.pushInfo("Stage 3/6: removing candidates on road intersections")
        feedback.setProgress(40)
        inter = as_layer(processing.run(
            "native:lineintersections",
            {"INPUT": filtered, "INTERSECT": filtered, "INPUT_FIELDS": [], "INTERSECT_FIELDS": [],
             "INTERSECT_FIELDS_PREFIX": "", "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context)

        if interbuf > 0:
            inter_buf = as_layer(processing.run(
                "native:buffer",
                {"INPUT": inter, "DISTANCE": interbuf, "SEGMENTS": 8, "DISSOLVE": False, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context)
        else:
            inter_buf = as_layer(processing.run(
                "native:extractbyexpression",
                {"INPUT": inter, "EXPRESSION": "FALSE", "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context)

        processing.run("native:createspatialindex",
            {"INPUT": inter_buf}, is_child_algorithm=True, context=context, feedback=feedback
        )

        remove = as_layer(processing.run(
            "native:extractbylocation",
            {"INPUT": cand, "PREDICATE": [0], "INTERSECT": inter_buf, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context)

        clean = (as_layer(processing.run(
            "native:difference",
            {"INPUT": cand, "OVERLAY": remove, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context) if remove.featureCount() > 0 else cand)

        # Second de-dup + optional thinning (>= spacing/2 apart)
        clean = as_layer(processing.run(
            "native:deleteduplicategeometries",
            {"INPUT": clean, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context)

        try:
            keep = []
            kept_idx = QgsSpatialIndex()
            kept_lookup = {}
            half = max(0.1, space / 2.0)
            cnt = 0
            for f in clean.getFeatures():
                if feedback.isCanceled():
                    raise QgsProcessingException("Canceled by user")
                cnt += 1
                if cnt % 200 == 0:
                    feedback.pushInfo(f"Thinning points: processed {cnt}")
                g = f.geometry()
                if not g or g.isEmpty():
                    continue
                bb = g.buffer(half, 8).boundingBox()
                hits = kept_idx.intersects(bb)
                too_close = False
                if hits:
                    for kid in hits:
                        kg = kept_lookup.get(kid)
                        if kg and kg.distance(g) < half:
                            too_close = True
                            break
                if not too_close:
                    newf = QgsFeature(f)
                    keep.append(newf)
                    kept_idx.addFeature(newf)
                    kept_lookup[newf.id()] = newf.geometry()
            if keep:
                kept_layer = QgsVectorLayer(f"Point?crs={clean.crs().authid()}", "kept", "memory")
                kp_pr = kept_layer.dataProvider()
                kp_pr.addAttributes(clean.fields())
                kept_layer.updateFields()
                kp_pr.addFeatures(keep)
                kept_layer.updateExtents()
                clean = as_layer(kept_layer, context)
        except QgsProcessingException:
            raise
        except Exception as e:
            feedback.reportError(f"Near-duplicate thinning skipped: {e}")

        processing.run("native:createspatialindex",
            {"INPUT": clean}, is_child_algorithm=True, context=context, feedback=feedback
        )

        removed_fields = build_fields(THIN_PROFILES["INTERMEDIATE_POINT"] + ["REASON"])
        sinkR, idR = self.parameterAsSink(params, self.OUT_REMOVED, context,
                                          removed_fields, remove.wkbType(), polys_valid.crs())
        if sinkR is not None and remove.featureCount() > 0:
            rid = 1
            for f in remove.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                sinkR.addFeature(_thin_feature(removed_fields, g, {
                    COMMON_FIELDS.SRC_ID: str(rid),
                    COMMON_FIELDS.STAGE: "removed_intersections",
                    "REASON": "intersection_buffer",
                }))
                rid += 1

        clean_fields = build_fields(THIN_PROFILES["INTERMEDIATE_POINT"])
        sinkClean, idClean = self.parameterAsSink(params, self.OUT_CLEAN, context,
                                                  clean_fields, clean.wkbType(), polys_valid.crs())
        if sinkClean is not None:
            clid = 1
            for f in clean.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                sinkClean.addFeature(_thin_feature(clean_fields, g, {
                    COMMON_FIELDS.SRC_ID: str(clid),
                    COMMON_FIELDS.STAGE: "clean_pdps",
                }))
                clid += 1

        # --- Stage 3: assign 1 PDP per polygon (use VALIDATED polys) ---
        feedback.pushInfo("Stage 4/6: assigning the best PDP per polygon")
        feedback.setProgress(60)
        poly_fields = polys_valid.fields()
        idx = poly_fields.indexOf(pid_field)
        if idx < 0:
            lower = {n.lower(): n for n in poly_fields.names()}
            if pid_field.lower() in lower:
                pid_field = lower[pid_field.lower()]
                idx = poly_fields.indexOf(pid_field)
            else:
                raise QgsProcessingException(f"Field '{pid_field}' not found on polygons.")

        out_fields = build_fields(THIN_PROFILES["PDP"] + ["label"])
        # Carry the homes count and full splitter plan onto each PDP (one PDP per
        # polygon), so the splitter sizing is available directly on the network layer.
        _pdp_extra = [
            ("HH", QMetaType.Type.Int), ("SPLIT_SIZE", QMetaType.Type.QString), ("SPLIT_CNT", QMetaType.Type.Int),
            ("SPLIT_UTIL", QMetaType.Type.Double), ("SPLIT_OK", QMetaType.Type.Int), ("SPL_PLAN", QMetaType.Type.QString),
            ("SPL_PORTS", QMetaType.Type.Int), ("SPL_4", QMetaType.Type.Int), ("SPL_8", QMetaType.Type.Int),
            ("SPL_16", QMetaType.Type.Int), ("SPL_32", QMetaType.Type.Int), ("SPL_64", QMetaType.Type.Int),
        ]
        for _n, _t in _pdp_extra:
            if out_fields.indexOf(_n) == -1:
                out_fields.append(QgsField(_n, _t))
        sinkA, idA = self.parameterAsSink(params, self.OUT_ASSIGNED, context, out_fields, QgsWkbTypes.Point, polys_valid.crs())

        # Partition service polygons into MFG service areas by premise load and
        # shortest road-network reach. A polygon is a premise/PDP unit, not an
        # MFG boundary: disconnected polygon geometry alone never creates a new
        # cabinet catchment.
        polygon_loads = {}
        polygon_geometries = {}
        hh_field = first_field_case_insensitive(
            polys_valid, ["HH", "SUM_HOMES", "homes", "hh_count"]
        )
        polygon_id_field = first_field_case_insensitive(
            polys_valid, ["POLYGON_ID", "polygon_id"]
        ) or pid_field
        polygon_rows = {}
        canonical_polygon_field = first_field_case_insensitive(
            polys_valid, ["POLYGON_ID", "polygon_id"]
        )
        polygon_key_by_fid = {}
        for polygon_feature in polys_valid.getFeatures():
            # Feature IDs are unique within this validated layer and avoid
            # collapsing duplicate/null source polygon labels in the partition.
            geometry = polygon_feature.geometry()
            if not geometry or geometry.isEmpty():
                continue
            polygon_id = str(polygon_feature.id())
            polygon_key_by_fid[polygon_feature.id()] = polygon_id
            try:
                household_load = int(float(polygon_feature[hh_field] or 0)) if hh_field else 0
            except (TypeError, ValueError):
                household_load = 0
            polygon_loads[polygon_id] = household_load
            polygon_geometries[polygon_id] = QgsGeometry(geometry)
            polygon_rows[polygon_id] = QgsFeature(polygon_feature)

        polygon_ids = sorted(polygon_geometries)
        polygon_index = QgsSpatialIndex()
        polygon_fids = {}
        for polygon_id in polygon_ids:
            polygon_feature = QgsFeature(polygon_rows[polygon_id])
            polygon_index.addFeature(polygon_feature)
            polygon_fids[polygon_feature.id()] = polygon_id
        # Roads are expanded to the full service radius before graph building:
        # the original 100 m polygon clip cannot answer a 3 km cabinet reach.
        mfg_service_buffer = as_layer(processing.run(
            "native:buffer",
            {"INPUT": polys_valid, "DISTANCE": 3000.0, "SEGMENTS": 8,
             "DISSOLVE": True, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context)
        mfg_route_source = as_layer(processing.run(
            "native:clip",
            {"INPUT": roads_aligned, "OVERLAY": mfg_service_buffer,
             "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            is_child_algorithm=True, context=context, feedback=feedback
        )["OUTPUT"], context)
        mfg_route_fields = {name.lower() for name in mfg_route_source.fields().names()}
        mfg_class_field = "fclass" if "fclass" in mfg_route_fields else (
            "highway" if "highway" in mfg_route_fields else None
        )
        if mfg_class_field:
            road_classes = (
                "residential", "living_street", "unclassified", "tertiary",
                "secondary", "primary", "service", "footway", "pedestrian", "path",
            )
            class_expr = ",".join("'{}'".format(value) for value in road_classes)
            mfg_route_roads = as_layer(processing.run(
                "native:extractbyexpression",
                {"INPUT": mfg_route_source,
                 "EXPRESSION": f'"{mfg_class_field}" IN ({class_expr})',
                 "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                is_child_algorithm=True, context=context, feedback=feedback
            )["OUTPUT"], context)
        else:
            feedback.pushWarning(
                "MFG road reach: no fclass/highway field; all lines in the 3 km service envelope are eligible."
            )
            mfg_route_roads = mfg_route_source

        mfg_locations, mfg_road_distances = _mfg_road_distance_matrix(
            polygon_geometries, mfg_route_roads, feedback, max_road_m=3000.0
        )
        mfg_areas = partition_mfg_service_areas(
            polygon_loads, mfg_road_distances,
            min_hh=2000, target_hh=3000, max_hh=4000, max_road_m=3000.0,
        )
        polygon_to_mfg = {
            polygon_id: area["mfg_id"]
            for area in mfg_areas
            for polygon_id in area["polygon_keys"]
        }
        feedback.pushInfo(
            "MFG service-area partition: {} catchment(s), {} total household equivalents "
            "(capacity 2,000–4,000; 3,000 m road-network reach).".format(
                len(mfg_areas), sum(polygon_loads.values())
            )
        )
        for area in mfg_areas:
            if area["review"]:
                feedback.pushWarning(
                    "{} requires review: {} HH. {}".format(
                        area["mfg_id"], area["hh_count"], area["capacity_warning"]
                    )
                )

        # Create NetworkManager to assign IDs and track relationships.
        manager = NetworkManager(polys_valid.crs(), feedback)

        clean_feats = list(clean.getFeatures())
        used = set()
        placed = 0

        try:
            pts_idx = QgsSpatialIndex()
            pts_idx.addFeatures(clean_feats)
        except Exception:
            pts_idx = None

        # Spatial index + geometry lookup of ALL polygons, so each PDP can be
        # checked against — and kept out of — every OTHER polygon's area.
        _poly_index = QgsSpatialIndex()
        _poly_geoms = {}                       # polygon feature id -> geometry
        for _pf in polys_valid.getFeatures():
            _pg = _pf.geometry()
            if _pg is None or _pg.isEmpty():
                continue
            _poly_geoms[_pf.id()] = QgsGeometry(_pg)
            _tf = QgsFeature(_pf.id())
            _tf.setGeometry(_pg)
            _poly_index.addFeature(_tf)

        def _inside_other_polygon(pt_geom, own_fid):
            """True if the point lies inside a polygon other than own_fid."""
            if pt_geom is None or pt_geom.isEmpty():
                return False
            for _fid in _poly_index.intersects(pt_geom.boundingBox()):
                if _fid == own_fid:
                    continue
                _pg = _poly_geoms.get(_fid)
                if _pg is not None and _pg.contains(pt_geom):
                    return True
            return False

        poly_count = polys_valid.featureCount()
        pidx = 0

        # Brownfield: load the existing-asset registry once for PDP reuse below.
        bf_pdp_reg = None
        try:
            from ..utils.brownfield import BrownfieldRegistry
            bf_pdp_reg = BrownfieldRegistry.load_from_project()
        except Exception:
            bf_pdp_reg = None

        for poly in polys_valid.getFeatures():
            if feedback.isCanceled():
                raise QgsProcessingException("Canceled by user")
            pidx += 1
            if pidx % 50 == 0:
                feedback.pushInfo(f"Assigning PDPS: processing polygon {pidx}/{poly_count}")
            g = poly.geometry()
            if not g or g.isEmpty():
                continue

            centroid = g.centroid()
            if not centroid or centroid.isEmpty() or not g.contains(centroid.asPoint()):
                pos = g.pointOnSurface()
                centroid = pos if pos and not pos.isEmpty() else g.centroid()
            centroid_geom = centroid
            centroid_buf = centroid_geom.buffer(centr, 8) if centr > 0 else None

            try:
                inner = g.buffer(-abs(inner_b), 8) if inner_b else g
                if not inner or inner.isEmpty():
                    inner = g
            except Exception:
                inner = g

            # Prefer canonical generated POLYGON_ID when present; fallback to user-selected field.
            pid_val = None
            try:
                if poly.fields().indexOf("POLYGON_ID") >= 0:
                    v = poly["POLYGON_ID"]
                    if v is not None and str(v).strip() != "":
                        pid_val = str(v).strip()
            except Exception:
                pid_val = None
            if pid_val is None:
                pid_val = poly[pid_field]
            refs = _polygon_reference_points(g, objects, polys_valid.crs())

            # Build a candidate pool: prefer points inside/touching the polygon,
            # fall back to a centroid-radius search, then to all unused points.
            avail = [f for f in clean_feats if f.id() not in used and f.geometry() and not f.geometry().isEmpty()]
            inside = [f for f in avail if _geom_covers(inner, f.geometry())]

            poly_candidates = inside
            if not poly_candidates and centroid_buf is not None:
                if pts_idx is not None:
                    poly_candidates = [
                        x for x in avail
                        if pts_idx.intersects(centroid_buf.boundingBox())
                        and centroid_buf.intersects(x.geometry())
                    ]
                else:
                    poly_candidates = [f for f in avail if centroid_buf.intersects(f.geometry())]

            # Widen search box to polygon bounding box if still empty
            if not poly_candidates and pts_idx is not None:
                poly_bb = g.boundingBox()
                poly_candidates = [
                    x for fid in pts_idx.intersects(poly_bb)
                    for x in avail if x.id() == fid
                ]

            # Final fallback: closest point globally
            if not poly_candidates:
                poly_candidates = avail

            chosen = None
            if poly_candidates:
                if refs:
                    # Score each candidate by total distance to all premises in this polygon
                    def _score(feat):
                        geom = feat.geometry()
                        try:
                            pt = QgsPointXY(geom.asPoint())
                            return sum(pt.distance(ref) for ref in refs)
                        except Exception:
                            return geom.distance(centroid_geom)
                    chosen = min(poly_candidates, key=_score)
                else:
                    # No premises available: pick closest to polygon centroid
                    chosen = min(poly_candidates, key=lambda f: f.geometry().distance(centroid_geom))

            if sinkA is not None:
                if chosen is not None:
                    snapped_geom = _snap_geom_to_lines(
                        chosen.geometry(), road_boundary_lines, tol_m=max(0.5, off * 0.75), crs_authid=polys_valid.crs().authid()
                    )
                    final_pdp_geom = snapped_geom if snapped_geom else chosen.geometry()
                    _src_id = str(chosen.id())

                    # ── Brownfield: reuse an existing chamber/PDP within 10 m ──
                    if bf_pdp_reg is not None and bf_pdp_reg.asset_count:
                        try:
                            new_geom, bf_aid = bf_pdp_reg.try_snap_pdp_to_chamber(
                                final_pdp_geom, max_dist=10.0
                            )
                            if bf_aid:
                                moved_m = final_pdp_geom.distance(new_geom)
                                final_pdp_geom = new_geom
                                _src_id = bf_aid
                                feedback.pushInfo(
                                    f"Polygon {pid_val}: PDP reused existing point asset "
                                    f"'{bf_aid}' ({moved_m:.1f}m away)."
                                )
                        except Exception as e:
                            # Best-effort: a failure here must never break PDP placement.
                            feedback.pushWarning(
                                f"Polygon {pid_val}: brownfield PDP snap skipped ({e!r})."
                            )
                else:
                    # Guarantee a PDP for EVERY polygon — but keep it on a
                    # STREET. The old fallback used the polygon's point-on-
                    # surface, which for a residential block is the middle of
                    # the block: measured on the reference run that is how
                    # PDP00030 landed 117.8 m from the nearest road (and
                    # PDP00017/18/19 27-38 m), cabinets with no street to be
                    # built on. The nearest point on the nearest allowed street
                    # is the honest position; the point-on-surface is kept only
                    # for an area with no street at all.
                    street_pt, street_d = _nearest_street_point(centroid_geom, filtered)
                    if street_pt is not None:
                        final_pdp_geom = street_pt
                        _src_id = ""
                        feedback.pushInfo(
                            f"Polygon {pid_val}: no sidewalk candidate point — PDP "
                            f"placed on the nearest street ({street_d:.1f} m away)."
                        )
                    else:
                        final_pdp_geom = QgsGeometry(centroid_geom)
                        _src_id = ""
                        feedback.pushWarning(
                            f"Polygon {pid_val}: no candidate PDP point and no street "
                            "in the area — PDP placed at polygon point-on-surface."
                        )

                # --- Keep the PDP inside its OWN polygon, and NEVER inside another ---
                # Preferred: inside its own polygon. Acceptable: a clean nearby road
                # point that sits in no polygon at all. Forbidden: inside a neighbouring
                # polygon — when that happens, relocate to the nearest clean road point
                # that is out of every other polygon; failing that, drop it onto the
                # own polygon's point-on-surface (always inside, never in a neighbour
                # since polygons do not overlap).
                own_fid = poly.id()
                if (not g.contains(final_pdp_geom)) and _inside_other_polygon(final_pdp_geom, own_fid):
                    alt_geom = None
                    search_bb = g.boundingBox()
                    search_bb.grow(max(centr, off) + space)
                    near_ids = set(pts_idx.intersects(search_bb)) if pts_idx is not None else None
                    alt_pool = []
                    for cf in clean_feats:
                        if cf.id() in used:
                            continue
                        if chosen is not None and cf.id() == chosen.id():
                            continue
                        if near_ids is not None and cf.id() not in near_ids:
                            continue
                        cg = cf.geometry()
                        if cg is None or cg.isEmpty():
                            continue
                        # inside own is best; otherwise it must be in NO other polygon
                        if g.contains(cg) or not _inside_other_polygon(cg, own_fid):
                            alt_pool.append(cf)
                    if alt_pool:
                        def _alt_score(cf):
                            cg = cf.geometry()
                            inside_own = 0 if g.contains(cg) else 1
                            try:
                                pt = QgsPointXY(cg.asPoint())
                                d = sum(pt.distance(ref) for ref in refs) if refs else cg.distance(centroid_geom)
                            except Exception:
                                d = cg.distance(centroid_geom)
                            return (inside_own, d)
                        best_alt = min(alt_pool, key=_alt_score)
                        alt_geom = QgsGeometry(best_alt.geometry())
                        chosen = best_alt
                        _src_id = str(best_alt.id())
                    if alt_geom is not None:
                        final_pdp_geom = alt_geom
                        feedback.pushInfo(
                            f"Polygon {pid_val}: PDP fell inside a neighbouring polygon — "
                            "relocated to a clean nearby road point outside all other polygons."
                        )
                    else:
                        pos = g.pointOnSurface()
                        final_pdp_geom = pos if (pos and not pos.isEmpty()) else QgsGeometry(centroid_geom)
                        chosen = None
                        _src_id = ""
                        feedback.pushInfo(
                            f"Polygon {pid_val}: PDP fell inside a neighbouring polygon and no clean "
                            "road point was free — placed inside the polygon (point-on-surface)."
                        )

                # Find addresses inside this polygon for the manager.
                # Use the full polygon `g`, NOT `inner` (the INNER_BUF-shrunk
                # geometry is only for PDP placement): addresses within
                # INNER_BUF of the polygon edge would otherwise never be
                # registered and end up with no PDP_ID / no distribution trench.
                addresses_in_poly = []
                if objects is not None:
                    for obj_feat in objects.getFeatures():
                        if obj_feat.geometry() and _geom_covers(g, obj_feat.geometry()):
                            addresses_in_poly.append(obj_feat.id())

                # Register with manager. Preserve source polygon ID when available
                # so PDP/polygon/object sync all use the same polygon identity.
                polygon_id, pdp_id = manager.register_polygon(
                    polygon_geom=g,
                    pdp_geom=final_pdp_geom,
                    addresses_in_polygon=addresses_in_poly,
                    polygon_id_override=pid_val,
                )
                partition_key = polygon_key_by_fid.get(poly.id())
                assigned_mfg_id = polygon_to_mfg.get(partition_key)
                if assigned_mfg_id is None:
                    raise QgsProcessingException(
                        f"Polygon {polygon_id} has no MFG partition assignment."
                    )
                manager.get_registry().set_mfg_for_polygon(polygon_id, assigned_mfg_id)

                # Write PDP feature using assigned IDs
                nf = QgsFeature(out_fields)
                nf.setGeometry(final_pdp_geom)
                nf[COMMON_FIELDS.POLYGON_ID] = str(polygon_id)
                nf[COMMON_FIELDS.PDP_ID] = str(pdp_id)
                nf[COMMON_FIELDS.MFG_ID] = assigned_mfg_id
                nf[COMMON_FIELDS.NODE_TYPE] = "PDP"
                nf[COMMON_FIELDS.SRC_ID] = _src_id
                nf[COMMON_FIELDS.STAGE] = "assigned_pdp"
                nf["label"] = f"Network_{polygon_id}"

                # Splitter plan for this PDP, derived from the polygon's homes.
                _hh = 0
                _hh_field = first_field_case_insensitive(
                    polys_valid, ["HH", "SUM_HOMES", "homes", "hh_count"]
                )
                if _hh_field:
                    try:
                        _v = poly[_hh_field]
                        _hh = int(_v) if _v not in (None, "") else 0
                    except Exception:
                        _hh = 0
                _plan = plan_splitters(_hh)
                _c = _plan["counts"]
                for _k, _v in [
                    ("HH", _hh),
                    ("SPLIT_SIZE", f"1:{_plan['primary']}" if _plan["primary"] else "-"),
                    ("SPLIT_CNT", _plan["total"]),
                    ("SPLIT_UTIL", _plan["util"]),
                    ("SPLIT_OK", _plan["ok"]),
                    ("SPL_PLAN", _plan["label"]),
                    ("SPL_PORTS", _plan["ports"]),
                    ("SPL_4", _c.get(4, 0)), ("SPL_8", _c.get(8, 0)), ("SPL_16", _c.get(16, 0)),
                    ("SPL_32", _c.get(32, 0)), ("SPL_64", _c.get(64, 0)),
                ]:
                    if out_fields.indexOf(_k) != -1:
                        nf[_k] = _v

                sinkA.addFeature(nf)

                if chosen is not None:
                    used.add(chosen.id())
                placed += 1
            else:
                feedback.pushInfo(f"No PDP sink available for polygon FID {poly.id()}")

        feedback.pushInfo(f"Assigned {placed} PDPs to {poly_count} polygons (every polygon has a PDP).")

        # Update object layer with POLYGON_ID and PDP_ID via manager
        if objects is not None:
            feedback.pushInfo("Updating object layer with assigned polygon and PDP IDs")
            # Addresses were registered using QgsFeature.id(), so sync in feature-id mode.
            _polygon_id_field = canonical_polygon_field or polygon_id_field
            mfg_by_polygon_id = {
                str(polygon_rows[key][_polygon_id_field]): area["mfg_id"]
                for area in mfg_areas
                for key in area["polygon_keys"]
            }

            sync_stats = manager.update_object_layer(
                objects, addr_id_field="id", use_feature_id=True, mfg_id=None,
                mfg_by_polygon=mfg_by_polygon_id,
            )
            if sync_stats.get("updated", 0) == sync_stats.get("expected", 0) and sync_stats.get("commit_ok", False):
                feedback.pushInfo(
                    "Object attributes synced: {updated}/{expected} addresses updated.".format(
                        updated=sync_stats.get("updated", 0),
                        expected=sync_stats.get("expected", 0),
                    )
                )
            else:
                feedback.pushWarning(
                    "Object sync incomplete: updated={updated}, expected={expected}, "
                    "unmatched={unmatched}, commit_ok={commit_ok}".format(
                        updated=sync_stats.get("updated", 0),
                        expected=sync_stats.get("expected", 0),
                        unmatched=sync_stats.get("unmatched", 0),
                        commit_ok=sync_stats.get("commit_ok", False),
                    )
                )
        else:
            feedback.pushWarning("No object layer provided; POLYGON_ID and PDP_ID not assigned to addresses.")

        # Store manager for potential downstream use (best-effort only).
        # Must never abort processing, otherwise Stage 5 (MFG) will not run.
        try:
            proj = QgsProject.instance()
            if hasattr(proj, "setCustomProperty"):
                proj.setCustomProperty("network_registry", manager.get_registry())
            else:
                feedback.pushWarning("Project custom properties API not available; skipping in-memory registry export.")
        except Exception as e:
            feedback.pushWarning(f"Skipping registry export due to API/runtime limitation: {e}")


        # Place one MFG per road-reachable capacity catchment; polygon IDs stay
        # as independent premise/PDP identities and map to the serving MFG.
        feedback.pushInfo("Stage 5/6: placing MFG points for capacity/reach service areas")
        feedback.setProgress(80)
        out_mfg_id = None
        mfg_geometries = {}
        mfg_service_geometries = {}
        if do_mfg:
            for area in mfg_areas:
                parts = [polygon_geometries[key] for key in area["polygon_keys"]]
                member_union = QgsGeometry.unaryUnion(parts)
                if member_union is None or member_union.isEmpty():
                    raise QgsProcessingException(
                        f"{area['mfg_id']}: could not build its service-area geometry."
                    )
                # MFG serves an area, not the individual premise/polygon
                # footprints. The convex envelope is one service-boundary
                # feature per MFG; the original premise polygons remain
                # unchanged in Polygons.gpkg.
                area_geometry = member_union.convexHull()
                if area_geometry is None or area_geometry.isEmpty() or (
                    QgsWkbTypes.geometryType(area_geometry.wkbType())
                    != QgsWkbTypes.PolygonGeometry
                ):
                    raise QgsProcessingException(
                        f"{area['mfg_id']}: service-area boundary is not polygonal."
                    )
                area_geometry.convertToMultiType()
                mfg_service_geometries[area["mfg_id"]] = area_geometry

                seed_geometry = polygon_geometries[area["seed_polygon"]]
                seed_road_node = mfg_locations.get(area["seed_polygon"])
                if seed_road_node is not None:
                    # This is the actual network projection used to measure the
                    # 3 km reach, so keep the cabinet at that same road node.
                    final_geom = QgsGeometry.fromPointXY(
                        QgsPointXY(seed_road_node[0], seed_road_node[1])
                    )
                else:
                    final_geom, road_offset = _nearest_street_point(
                        seed_geometry.pointOnSurface(), mfg_route_roads
                    )
                    if final_geom is None:
                        final_geom = seed_geometry.pointOnSurface()
                        feedback.pushWarning(
                            f"{area['mfg_id']}: no eligible road for its serving-area anchor."
                        )
                    elif road_offset is not None and road_offset > 100.0:
                        feedback.pushWarning(
                            f"{area['mfg_id']}: seed polygon is {road_offset:.1f} m from an eligible road."
                        )
                mfg_geometries[area["mfg_id"]] = final_geom

            out_fields_m = build_fields(THIN_PROFILES["MFG"])
            for _name, _type in (
                ("HH_COUNT", QMetaType.Type.Int),
                ("TARGET_HH", QMetaType.Type.Int),
                ("MIN_HH", QMetaType.Type.Int),
                ("MAX_HH", QMetaType.Type.Int),
                ("RANGE_M", QMetaType.Type.Double),
                ("CAPACITY_STATUS", QMetaType.Type.QString),
                ("CAPACITY_WARNING", QMetaType.Type.QString),
                ("REVIEW", QMetaType.Type.Int),
            ):
                if out_fields_m.indexOf(_name) < 0:
                    out_fields_m.append(QgsField(_name, _type))
            sinkM, out_mfg_id = self.parameterAsSink(
                params, self.OUT_MFG, context, out_fields_m, QgsWkbTypes.Point, polys_valid.crs()
            )
            if sinkM is None:
                raise QgsProcessingException(self.invalidSinkError(params, self.OUT_MFG))
            sinkArea, out_mfg_areas_id = self.parameterAsSink(
                params, self.OUT_MFG_AREAS, context, out_fields_m,
                QgsWkbTypes.MultiPolygon, polys_valid.crs()
            )
            for index, area in enumerate(mfg_areas, 1):
                mfg_id = manager.register_mfg(
                    mfg_geometries[area["mfg_id"]],
                    mfg_id_override=area["mfg_id"],
                )

                mfg_f = QgsFeature(out_fields_m)
                mfg_f.setGeometry(mfg_geometries[area["mfg_id"]])
                mfg_f[COMMON_FIELDS.MFG_ID] = mfg_id
                mfg_f[COMMON_FIELDS.NODE_TYPE] = "MFG"
                mfg_f[COMMON_FIELDS.SRC_ID] = str(index)
                mfg_f[COMMON_FIELDS.STAGE] = "mfg"
                mfg_f["HH_COUNT"] = int(area["hh_count"])
                mfg_f["TARGET_HH"] = int(area["target_hh"])
                mfg_f["MIN_HH"] = int(area["min_hh"])
                mfg_f["MAX_HH"] = int(area["max_hh"])
                mfg_f["RANGE_M"] = (
                    float(area["range_m"]) if area["range_m"] is not None else None
                )
                mfg_f["CAPACITY_STATUS"] = area["capacity_status"]
                mfg_f["CAPACITY_WARNING"] = area["capacity_warning"]
                mfg_f["REVIEW"] = int(area["review"])
                sinkM.addFeature(mfg_f, QgsFeatureSink.FastInsert)
                if sinkArea is not None:
                    area_f = QgsFeature(out_fields_m)
                    area_f.setGeometry(mfg_service_geometries[area["mfg_id"]])
                    for field in out_fields_m.names():
                        if field == "MFG_ID":
                            area_f[field] = mfg_id
                        elif field == "NODE_TYPE":
                            area_f[field] = "MFG_SERVICE_AREA"
                        elif field == "SRC_ID":
                            area_f[field] = str(index)
                        elif field == "STAGE":
                            area_f[field] = "mfg_service_area"
                        elif field == "HH_COUNT":
                            area_f[field] = int(area["hh_count"])
                        elif field == "TARGET_HH":
                            area_f[field] = int(area["target_hh"])
                        elif field == "MIN_HH":
                            area_f[field] = int(area["min_hh"])
                        elif field == "MAX_HH":
                            area_f[field] = int(area["max_hh"])
                        elif field == "RANGE_M":
                            area_f[field] = (
                                float(area["range_m"]) if area["range_m"] is not None else None
                            )
                        elif field == "CAPACITY_STATUS":
                            area_f[field] = area["capacity_status"]
                        elif field == "CAPACITY_WARNING":
                            area_f[field] = area["capacity_warning"]
                        elif field == "REVIEW":
                            area_f[field] = int(area["review"])
                    sinkArea.addFeature(area_f, QgsFeatureSink.FastInsert)
            feedback.pushInfo("Placed {} MFG points and matching service-area boundaries.".format(len(mfg_areas)))

        # ------------------------------------------------------------------
        # Final Object Layer: a NEW layer (input objects are left untouched
        # apart from the legacy in-place sync) carrying every source column
        # plus canonical POLYGON_ID / PDP_ID / MFG_ID from the registry.
        # ------------------------------------------------------------------
        id_final_obj = None
        if objects is None:
            feedback.pushInfo(
                "INPUT_OBJECTS not provided \u2014 Final_Object_Layer will not be generated "
                "(no source objects to append POLYGON_ID / PDP_ID / MFG_ID onto)."
            )
        else:
            try:
                src_fields = objects.fields()
                fo_fields = QgsFields()
                for _fld in src_fields:
                    fo_fields.append(QgsField(_fld.name(), _fld.type(), _fld.typeName(), _fld.length(), _fld.precision()))
                for _extra in (COMMON_FIELDS.POLYGON_ID, COMMON_FIELDS.PDP_ID, COMMON_FIELDS.MFG_ID):
                    if fo_fields.lookupField(_extra) < 0:
                        fo_fields.append(QgsField(_extra, QMetaType.Type.QString))

                # FIX: parameterAsSink silently returns (None, None) for an
                # optional FeatureSink when no destination is provided (typical
                # for headless processing.run() invocations). Inject a default
                # destination so the QGIS framework creates a (memory) sink
                # and id_final_obj is non-null, keeping OUT_FINAL_OBJECTS in
                # the returned dict.
                _dfo = params.get(self.OUT_FINAL_OBJECTS)
                if not _dfo:
                    params[self.OUT_FINAL_OBJECTS] = QgsProcessing.TEMPORARY_OUTPUT
                    feedback.pushInfo(
                        "Final_Object_Layer destination not provided; falling back to a "
                        "temporary memory sink so the output is always generated."
                    )

                sinkFO, id_final_obj = self.parameterAsSink(
                    params, self.OUT_FINAL_OBJECTS, context, fo_fields, objects.wkbType(), objects.crs()
                )
                if sinkFO:
                    # address feature-id -> (POLYGON_ID, PDP_ID) from the registry
                    _lookup = {}
                    _reg = manager.get_registry()
                    for _pid in _reg.polygon_ids():
                        _net = _reg.get_network(_pid)
                        for _aid in _net["addresses"]:
                            _lookup[_aid] = (
                                _pid, _net["pdp_id"], _net.get("mfg_id")
                            )

                    _poly_i = fo_fields.lookupField(COMMON_FIELDS.POLYGON_ID)
                    _pdp_i = fo_fields.lookupField(COMMON_FIELDS.PDP_ID)
                    _mfg_i = fo_fields.lookupField(COMMON_FIELDS.MFG_ID)
                    _linked = _unlinked = 0
                    for _of in objects.getFeatures():
                        _nf = QgsFeature(fo_fields)
                        _nf.setGeometry(_of.geometry())
                        for _fld in src_fields:
                            _nf[_fld.name()] = _of[_fld.name()]
                        _match = _lookup.get(_of.id())
                        if _match:
                            _nf[_poly_i] = _match[0]
                            _nf[_pdp_i] = _match[1]
                            _nf[_mfg_i] = _match[2]
                            _linked += 1
                        else:
                            # explicit NULL beats a stale source value
                            _nf[_poly_i] = None
                            _nf[_pdp_i] = None
                            _unlinked += 1
                        if not _match:
                            _nf[_mfg_i] = None
                        sinkFO.addFeature(_nf)
                    feedback.pushInfo(
                        f"Final_Object_Layer written: {_linked} objects linked to PDPs/MFGs, "
                        f"{_unlinked} unlinked."
                    )
                    if _unlinked:
                        feedback.pushWarning(
                            f"\u26a0\ufe0f {_unlinked} object(s) have no POLYGON_ID/PDP_ID in Final_Object_Layer \u2014 "
                            "they fall outside every service polygon."
                        )
                else:
                    # Sink creation refused despite the fallback \u2014 surface it
                    # loudly instead of silently swallowing the failure.
                    feedback.reportError(
                        "Final_Object_Layer sink could not be created even with a "
                        "TEMPORARY_OUTPUT fallback \u2014 output will be absent."
                    )
            except Exception as _e:
                # Surface real failures (CRS mismatch, schema problems, etc.)
                # instead of silently dropping the output.
                feedback.pushWarning(
                    f"Final_Object_Layer could not be written: "
                    f"{type(_e).__name__}: {_e}"
                )
                id_final_obj = None

        feedback.pushInfo(f"PDPs placed: {placed} / {polys_valid.featureCount()}")
        feedback.pushInfo("Stage 6/6: writing outputs")
        feedback.setProgress(100)
        feedback.pushInfo(manager.summary())

        out = {
            self.OUT_EDGES:    idE,
            self.OUT_CAND:     idC,
            self.OUT_REMOVED:  idR,
            self.OUT_CLEAN:    idClean,
            self.OUT_ASSIGNED: idA,
        }
        if idRoads:
            out[self.OUT_CLIPPED_ROADS] = idRoads
        if out_mfg_id is not None:
            out[self.OUT_MFG] = out_mfg_id
        if locals().get("out_mfg_areas_id") is not None:
            out[self.OUT_MFG_AREAS] = out_mfg_areas_id
        if id_final_obj is not None:
            out[self.OUT_FINAL_OBJECTS] = id_final_obj
        return out
