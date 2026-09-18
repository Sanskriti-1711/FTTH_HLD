# -*- coding: utf-8 -*-
"""
Duct Manager — duct_layer.py (embedded)
Runs:
  11) Feeder Ducts (virtual nodes, prefix bundling)
  12) Distribution Ducts (Strict PDP→HH; no polygons)

QGIS 3.44 / Python 3.12 compatible

Notes:
- No provider IDs used. Both algorithms are embedded below and invoked directly.
- Sidewalk L/R auto-detection: if not provided, tries to find layers in the project by common names.
- If 'Final Tangent Trenches' is not set for Distribution, we reuse the Feeder network lines input.

Dependencies:
- distribution step requires 'networkx' in the QGIS Python env.
  (On Linux: <qgis-python> -m pip install networkx)
"""
import heapq
import math, os
from collections import defaultdict
from qgis.PyQt.QtCore import QCoreApplication, QMetaType
from qgis.core import (
    QgsProcessing,QgsCoordinateReferenceSystem,
    QgsProcessingAlgorithm,QgsProcessingParameterFeatureSource,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterField,QgsFeatureRequest,
    QgsProcessingParameterCrs,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterVectorDestination,
    QgsProcessingParameterFeatureSink,
    QgsProcessingException,QgsSpatialIndex,
    QgsFeatureSink,QgsFeature,QgsMessageLog,
    QgsFields, QgsField, QgsWkbTypes,
    QgsGeometry, QgsPointXY, QgsProject,
    QgsProcessingUtils, QgsSymbol,
    QgsVectorLayer, QgsCoordinateTransform,
)
from qgis import processing
from ..utils.geom import round_key_xy, geom_substring, edges_to_geom, path_len, is_prefix, lcp_len
from ..utils.snap import snap_point_create_virtual
from ..utils.graph import add_edge, dijkstra_with_parents, reconstruct_path
from ..utils.style_utils import color_for_index, TRUNK_COLOR, apply_color_renderer, distribution_color
from ..utils.fields import first_field_case_insensitive
import math as _math
from collections import defaultdict as _dd, OrderedDict as _OD
try:
    import networkx as nx
except Exception:
    nx = None

from ..utils.layer_ops import fix_geometries, reproject_if_needed, snap_layer, linemerge_layer
from ..utils.string_utils import normalize_key
from ..utils.geom_utils import geom_str_from_wkb

# -------------------------------
# Helpers for the wrapper
# -------------------------------
def _tr(s: str) -> str:
    return QCoreApplication.translate("duct_layer", s)

def _find_layer_by_partial_name(names):
    if not names:
        return None
    lname = [n.lower() for n in names]
    for lyr in QgsProject.instance().mapLayers().values():
        n = (lyr.name() or "").lower()
        if any(tag in n for tag in lname):
            return lyr
    return None


def _ensure_output_parent_dir(out_spec):
    if not isinstance(out_spec, str):
        return
    spec = out_spec.strip()
    if not spec or spec.lower().startswith("memory:"):
        return

    base_path = spec.split("|", 1)[0].strip()
    if not base_path:
        return

    parent = os.path.dirname(base_path)
    if parent:
        os.makedirs(parent, exist_ok=True)


# ======================================================================
# 11) Feeder Ducts (embedded from your alg_11_feeder_ducts.py)
# ======================================================================
# (Only tiny edits: keep class/name; no provider IDs.)

class AlgFeederDuctsNoSplit(QgsProcessingAlgorithm):
    def shortHelpString(self):
        return 'Runs the {} algorithm.'.format(self.displayName())

    def createInstance(self):
        return AlgFeederDuctsNoSplit()
    def _make_sink(self, p, key, context, feedback, fields, wkb, crs):
        """
        Create a QgsFeatureSink for key `key`.
        - If the caller provided a string URI in p[key], use QgsProcessingUtils.createFeatureSink.
        - Otherwise, fall back to parameterAsSink (works when run via processing.run).
        Returns (sink, out_id).
        """
        from qgis.core import QgsProcessingUtils
        out_spec = p.get(key, None)
        # If caller passed a destination (e.g., memory:, GPKG path, etc.)
        if isinstance(out_spec, str) and out_spec.strip():
            try:
                _ensure_output_parent_dir(out_spec)
                sink, out_id = QgsProcessingUtils.createFeatureSink(
                    out_spec, context, fields, wkb, crs
                )
                if sink is not None:
                    return sink, out_id
            except Exception as e:
                try:
                    feedback.reportError(f"Failed to create sink from dest '{out_spec}': {e}")
                except Exception:
                    pass
        # Fallback to framework-provided sink (works when run via processing.run)
        sink, out_id = self.parameterAsSink(p, key, context, fields, wkb, crs)
        return sink, out_id

    # Inputs
    L_NET   = "NETWORK_LINES"
    L_MFG   = "MFG_POINTS"
    L_PDP   = "PDP_POINTS"
    F_PDPID = "FIELD_PDP_ID"
    F_MFGID = "FIELD_MFG_ID"

    # Parameters
    SNAP_TOL = "SNAP_TOLERANCE_M"
    NODE_TOL = "NODE_SNAP_TOL_M"
    END_EPS  = "ENDPOINT_EPS"
    INT_EPS  = "INTERSECT_EPS"
    INC_TRUNK= "INCLUDE_TRUNK"
    MAX_K    = "MAX_PDPS_PER_DUCT"
    ADD_STYLE= "ADD_STYLED_TO_PROJECT"
    DEFAULT_MAX_PDPS_PER_DUCT = 4

    # Output
    O_DUCTS  = "OUT_FEEDER_DUCTS"  # (optional) rename to "Feeder_Duct" if you want OneClick auto-pickup

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.L_NET, "Network Lines (e.g., Final_Trenches)", [QgsProcessing.TypeVectorLine]
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.L_MFG, "MFG Points", [QgsProcessing.TypeVectorPoint]
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.L_PDP, "PDP Points", [QgsProcessing.TypeVectorPoint]
        ))
        self.addParameter(QgsProcessingParameterField(
            self.F_PDPID, "Field on PDPs: pdp_id (optional)",
            parentLayerParameterName=self.L_PDP, type=QgsProcessingParameterField.Any, optional=True
        ))
        self.addParameter(QgsProcessingParameterField(
            self.F_MFGID, "Field on MFGs: mfg_id (optional)",
            parentLayerParameterName=self.L_MFG, type=QgsProcessingParameterField.Any, optional=True
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.SNAP_TOL, "Snap tolerance (m) for point→line", QgsProcessingParameterNumber.Double,
            defaultValue=1.5, minValue=0.0
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.NODE_TOL, "Node rounding tolerance (m)", QgsProcessingParameterNumber.Double,
            defaultValue=0.50, minValue=0.0
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.END_EPS, "Endpoint epsilon (m)", QgsProcessingParameterNumber.Double,
            defaultValue=0.25, minValue=0.0
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.INT_EPS, "Intersection epsilon (m)", QgsProcessingParameterNumber.Double,
            defaultValue=0.25, minValue=0.0
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.INC_TRUNK, "Include trunk (longest common prefix)", defaultValue=True
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_K, "Max PDPs per duct", QgsProcessingParameterNumber.Integer,
            defaultValue=self.DEFAULT_MAX_PDPS_PER_DUCT, minValue=2, maxValue=16
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ADD_STYLE, "Add categorized style (by color) to project", defaultValue=True
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.O_DUCTS, "Feeder_Ducts (bundled)", QgsProcessing.TypeVectorLine
        ))

    def processAlgorithm(self, p, context, feedback):
        net = p.get(self.L_NET)
        mfg = p.get(self.L_MFG)
        pdp = p.get(self.L_PDP)
        # --- Robust resolve: accept feature sources and project layers transparently
        def _resolve(v):
            # 1) Already a live QgsVectorLayer?
            try:
                from qgis.core import QgsVectorLayer
                if isinstance(v, QgsVectorLayer) and v.isValid():
                    return v
            except Exception:
                pass
            
            # 2) Processing feature-source or feature-source definition → save to memory
            try:
                from qgis.core import QgsProcessingFeatureSource, QgsProcessingFeatureSourceDefinition
                if (isinstance(v, QgsProcessingFeatureSource) or
                    isinstance(v, QgsProcessingFeatureSourceDefinition)):
                    return processing.run(
                        "native:savefeatures",
                        {"INPUT": v, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                        context=context, feedback=feedback
                    )["OUTPUT"]
            except Exception:
                pass
            
            # 3) Try resolving by layer id/name via Processing utils
            try:
                from qgis.core import QgsProcessingUtils
                cand = QgsProcessingUtils.mapLayerFromString(str(v), context)
                if cand and cand.isValid():
                    return cand
            except Exception:
                pass
            
            # 4) Try as OGR path/URI
            try:
                from qgis.core import QgsVectorLayer
                lyr = QgsVectorLayer(str(v), "resolved", "ogr")
                if lyr.isValid():
                    return lyr
            except Exception:
                pass
            
            return None
        
        net = _resolve(net); mfg = _resolve(mfg); pdp = _resolve(pdp)
        missing = [n for n, x in (("network", net), ("mfg", mfg), ("pdp", pdp)) if x is None]
        if missing:
            raise QgsProcessingException(f"Missing required layers: {', '.join(missing)}.")

        f_pdp = (self.parameterAsString(p, self.F_PDPID, context) or "").strip()
        f_mfg = (self.parameterAsString(p, self.F_MFGID, context) or "").strip()

        t_duct_total = _math.time() if hasattr(_math, "time") else None
        import time as _time
        t_duct_total = _time.time()
        snap_tol = float(self.parameterAsDouble(p, self.SNAP_TOL, context))
        node_tol = float(self.parameterAsDouble(p, self.NODE_TOL, context))
        end_eps  = float(self.parameterAsDouble(p, self.END_EPS,  context))
        int_eps  = float(self.parameterAsDouble(p, self.INT_EPS,  context))
        include_trunk = bool(self.parameterAsBool(p, self.INC_TRUNK, context))
        max_k   = int(self.parameterAsInt(p, self.MAX_K, context))
        add_style = bool(self.parameterAsBool(p, self.ADD_STYLE, context))

        # --- guard against 0 or negative values ---
        EPS = 1e-6
        snap_tol = max(snap_tol, EPS)
        node_tol = max(node_tol,  EPS)   # critical: used as a divisor in utils/snap.py
        end_eps  = max(end_eps,  EPS)
        int_eps  = max(int_eps,  EPS)

        crs = net.crs()

        t0 = _time.time()
        net_fix = processing.run("native:fixgeometries",
                                 {"INPUT": net, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                                 context=context, feedback=feedback)["OUTPUT"]
        feedback.pushInfo(f"  [timing] Duct geometry fix: {_time.time() - t0:.3f}s")
        t0 = _time.time()
        net_single = processing.run("native:multiparttosingleparts",
                                    {"INPUT": net_fix, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                                    context=context, feedback=feedback)["OUTPUT"]

        feedback.pushInfo(f"  [timing] Duct multipart split: {_time.time() - t0:.3f}s")
        t0 = _time.time()
        try:
            inter_pts = processing.run("native:lineintersections",
                {"INPUT": net_single, "INTERSECT": net_single, "INPUT_FIELDS": [], "INTERSECT_FIELDS": [], "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                context=context, feedback=feedback)["OUTPUT"]
        except Exception:
            inter_pts = processing.run("qgis:lineintersections",
                {"INPUT": net_single, "INTERSECT": net_single, "INPUT_FIELDS": [], "INTERSECT_FIELDS": [], "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                context=context, feedback=feedback)["OUTPUT"]

        feedback.pushInfo(f"  [timing] Duct line intersections: {_time.time() - t0:.3f}s")
        t0 = _time.time()
        inter_pts = processing.run("native:deleteduplicategeometries",
                                   {"INPUT": inter_pts, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                                   context=context, feedback=feedback)["OUTPUT"]
        feedback.pushInfo(f"  [timing] Duct duplicate intersections: {_time.time() - t0:.3f}s")
        feedback.pushInfo("  [timing] Feeder duct graph preparation complete")

        seg_index = QgsSpatialIndex(net_single.getFeatures())
        fid_to_geom, fid_to_len = {}, {}
        for f in net_single.getFeatures():
            g = f.geometry()
            if not g or g.isEmpty():
                continue
            fid_to_geom[f.id()] = g
            fid_to_len[f.id()]  = g.length()

        # Initialize breaks with start/end and intersections
        fid_breaks = defaultdict(list)
        fid_break_xy = defaultdict(dict)
        for fid, L in fid_to_len.items():
            geom = fid_to_geom[fid]
            p0 = geom.interpolate(0.0).asPoint()
            pL = geom.interpolate(L).asPoint()
            fid_breaks[fid].extend([0.0, L])
            fid_break_xy[fid][0.0] = (p0.x(), p0.y())
            fid_break_xy[fid][L]   = (pL.x(), pL.y())

        for fp in inter_pts.getFeatures():
            pg = fp.geometry()
            if not pg or pg.isEmpty():
                continue
            pt = pg.asPoint()
            rect = pg.buffer(snap_tol * 2.0, 8).boundingBox()
            for fid in seg_index.intersects(rect):
                g = fid_to_geom.get(fid)
                if not g:
                    continue
                if g.distance(pg) <= int_eps:
                    L = fid_to_len[fid]
                    d = g.lineLocatePoint(pg)
                    if d <= 1e-6 or (L - d) <= 1e-6:
                        continue
                    fid_breaks[fid].append(d)
                    fid_break_xy[fid][d] = (pt.x(), pt.y())

        feedback.pushInfo(f"  [timing] Duct graph indexing: {_time.time() - t0:.3f}s")
        t0 = _time.time()
        # Snap MFG/PDP and register mid-segment breaks
        mfg_nodes, mfg_label = {}, {}
        for fm in mfg.getFeatures():
            nk, fid = snap_point_create_virtual(
                fm.geometry(), seg_index, fid_to_geom, fid_to_len,
                fid_breaks, fid_break_xy, snap_tol, node_tol, end_eps
            )
            if nk is not None:
                mfg_nodes[fm.id()] = (nk, fid)
                lab = fm.attribute(f_mfg) if f_mfg else fm.id()
                mfg_label[fm.id()] = str(lab)

        pdp_nodes, pdp_label = {}, {}
        for fp in pdp.getFeatures():
            nk, fid = snap_point_create_virtual(
                fp.geometry(), seg_index, fid_to_geom, fid_to_len,
                fid_breaks, fid_break_xy, snap_tol, node_tol, end_eps
            )
            if nk is not None:
                pdp_nodes[fp.id()] = (nk, fid)
                lab = fp.attribute(f_pdp) if f_pdp else fp.id()
                pdp_label[fp.id()] = str(lab)

        if not mfg_nodes or not pdp_nodes:
            raise QgsProcessingException("No valid snapped MFG / PDP points found on the network.")

        feedback.pushInfo(f"  [timing] Duct MFG/PDP snapping: {_time.time() - t0:.3f}s")
        t0 = _time.time()
        # Build graph from split edges
        adj = defaultdict(list)
        edge_geom, edge_len = {}, {}

        for fid, breaks in fid_breaks.items():
            geom = fid_to_geom.get(fid)
            L = fid_to_len.get(fid, 0.0)
            if not geom or L <= 0:
                continue
            uniq = sorted(set([b for b in breaks if 0.0 <= b <= L]))
            if len(uniq) < 2:
                continue
            coords_at = {}
            for d in uniq:
                c = fid_break_xy[fid].get(d)
                if c is None:
                    p = geom.interpolate(d).asPoint()
                    c = (p.x(), p.y())
                coords_at[d] = c
            for i in range(len(uniq) - 1):
                d0, d1 = uniq[i], uniq[i + 1]
                if (d1 - d0) <= 1e-6:
                    continue
                p0 = coords_at[d0]
                p1 = coords_at[d1]
                u = round_key_xy(p0[0], p0[1], node_tol)
                v = round_key_xy(p1[0], p1[1], node_tol)
                sub = geom_substring(geom, d0, d1)
                add_edge(adj, edge_geom, edge_len, u, v, sub)

        feedback.pushInfo(f"  [timing] Duct graph construction: {_time.time() - t0:.3f}s")
        t0 = _time.time()
        # Label nodes by nearest MFG (multi-source Dijkstra front)
        label_dist = {}
        heap = []
        for mfg_id, (node_k, _) in mfg_nodes.items():
            if node_k in adj:
                heapq.heappush(heap, (0.0, str(node_k), node_k, mfg_id))
        while heap:
            dist_u, _tie_u, u, lab = heapq.heappop(heap)
            if (u in label_dist) and (dist_u > label_dist[u][0] + 1e-9):
                continue
            if u not in label_dist:
                label_dist[u] = (dist_u, lab)
            for v, seg_id, w in adj.get(u, []):
                cand = dist_u + w
                if (v not in label_dist) or (cand + 1e-9 < label_dist[v][0]) or \
                   (abs(cand - label_dist[v][0]) <= 1e-9 and str(lab) < str(label_dist[v][1])):
                    heapq.heappush(heap, (cand, str(seg_id), v, lab))

        pdp_to_mfg = {}
        for pid, (nk, _) in pdp_nodes.items():
            if nk in label_dist:
                pdp_to_mfg[pid] = label_dist[nk][1]

        # Prepare output
        fields = QgsFields()
        fields.append(QgsField("mfg_id",    QMetaType.Type.QString))
        fields.append(QgsField("pdp_ids",   QMetaType.Type.QString))
        fields.append(QgsField("pdp_count", QMetaType.Type.Int))
        fields.append(QgsField("capacity_total", QMetaType.Type.Int))
        fields.append(QgsField("capacity_used", QMetaType.Type.Int))
        fields.append(QgsField("capacity_spare", QMetaType.Type.Int))
        fields.append(QgsField("part",      QMetaType.Type.QString))
        fields.append(QgsField("color",     QMetaType.Type.QString))
        fields.append(QgsField("edge_cnt",  QMetaType.Type.Int))
        fields.append(QgsField("length_m",  QMetaType.Type.Double))
        fields.append(QgsField("duct_idx",  QMetaType.Type.Int))

        sink, out_id = self._make_sink(
            p, self.O_DUCTS, context, feedback,
            fields, QgsWkbTypes.MultiLineString, net.crs()
        )
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(p, self.O_DUCTS))

        made = 0
        # Build ducts per MFG
        for mfg_fid, (mnode, _) in mfg_nodes.items():
            if mnode not in adj:
                continue
            assigned_pids = [pid for pid, lab in pdp_to_mfg.items() if lab == mfg_fid]
            if not assigned_pids:
                continue

            dist, parent = dijkstra_with_parents(mnode, adj)

            pid_to_path = {}
            for pid in assigned_pids:
                nk, _ = pdp_nodes.get(pid, (None, None))
                if nk is None:
                    continue
                path = reconstruct_path(parent, nk, mnode)
                if not path:
                    continue
                pid_to_path[pid] = path
            if not pid_to_path:
                continue

            all_paths = sorted([pid_to_path[pid] for pid in pid_to_path], key=len, reverse=True)
            k = lcp_len(all_paths)

            if include_trunk and k > 0:
                gtr = edges_to_geom(edge_geom, all_paths[0][:k])
                if gtr and not gtr.isEmpty():
                    ft = QgsFeature(fields)
                    ft.setGeometry(gtr)
                    ft["mfg_id"]    = str(mfg_fid)
                    ft["pdp_ids"]   = ""
                    ft["pdp_count"] = 0
                    ft["capacity_total"] = max_k
                    ft["capacity_used"] = 0
                    ft["capacity_spare"] = max_k
                    ft["part"]      = "trunk"
                    ft["color"]     = TRUNK_COLOR
                    ft["edge_cnt"]  = int(k)
                    ft["length_m"]  = float(path_len(edge_len, all_paths[0][:k]))
                    ft["duct_idx"]  = -1
                    sink.addFeature(ft, QgsFeatureSink.FastInsert)
                    made += 1

            pid_suffix = {pid: pid_to_path[pid][k:] for pid in pid_to_path}
            def plen(s): return path_len(edge_len, s)

            # Group by common-prefix branches
            branches = []
            for pid, suf in pid_suffix.items():
                placed = False
                for b in branches:
                    bs = b["rep"]
                    if is_prefix(suf, bs) or is_prefix(bs, suf):
                        if plen(suf) > plen(bs):
                            b["rep"] = suf
                        b["pids"].append(pid)
                        placed = True
                        break
                if not placed:
                    branches.append({"rep": suf, "pids": [pid]})

            # Within each branch, form ducts up to max_k PDPs along farthest path
            for bi, b in enumerate(branches):
                branch_color = color_for_index(bi)
                cand = [(pid, pid_suffix[pid], plen(pid_suffix[pid])) for pid in b["pids"]]
                remaining = {pid for pid, _, _ in cand}
                duct_idx = 0
                while remaining:
                    far_pid = max(remaining, key=lambda p_: next(L for (pp, _, L) in cand if pp == p_))
                    far_suf = next(s for (pp, s, _) in cand if pp == far_pid)
                    far_len = next(L for (pp, _, L) in cand if pp == far_pid)
                    on_way = []
                    for pid, suf, L in sorted(cand, key=lambda x: x[2]):
                        if pid in remaining and pid != far_pid and is_prefix(suf, far_suf):
                            on_way.append(pid)
                        if len(on_way) >= (max_k - 1):
                            break
                    group = [far_pid] + on_way
                    for ppid in group:
                        remaining.discard(ppid)

                    geom = edges_to_geom(edge_geom, far_suf)
                    if not geom or geom.isEmpty():
                        continue

                    fb = QgsFeature(fields)
                    fb.setGeometry(geom)
                    fb["mfg_id"]    = str(mfg_fid)
                    fb["pdp_ids"]   = ",".join(sorted(str(pid) for pid in group))
                    fb["pdp_count"] = int(len(group))
                    fb["capacity_total"] = max_k
                    fb["capacity_used"] = int(len(group))
                    fb["capacity_spare"] = max(0, max_k - len(group))
                    fb["part"]      = f"branch{bi}_duct{duct_idx}"
                    fb["color"]     = branch_color
                    fb["edge_cnt"]  = int(len(far_suf))
                    fb["length_m"]  = float(far_len)
                    fb["duct_idx"]  = int(duct_idx)
                    sink.addFeature(fb, QgsFeatureSink.FastInsert)
                    made += 1
                    duct_idx += 1

        feedback.pushInfo(f"  [timing] Feeder duct routing/grouping: {_time.time() - t0:.3f}s")
        feedback.pushInfo(f"✅ Feeder ducts created: {made}")
        feedback.pushDebugInfo(f"Edges: {len(edge_geom)} Nodes: {len(adj)} MFGs: {len(mfg_nodes)} PDPs: {len(pdp_nodes)}")

        if add_style:
            apply_color_renderer(out_id, "Feeder_Ducts", "color")

        if sink:
            del sink

        return {self.O_DUCTS: out_id}

    def name(self): return "feeder_ducts_nosplit"
    def displayName(self): return "11) Feeder Ducts (virtual nodes, prefix bundling)"
    def group(self): return "Duct Manager"
    def groupId(self): return "duct_manager"

# ======================================================================
# 12) Distribution Ducts (embedded from your alg_12_distribution_ducts.py)
# ======================================================================

class AlgDistributionDucts(QgsProcessingAlgorithm):
    # Inputs
    L_PDP    = "PDP_POINTS"
    L_HH     = "OBJECT_POINTS"
    L_LEFT   = "SIDEWALK_LEFT"
    L_RIGHT  = "SIDEWALK_RIGHT"
    L_TAN    = "TANGENT_TRENCHES"

    # Fields
    F_PDP_ON_PDP  = "FIELD_PDP_ON_PDP"
    F_HH_ID       = "FIELD_HH_ID"
    F_PDP_ON_HH   = "FIELD_PDP_ON_HH"

    # Options
    CRS_TGT   = "TARGET_CRS"
    DENSE_M   = "DENSIFY_INTERVAL_M"
    HEAL_M    = "HEALING_THRESHOLD_M"
    SNAP_M    = "MAX_SNAP_DIST_M"
    MAX_HH    = "MAX_HH_PER_DUCT"
    ADD_STYLE = "ADD_CATEGORIZED_STYLE"

    # Output
    O_DUCTS   = "OUT_DISTRIBUTION_DUCTS"

    def createInstance(self): return AlgDistributionDucts()
    def name(self): return "distribution_ducts"
    def displayName(self): return "12) Distribution Ducts (strict PDP match, no polygons)"
    def group(self): return "Duct Manager"
    def groupId(self): return "duct_manager"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(self.L_PDP,  "PDP Points", [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterVectorLayer(self.L_HH,   "Object/HH Points (must have HH ID and PDP ID)", [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterVectorLayer(self.L_LEFT, "Sidewalk Left (lines)", [QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterVectorLayer(self.L_RIGHT,"Sidewalk Right (lines)", [QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterVectorLayer(self.L_TAN,  "Final Tangent Trenches (optional)", [QgsProcessing.TypeVectorLine], optional=True))
        self.addParameter(QgsProcessingParameterField(self.F_PDP_ON_PDP, "Field on PDPs: PDP ID", parentLayerParameterName=self.L_PDP, type=QgsProcessingParameterField.Any))
        self.addParameter(QgsProcessingParameterField(self.F_HH_ID,      "Field on Objects: HH ID (label on output)", parentLayerParameterName=self.L_HH, type=QgsProcessingParameterField.Any))
        self.addParameter(QgsProcessingParameterField(self.F_PDP_ON_HH,  "Field on Objects: PDP ID (strict match)", parentLayerParameterName=self.L_HH, type=QgsProcessingParameterField.Any))
        self.addParameter(QgsProcessingParameterCrs(self.CRS_TGT, "Target processing CRS (meters recommended)", defaultValue=QgsProject.instance().crs()))
        self.addParameter(QgsProcessingParameterNumber(self.DENSE_M, "Densify interval (m)", QgsProcessingParameterNumber.Double, defaultValue=2.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(self.HEAL_M,  "Graph healing threshold (m)", QgsProcessingParameterNumber.Double, defaultValue=3.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(self.SNAP_M,  "Max snap distance to graph (m)", QgsProcessingParameterNumber.Double, defaultValue=50.0, minValue=0.1))
        self.addParameter(QgsProcessingParameterNumber(self.MAX_HH,  "Max HH per duct", QgsProcessingParameterNumber.Integer, defaultValue=10, minValue=2, maxValue=100))
        self.addParameter(QgsProcessingParameterBoolean(self.ADD_STYLE,"Add categorized style to project", defaultValue=True))
        self.addParameter(QgsProcessingParameterFeatureSink(self.O_DUCTS, "Distribution_Ducts (per PDP; strict PDP match; ≤HH cap)", QgsProcessing.TypeVectorLine))

    # ---- helpers (trimmed to essentials) ----
    @staticmethod
    def _densify(geom, step_m):
        try:
            return geom.densifyByDistance(step_m) if step_m and step_m > 0 else geom
        except Exception:
            return geom

    @staticmethod
    def _line_parts(geom):
        if not geom or geom.isEmpty(): return []
        return ([ [QgsPointXY(p) for p in part] for part in geom.asMultiPolyline() ]
                if geom.isMultipart() else
                [ [QgsPointXY(p) for p in geom.asPolyline()] ])

    @staticmethod
    def _add_lines_to_graph(G, layer, step_m):
        for f in layer.getFeatures():
            g = f.geometry()
            if not g or g.isEmpty(): continue
            g = AlgDistributionDucts._densify(g, step_m)
            for line in AlgDistributionDucts._line_parts(g):
                for i in range(len(line)-1):
                    a, b = line[i], line[i+1]
                    if a == b: continue
                    w = _math.hypot(a.x()-b.x(), a.y()-b.y())
                    if w <= 0: continue
                    G.add_edge((a.x(), a.y()), (b.x(), b.y()), weight=w)

    @staticmethod
    def _segment_endpoints_near_point(geom, pt_xy, step_m):
        g = AlgDistributionDucts._densify(geom, step_m)
        best = (float("inf"), None)
        for line in AlgDistributionDucts._line_parts(g):
            if len(line) < 2: continue
            lg = QgsGeometry.fromPolylineXY(line)
            try:
                dist, _, after_idx, _ = lg.closestSegmentWithContext(pt_xy)
            except Exception:
                continue
            if dist < best[0]:
                i2 = max(1, min(after_idx, len(line)-1)); i1 = i2-1
                a, b = line[i1], line[i2]
                best = (dist, ((a.x(), a.y()), (b.x(), b.y())))
        return best[1]

    @staticmethod
    def _bridge_intersections(G, A, B, step_m):
        if A is None or B is None: return 0
        idxB = QgsSpatialIndex(B.getFeatures())
        made = 0
        for af in A.getFeatures():
            ag = af.geometry()
            if not ag or ag.isEmpty(): continue
            for bid in idxB.intersects(ag.boundingBox()):
                bg = B.getFeature(bid).geometry()
                if not bg or bg.isEmpty() or not ag.intersects(bg): continue
                inter = ag.intersection(bg)
                if not inter or inter.isEmpty(): continue
                if QgsWkbTypes.geometryType(inter.wkbType()) != QgsWkbTypes.PointGeometry: continue
                pts = inter.asMultiPoint() if QgsWkbTypes.isMultiType(inter.wkbType()) else [inter.asPoint()]
                for pt in pts:
                    pxy = QgsPointXY(pt)
                    ends_a = AlgDistributionDucts._segment_endpoints_near_point(ag, pxy, step_m)
                    ends_b = AlgDistributionDucts._segment_endpoints_near_point(bg, pxy, step_m)
                    if not ends_a or not ends_b: continue
                    px, py = pxy.x(), pxy.y()
                    for ex, ey in (ends_a + ends_b):
                        w = _math.hypot(px-ex, py-ey)
                        if w > 0: G.add_edge((px,py), (ex,ey), weight=w); made += 1
        return made
    def _make_sink(self, p, key, context, feedback, fields, wkb, crs):
        """
        Create a QgsFeatureSink for key `key`.
        - If the caller provided a string URI in p[key], use QgsProcessingUtils.createFeatureSink.
        - Otherwise, fall back to parameterAsSink (works when run via processing.run).
        Returns (sink, out_id).
        """
        from qgis.core import QgsProcessingUtils
        out_spec = p.get(key, None)
        # If caller passed a destination (e.g., memory:, GPKG path, etc.)
        if isinstance(out_spec, str) and out_spec.strip():
            try:
                _ensure_output_parent_dir(out_spec)
                sink, out_id = QgsProcessingUtils.createFeatureSink(
                    out_spec, context, fields, wkb, crs
                )
                if sink is not None:
                    return sink, out_id
            except Exception as e:
                try:
                    feedback.reportError(f"Failed to create sink from dest '{out_spec}': {e}")
                except Exception:
                    pass
        # Fallback to framework-provided sink (works when run via processing.run)
        sink, out_id = self.parameterAsSink(p, key, context, fields, wkb, crs)
        return sink, out_id
    
    def processAlgorithm(self, p, context, feedback):
        if nx is None:
            raise QgsProcessingException("networkx is required (pip install into the QGIS Python environment).")

        def _reproject_if_needed(layer, target_crs):
            if not layer: return None
            if not target_crs or not target_crs.isValid():
                target_crs = QgsProject.instance().crs() or QgsCoordinateReferenceSystem("EPSG:3857")
            return (layer if layer.crs() == target_crs else
                    processing.run("native:reprojectlayer",
                                   {"INPUT": layer, "TARGET_CRS": target_crs, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                                   context=context, feedback=feedback)["OUTPUT"])

                # --- resolve layers and fields robustly ---
        # DuctLayer may call us directly and pass live QgsVectorLayer objects in `p`.
        # We first try `parameterAsVectorLayer`; if that returns None, fall back to
        # whatever raw value is present in the parameters dict.
        from qgis.core import QgsVectorLayer

        def _resolve_layer(key, current):
            # If parameterAsVectorLayer succeeded, keep it
            if current is not None:
                return current
            raw = p.get(key)
            try:
                if isinstance(raw, QgsVectorLayer) and raw.isValid():
                    return raw
            except Exception:
                pass
            return current  # still None → will be caught by missing_bits below

        pdps   = self.parameterAsVectorLayer(p, self.L_PDP,   context)
        objs   = self.parameterAsVectorLayer(p, self.L_HH,    context)
        left   = self.parameterAsVectorLayer(p, self.L_LEFT,  context)
        right  = self.parameterAsVectorLayer(p, self.L_RIGHT, context)
        tang   = self.parameterAsVectorLayer(p, self.L_TAN,   context)

        # fallback for direct `processAlgorithm` calls with live layers
        pdps   = _resolve_layer(self.L_PDP,   pdps)
        objs   = _resolve_layer(self.L_HH,    objs)
        left   = _resolve_layer(self.L_LEFT,  left)
        right  = _resolve_layer(self.L_RIGHT, right)
        tang   = _resolve_layer(self.L_TAN,   tang)

        fld_pdp_pdp = (self.parameterAsString(p, self.F_PDP_ON_PDP,  context) or "").strip()
        fld_hh_id   = (self.parameterAsString(p, self.F_HH_ID,       context) or "").strip()
        fld_pdp_obj = (self.parameterAsString(p, self.F_PDP_ON_HH,   context) or "").strip()

        crs_t   = self.parameterAsCrs(p, self.CRS_TGT,  context)
        dense_m = float(self.parameterAsDouble(p, self.DENSE_M,  context))
        heal_m  = float(self.parameterAsDouble(p, self.HEAL_M,   context))
        snap_m  = float(self.parameterAsDouble(p, self.SNAP_M,   context))
        max_hh  = int(self.parameterAsInt(p, self.MAX_HH, context))

        missing_bits = []
        if pdps is None:  missing_bits.append("PDPs layer")
        if objs  is None: missing_bits.append("Objects layer")
        if left  is None: missing_bits.append("Sidewalk_Left")
        if right is None: missing_bits.append("Sidewalk_Right")
        if not fld_pdp_pdp: missing_bits.append("PDP field on PDPs")
        if not fld_pdp_obj: missing_bits.append("PDP field on Objects")
        if not fld_hh_id:   missing_bits.append("HH field on Objects")
        if missing_bits:
            raise QgsProcessingException("Distribution ducts: missing → " + ", ".join(missing_bits))


        pdps_t  = _reproject_if_needed(pdps,  crs_t)
        objs_t  = _reproject_if_needed(objs,  crs_t)
        left_t  = _reproject_if_needed(left,  crs_t)
        right_t = _reproject_if_needed(right, crs_t)
        tang_t  = _reproject_if_needed(tang,  crs_t) if tang else None

        # Build spatial indexes for side classification
        idx_left = QgsSpatialIndex(left_t.getFeatures()) if left_t else None
        idx_right = QgsSpatialIndex(right_t.getFeatures()) if right_t else None

        # Build graph
        feedback.pushInfo("🔧 Building sidewalk+tangent graph …")
        G = nx.Graph()
        self._add_lines_to_graph(G, left_t,  dense_m)
        self._add_lines_to_graph(G, right_t, dense_m)
        if tang_t:
            self._add_lines_to_graph(G, tang_t, dense_m)

        # Bridge at intersections
        bridges  = self._bridge_intersections(G, left_t,  right_t, dense_m)
        if tang_t:
            bridges += self._bridge_intersections(G, left_t,  tang_t, dense_m)
            bridges += self._bridge_intersections(G, right_t, tang_t, dense_m)
            bridges += self._bridge_intersections(G, tang_t, tang_t, dense_m)
        feedback.pushInfo(f"🔗 Bridges added: {bridges}")

        # Heal small gaps
        feedback.pushInfo(f"🩹 Healing gaps ≤ {heal_m} m …")
        healed = 0
        if heal_m and heal_m > 0:
            nodes = list(G.nodes)
            cell = heal_m
            buckets = _dd(list)
            for (x, y) in nodes:
                buckets[(int(x//cell), int(y//cell))].append((x, y))
            nbrs = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,0),(0,1),(1,-1),(1,0),(1,1)]
            for (ix, iy), grp in buckets.items():
                for dx, dy in nbrs:
                    other = buckets.get((ix+dx, iy+dy), [])
                    for a in grp:
                        for b in other:
                            if a >= b: continue
                            d = _math.hypot(a[0]-b[0], a[1]-b[1])
                            if 0 < d <= heal_m and not G.has_edge(a, b):
                                G.add_edge(a, b, weight=d); healed += 1
        feedback.pushInfo(f"📈 Graph: {G.number_of_nodes()} nodes / {G.number_of_edges()} edges (healed: {healed})")

        # Node index for nearest-node snap (fast)
        node_layer = QgsVectorLayer(f"Point?crs={crs_t.authid()}", "_graph_nodes", "memory")
        prv = node_layer.dataProvider()
        prv.addAttributes([QgsField("nid", QMetaType.Type.Int)]); node_layer.updateFields()
        nid2node, feats = {}, []
        for i, n in enumerate(G.nodes):
            f = QgsFeature(node_layer.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(n[0], n[1])))
            f["nid"] = i; feats.append(f); nid2node[i] = n
        if feats:
            prv.addFeatures(feats); node_layer.updateExtents()
        idx_nodes = QgsSpatialIndex(node_layer.getFeatures())

        def nearest_node(pt_xy: QgsPointXY):
            for r in [snap_m, snap_m*2, snap_m*5, snap_m*10, None]:
                cand = []
                if r is None:
                    cand = list(G.nodes)
                else:
                    rect = QgsGeometry.fromPointXY(pt_xy).buffer(r, 8).boundingBox()
                    for fid in idx_nodes.intersects(rect):
                        cand.append(nid2node[node_layer.getFeature(fid)["nid"]])
                if not cand: continue
                best, bestd2 = None, float("inf")
                for n in cand:
                    dx, dy = pt_xy.x() - n[0], pt_xy.y() - n[1]
                    d2 = dx*dx + dy*dy
                    if d2 < bestd2:
                        best, bestd2 = n, d2
                if best is not None:
                    return best
            return None

        # Prepare output
        fields = QgsFields()
        fields.append(QgsField("PDP_ID",    QMetaType.Type.QString))
        fields.append(QgsField("duct_idx",  QMetaType.Type.Int))
        fields.append(QgsField("hh_ids",    QMetaType.Type.QString))
        fields.append(QgsField("hh_count",  QMetaType.Type.Int))
        fields.append(QgsField("length_m",  QMetaType.Type.Double))
        fields.append(QgsField("side",      QMetaType.Type.QString))
        fields.append(QgsField("duct_uid",  QMetaType.Type.Int))
        fields.append(QgsField("color",     QMetaType.Type.QString))

        sink, out_id = self._make_sink(
            p, self.O_DUCTS, context, feedback,
            fields, QgsWkbTypes.LineString, crs_t
        )
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(p, self.O_DUCTS))
        

        # Side classifier by proximity to the left/right layers
        def _min_dist_to_layer(pt_xy: QgsPointXY, layer: QgsVectorLayer, idx: QgsSpatialIndex, search_r: float) -> float:
            if not layer or not idx:
                return 1e12
            pt_g = QgsGeometry.fromPointXY(pt_xy)
            rect = pt_g.buffer(search_r, 8).boundingBox()
            best = 1e12
            for fid in idx.intersects(rect):
                try:
                    g = layer.getFeature(fid).geometry()
                    if not g or g.isEmpty():
                        continue
                    d = g.distance(pt_g)
                    if d < best:
                        best = d
                except Exception:
                    continue
            return best

        def side_of_point(pt_xy: QgsPointXY) -> str:
            # use snap_m as a reasonable search radius (fallback to larger if needed)
            dl = _min_dist_to_layer(pt_xy, left_t, idx_left, snap_m)
            dr = _min_dist_to_layer(pt_xy, right_t, idx_right, snap_m)
            # if both huge (no sidewalks nearby), default Right to keep behavior deterministic
            if dl >= 1e11 and dr >= 1e11:
                return "R"
            return "L" if dl <= dr else "R"

        feedback.pushInfo("📋 Indexing PDPs by ID …")
        pdp_map = _OD()
        for f in pdps_t.getFeatures():
            v = f.attribute(fld_pdp_pdp)
            if v is None: continue
            pid = str(v).strip()
            if not pid or pid in pdp_map: continue
            pt = f.geometry().asPoint()
            n = nearest_node(QgsPointXY(pt))
            if not n: continue
            # lightweight connection PDP point to nearest node
            G.add_edge(n, (pt.x(), pt.y()), weight=0.01)
            pdp_map[pid] = (pt, n)

        if not pdp_map:
            raise QgsProcessingException("No PDPs could be snapped to the network.")

        feedback.pushInfo("🧩 Grouping HH by PDP ID …")
        hh_by_pid = _dd(list)
        hh_feat_cache = {}
        for f in objs_t.getFeatures():
            pid_val = f.attribute(fld_pdp_obj)
            if pid_val is None: continue
            pid = str(pid_val).strip()
            if pid not in pdp_map:
                continue
            g = f.geometry()
            if not g or g.isEmpty(): continue
            pt = g.asMultiPoint()[0] if g.isMultipart() else g.asPoint()
            n = nearest_node(QgsPointXY(pt))
            if not n: continue
            hh_by_pid[pid].append((f.id(), n, pt))
            hh_feat_cache[f.id()] = f

        state_uid = 0
        total = 0

        for pid, items in hh_by_pid.items():
            if not items: continue
            pdp_pt, pdp_node = pdp_map[pid]
            try:
                lengths, paths = nx.single_source_dijkstra(G, pdp_node, weight="weight")
            except Exception:
                lengths = nx.single_source_shortest_path_length(G, pdp_node)
                paths   = nx.single_source_shortest_path(G, pdp_node)

            # Reachable HH, record side by proximity to left/right
            reachable = []
            for hid, n, pt in items:
                if n in paths:
                    reachable.append((hid, n, pt, side_of_point(QgsPointXY(pt))))

            if not reachable:
                feedback.pushInfo(f"[Warn] PDP_ID={pid}: no reachable HH; skipped.")
                continue

            # Split by side
            for side_name in ("L", "R"):
                cand = [(hid, n, pt) for (hid, n, pt, s) in reachable if s == side_name]
                if not cand:
                    continue

                # Build (hid, path, length) list
                pl = []
                for hid, n, pt in cand:
                    seq = paths[n]
                    Lm = float(sum(G[u][v]["weight"] for u, v in zip(seq[:-1], seq[1:])))
                    pl.append((hid, seq, Lm))

                # Greedy grouping: farthest path + on-way HHs (prefix) up to max_hh
                remaining = {hid for (hid, _, _) in pl}
                index_map = {hid: (seq, Lm) for (hid, seq, Lm) in pl}
                duct_idx = 0
                while remaining:
                    # choose farthest
                    far_hid = max(remaining, key=lambda h: index_map[h][1])
                    far_seq, far_len = index_map[far_hid]
                    # collect on-way HHs
                    on_way = []
                    for hid in sorted(list(remaining - {far_hid}), key=lambda h: index_map[h][1]):
                        seq, Lm = index_map[hid]
                        if is_prefix(seq, far_seq):
                            on_way.append(hid)
                        if len(on_way) >= (max_hh - 1):
                            break
                    group = [far_hid] + on_way
                    for hid in group:
                        remaining.discard(hid)

                    # Create ONE feature per group: geometry = farthest path
                    geom = QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in far_seq])
                    if not geom or geom.isEmpty():
                        continue

                    f = QgsFeature(fields)
                    f.setGeometry(geom)
                    f["PDP_ID"]    = pid
                    f["duct_idx"]  = duct_idx
                    f["hh_ids"]    = ",".join(str(h) for h in group)
                    f["hh_count"]  = len(group)
                    f["length_m"]  = far_len
                    f["side"]      = side_name
                    f["duct_uid"]  = state_uid
                    f["color"]     = distribution_color(side_name, duct_idx)
                    sink.addFeature(f, QgsFeatureSink.FastInsert)
                    total += 1
                    state_uid += 1
                    duct_idx += 1

        feedback.pushInfo(f"✅ Distribution ducts created: {total}")

        if bool(self.parameterAsBool(p, self.ADD_STYLE, context)):
            apply_color_renderer(out_id, "Distribution_Ducts", "color")

        if sink:
            del sink

        return {self.O_DUCTS: out_id}


# ======================================================================
# 13) Combined wrapper — Duct Layer (Feeder + Distribution)
# ======================================================================

class DuctLayer(QgsProcessingAlgorithm):
    # Parameter keys
    P_NETWORK = "NETWORK_LINES"
    P_MFG     = "MFG_POINTS"
    P_PDP     = "PDP_POINTS"
    P_PDP_ID  = "PDP_ID"
    P_MFG_ID  = "MFG_ID"
    P_OBJECTS = "OBJECT_POINTS"
    P_HH_ID   = "HH_ID"
    P_OBJ_PDP = "OBJ_PDP_ID"
    P_SIDE_L  = "SIDEWALK_LEFT"
    P_SIDE_R  = "SIDEWALK_RIGHT"
    P_FINAL   = "FINAL_TANGENT_TRENCHES"
    P_CRS     = "TARGET_CRS"
    P_PSEUDO  = "PSEUDO_OBJECT_POINTS"
    P_GARDEN  = "GARDEN_TRENCHES"
    # New: cable layers (optional).  When provided, ducts are bundled per
    # route from the actual planned cables; otherwise the legacy duct
    # builders run unchanged.
    P_FEEDER_CABLES = "FEEDER_CABLES"
    P_DIST_CABLES   = "DIST_CABLES"
    O_FEEDER  = "OUT_FEEDER_DUCTS"
    O_DISTR   = "OUT_DISTRIBUTION_DUCTS"
    O_DROP    = "OUT_DROP_DUCTS"
    O_COUPLE  = "OUT_COUPLEURS"   # couplers at pseudo → object duct connections
    # Per-route ducts (one feature per {ways}-way duct).  The published
    # feeder/distribution layers carry ONE component per tier, so the route
    # features are written here instead: the chamber stage counts them as
    # "distinct ducts" at a junction, which is what places the chambers.
    O_FEEDER_RUNS = "OUT_FEEDER_DUCT_RUNS"
    O_DIST_RUNS   = "OUT_DISTRIBUTION_DUCT_RUNS"

    def createInstance(self): return DuctLayer()
    def name(self): return "05_duct_layer"
    def displayName(self): return "Generate Ducts"
    def group(self): return "05 Duct Layer"
    def groupId(self): return "05_duct_layer"

    # Parameter surface slimmed 2026-07-03: the four ID-field pickers are
    # auto-detected from canonical names (PDP_ID / MFG_ID / ADDR_ID) and the
    # target CRS is fixed to the pipeline standard EPSG:25833.
    DEFAULT_CRS_AUTHID = "EPSG:25833"

    def initAlgorithm(self, config=None):
        # Feeder inputs
        self.addParameter(QgsProcessingParameterVectorLayer(self.P_NETWORK, "Network Lines (e.g. Final_Trenches)", [QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterVectorLayer(self.P_MFG, "MFG Points", [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterVectorLayer(self.P_PDP, "PDP Points (PDP_ID auto-detected)", [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterVectorLayer(self.P_OBJECTS, "Object/HH Points (ADDR_ID / PDP_ID auto-detected)", [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterVectorLayer(self.P_SIDE_L, "Sidewalk Left (lines)", [QgsProcessing.TypeVectorLine], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(self.P_SIDE_R, "Sidewalk Right (lines)", [QgsProcessing.TypeVectorLine], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(self.P_FINAL, "Final Tangent Trenches (optional)", [QgsProcessing.TypeVectorLine], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(self.P_PSEUDO, "Pseudo Object/HH Points on Footway (optional; distribution duct ends)", [QgsProcessing.TypeVectorPoint], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(self.P_GARDEN, "Garden Trenches (optional; drop-duct source)", [QgsProcessing.TypeVectorLine], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(self.P_FEEDER_CABLES, "Feeder Cables (optional; one 4-way duct per route)", [QgsProcessing.TypeVectorLine], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(self.P_DIST_CABLES, "Distribution Cables (optional; one 2-way duct per co-route)", [QgsProcessing.TypeVectorLine], optional=True))
        self.addParameter(QgsProcessingParameterVectorDestination(self.O_FEEDER, "Feeder_Ducts"))
        self.addParameter(QgsProcessingParameterVectorDestination(self.O_DISTR, "Distribution_Ducts"))
        self.addParameter(QgsProcessingParameterVectorDestination(self.O_DROP, "Drop_Ducts (small; pseudo → object)"))
        self.addParameter(QgsProcessingParameterVectorDestination(self.O_COUPLE, "Coupleurs (pseudo → object connection points)", optional=True))
        self.addParameter(QgsProcessingParameterVectorDestination(
            self.O_FEEDER_RUNS, "Feeder_Ducts — per-route runs (chamber inputs)", optional=True))
        self.addParameter(QgsProcessingParameterVectorDestination(
            self.O_DIST_RUNS, "Distribution_Ducts — per-route runs (chamber inputs)", optional=True))

    def _build_drop_ducts(self, p, context, feedback, out_spec,
                          garden_lyr, pseudo_lyr, obj_lyr, crs,
                          coupler_out_spec=None):
        """Small 'drop' ducts that connect each pseudo object/HH point on the
        footway to the object/building itself.

        Primary source: the garden trenches (they already span object → footway
        and carry PDP_ID / ADDR_ID / POLYGON_ID / MFG_ID).  Fallback: straight
        lines between pseudo point and object joined by address id.  Emits an
        empty layer when nothing is available so downstream stages stay stable.

        When ``coupler_out_spec`` is provided, ALSO emits one COUPLER point per
        drop duct at the pseudo (footway) end — the joint where the drop duct
        meets the distribution duct (HLD review: mark every pseudo ↔ object
        duct connection with a coupler, own layer + ids).

        Returns (drop_layer_id, coupler_layer_id).
        """
        if not out_spec:
            return None, None

        fields = QgsFields()
        for nm, t in (
            ("PDP_ID", QMetaType.Type.QString),
            ("ADDR_ID", QMetaType.Type.QString),
            ("HH_ID", QMetaType.Type.QString),
            ("POLYGON_ID", QMetaType.Type.QString),
            ("MFG_ID", QMetaType.Type.QString),
            ("DUCT_TYPE", QMetaType.Type.QString),
            ("LENGTH_M", QMetaType.Type.Double),
            ("SIDE", QMetaType.Type.QString),
            ("DUCT_UID", QMetaType.Type.Int),
        ):
            fields.append(QgsField(nm, t))

        sink, out_id = QgsProcessingUtils.createFeatureSink(
            out_spec, context, fields, QgsWkbTypes.LineString, crs)
        if sink is None:
            return None, None

        # Coupler sink (optional output).
        c_fields = QgsFields()
        for nm, t in (
            ("coupler_id", QMetaType.Type.QString),
            ("COUPLER_TYPE", QMetaType.Type.QString),
            ("PDP_ID", QMetaType.Type.QString),
            ("ADDR_ID", QMetaType.Type.QString),
            ("HH_ID", QMetaType.Type.QString),
            ("POLYGON_ID", QMetaType.Type.QString),
            ("MFG_ID", QMetaType.Type.QString),
            ("DUCT_UID", QMetaType.Type.Int),
            ("SIDE", QMetaType.Type.QString),
        ):
            c_fields.append(QgsField(nm, t))
        c_sink, c_id = None, None
        if coupler_out_spec:
            try:
                c_sink, c_id = QgsProcessingUtils.createFeatureSink(
                    coupler_out_spec, context, c_fields, QgsWkbTypes.Point, crs)
            except Exception:
                c_sink, c_id = None, None

        uid = 0
        made = 0
        n_cpl = 0

        def _add(pdp, addr, hh, poly, mfg, geom, side):
            nonlocal uid, made, n_cpl
            if not geom or geom.isEmpty():
                return
            f = QgsFeature(fields)
            f.setGeometry(geom)
            f["PDP_ID"] = pdp
            f["ADDR_ID"] = addr
            f["HH_ID"] = hh
            f["POLYGON_ID"] = poly
            f["MFG_ID"] = mfg
            f["DUCT_TYPE"] = "Drop"
            f["LENGTH_M"] = round(float(geom.length()), 2)
            f["SIDE"] = side
            f["DUCT_UID"] = uid
            sink.addFeature(f, QgsFeatureSink.FastInsert)
            # Coupler at the pseudo (footway) end of the drop duct — the joint
            # where the drop duct meets the distribution network.
            if c_sink is not None:
                try:
                    ln = (geom.asMultiPolyline()[0] if geom.isMultipart()
                          else geom.asPolyline())
                    if ln:
                        cpt = QgsPointXY(ln[-1])
                        cf = QgsFeature(c_fields)
                        cf.setGeometry(QgsGeometry.fromPointXY(cpt))
                        cf["coupler_id"] = f"CPL-{uid + 1:04d}"
                        cf["COUPLER_TYPE"] = "Optical coupler (drop ↔ distribution)"
                        cf["PDP_ID"] = pdp
                        cf["ADDR_ID"] = addr
                        cf["HH_ID"] = hh
                        cf["POLYGON_ID"] = poly
                        cf["MFG_ID"] = mfg
                        cf["DUCT_UID"] = uid
                        cf["SIDE"] = side
                        c_sink.addFeature(cf, QgsFeatureSink.FastInsert)
                        n_cpl += 1
                except Exception:
                    pass
            uid += 1
            made += 1

        # 1) Preferred: garden trenches (object → footway) carry the exact
        #    connection geometry plus the needed linkage attributes.
        if garden_lyr is not None and garden_lyr.featureCount() > 0:
            f_pdp  = first_field_case_insensitive(garden_lyr, ["PDP_ID", "pdp_id"])
            f_addr = first_field_case_insensitive(garden_lyr, ["addr_id", "ADDR_ID", "address_id"])
            f_hh   = first_field_case_insensitive(garden_lyr, ["hhs", "hh", "hh_id"])
            f_poly = first_field_case_insensitive(garden_lyr, ["POLYGON_ID", "polygon_id"])
            f_mfg  = first_field_case_insensitive(garden_lyr, ["MFG_ID", "mfg_id"])
            f_side = first_field_case_insensitive(garden_lyr, ["sidewalk", "side"])
            # For snapping the object end back to the building when garden
            # trenches were trimmed against a building buffer.
            # NOTE: ADDR_ID is NOT guaranteed unique (two premises can share an
            # address). Index ALL objects per address and pick the one closest
            # to this garden line's start so we never snap a duct to a far-away
            # building that happens to share the same address.
            obj_by_addr = {}
            o_addr = None
            if obj_lyr is not None:
                o_addr = first_field_case_insensitive(obj_lyr, ["ADDR_ID", "addr_id", "id"])
                if o_addr:
                    for of in obj_lyr.getFeatures():
                        v = of.attribute(o_addr)
                        if v is None:
                            continue
                        k = str(v).strip().lower()
                        if k:
                            obj_by_addr.setdefault(k, []).append(of)
            for gf in garden_lyr.getFeatures():
                g = gf.geometry()
                # If the garden line was trimmed (start no longer touches the
                # building), rebuild it from the real object point to keep the
                # drop duct spanning pseudo → object.
                if obj_by_addr and f_addr:
                    v = gf.attribute(f_addr)
                    key = str(v).strip().lower() if v is not None else None
                    cands = obj_by_addr.get(key) if key else None
                    if cands:
                        of = cands[0]
                        if len(cands) > 1 and g and not g.isEmpty():
                            ln = g.asMultiPolyline()[0] if g.isMultipart() else g.asPolyline()
                            if ln:
                                g_start = QgsPointXY(ln[0])
                                gs_geom = QgsGeometry.fromPointXY(g_start)
                                def _dist_obj(o2):
                                    og2 = o2.geometry()
                                    if og2 is None or og2.isEmpty():
                                        return float("inf")
                                    if QgsWkbTypes.geometryType(og2.wkbType()) == QgsWkbTypes.PointGeometry:
                                        p2 = og2.asPoint()
                                    else:
                                        p2 = og2.centroid().asPoint()
                                    return gs_geom.distance(QgsGeometry.fromPointXY(QgsPointXY(p2)))
                                of = min(cands, key=_dist_obj)
                        og = of.geometry()
                        if og and not og.isEmpty() and g and not g.isEmpty():
                            if QgsWkbTypes.geometryType(og.wkbType()) == QgsWkbTypes.PointGeometry:
                                hpt = og.asPoint()
                            else:
                                hpt = og.centroid().asPoint()
                            ln = g.asMultiPolyline()[0] if g.isMultipart() else g.asPolyline()
                            if ln:
                                end_pt = QgsPointXY(ln[-1])
                                hg = QgsGeometry.fromPointXY(QgsPointXY(hpt))
                                if hg.distance(g) > 1.0:
                                    g = QgsGeometry.fromPolylineXY([QgsPointXY(hpt), end_pt])
                _add(
                    str(gf[f_pdp]) if f_pdp else None,
                    str(gf[f_addr]) if f_addr else None,
                    str(gf[f_hh]) if f_hh else None,
                    str(gf[f_poly]) if f_poly else None,
                    str(gf[f_mfg]) if f_mfg else None,
                    g,
                    str(gf[f_side]) if f_side else None,
                )
        # 2) Fallback: straight object → pseudo lines joined by address id.
        elif pseudo_lyr is not None and obj_lyr is not None:
            p_addr = first_field_case_insensitive(pseudo_lyr, ["addr_id", "ADDR_ID", "hh_id"])
            o_addr = first_field_case_insensitive(obj_lyr, ["ADDR_ID", "addr_id", "id"])
            o_pdp  = first_field_case_insensitive(obj_lyr, ["PDP_ID", "pdp_id"])
            o_poly = first_field_case_insensitive(obj_lyr, ["POLYGON_ID", "polygon_id"])
            o_mfg  = first_field_case_insensitive(obj_lyr, ["MFG_ID", "mfg_id"])
            if p_addr and o_addr:
                # ADDR_ID may be duplicated; index ALL pseudo points per address
                # and pick the one nearest this object (no far-away cross-pairs).
                pseudo_by_addr = {}
                for pf in pseudo_lyr.getFeatures():
                    v = pf.attribute(p_addr)
                    if v is None:
                        continue
                    k = str(v).strip().lower()
                    if k:
                        pseudo_by_addr.setdefault(k, []).append(pf)
                for of in obj_lyr.getFeatures():
                    v = of.attribute(o_addr)
                    if v is None:
                        continue
                    pf_list = pseudo_by_addr.get(str(v).strip().lower())
                    if not pf_list:
                        continue
                    og = of.geometry()
                    if not og or og.isEmpty():
                        continue
                    if QgsWkbTypes.geometryType(og.wkbType()) == QgsWkbTypes.PointGeometry:
                        hpt = og.asPoint()
                    else:
                        hpt = og.centroid().asPoint()
                    if len(pf_list) > 1:
                        hg = QgsGeometry.fromPointXY(QgsPointXY(hpt))
                        def _dist_p(p2):
                            pg2 = p2.geometry()
                            if pg2 is None or pg2.isEmpty():
                                return float("inf")
                            if QgsWkbTypes.geometryType(pg2.wkbType()) == QgsWkbTypes.PointGeometry:
                                pt2 = pg2.asPoint()
                            else:
                                pt2 = pg2.centroid().asPoint()
                            return hg.distance(QgsGeometry.fromPointXY(QgsPointXY(pt2)))
                        pf = min(pf_list, key=_dist_p)
                    else:
                        pf = pf_list[0]
                    pg = pf.geometry()
                    if not pg or pg.isEmpty():
                        continue
                    if QgsWkbTypes.geometryType(pg.wkbType()) == QgsWkbTypes.PointGeometry:
                        ppt = pg.asPoint()
                    else:
                        ppt = pg.centroid().asPoint()
                    _add(
                        str(of.attribute(o_pdp)) if o_pdp else None,
                        str(of.attribute(o_addr)),
                        None,
                        str(of.attribute(o_poly)) if o_poly else None,
                        str(of.attribute(o_mfg)) if o_mfg else None,
                        QgsGeometry.fromPolylineXY([QgsPointXY(hpt), QgsPointXY(ppt)]),
                        None,
                    )

        feedback.pushInfo(f"✅ Drop (small) ducts created: {made}; couplers placed: {n_cpl}")
        if sink:
            del sink
        if c_sink:
            del c_sink
        return out_id, c_id

    def _build_route_ducts(self, cables_lyr, out_uri, profile_key, crs,
                           context, feedback, subtract_lyr=None, runs_uri=None,
                           skip_cable_types=()):
        """Build ONE duct per connected route from a cable layer.

        Cables that co-route (spatially touch within a small tolerance) are
        clubbed into a single duct feature carrying the cable ids, so a route
        with several feeder/distribution cables gets exactly one duct
        (4-way for Feeder, 2-way for Distribution) instead of one per cable.

        ``subtract_lyr`` (optional): geometry to remove from every duct — used
        for Distribution so the drop legs (footway → object, i.e. the Garden
        Trenches, which are covered by the separate Drop_Ducts layer) are NOT
        duplicated inside the distribution duct. The duct then stops at the
        footway and carries only the route trunk.

        ``skip_cable_types`` (optional): cable rows whose ``CABLE_TYPE`` is in
        this set never enter the clubber. Distribution passes ``("Drop",)``:
        a garden-leg drop cable is a one-premise arm that TAPS the trunk, so
        the 0.5 m clubbing tolerance would otherwise club it into the trunk's
        duct and drag the whole drop leg into the distribution duct. This is
        the tier-level equivalent of the historical garden subtract pass (and
        replaces the geometric subtraction under per-span publishing) — the
        drop legs are carried by the separate ``Drop_Ducts`` layer.

        Returns the output layer id (or None when there is nothing to write).
        """
        if out_uri is None or cables_lyr is None:
            return None
        if cables_lyr.featureCount() == 0:
            return None

        # Pre-combine the subtract geometry once (same CRS as the cables).
        subtract_union = None
        if subtract_lyr is not None and subtract_lyr.featureCount() > 0:
            try:
                src_crs = subtract_lyr.crs()
                tgt_crs = crs
                xform = None
                if src_crs.isValid() and tgt_crs.isValid() and src_crs.authid() != tgt_crs.authid():
                    xform = QgsCoordinateTransform(src_crs, tgt_crs, QgsProject.instance())
                geoms = []
                for sf in subtract_lyr.getFeatures():
                    sg = sf.geometry()
                    if not sg or sg.isEmpty():
                        continue
                    if xform is not None:
                        try:
                            sg = sg.clone()
                            sg.transform(xform)
                        except Exception:
                            continue
                    geoms.append(sg)
                if geoms:
                    from ..utils.geometry_ops import unary_union_geoms as _uug_sub
                    subtract_union = _uug_sub(geoms)
                    # The cable layer builds the drop legs from a fixed/
                    # reprojected copy of the garden input, so its vertices can
                    # differ from this raw layer by a few cm. Buffer the
                    # subtract geometry slightly so GEOS difference removes the
                    # whole leg instead of leaving slivers (verified: 3% -> 0%
                    # overlap with Drop_Ducts at 0.05 m).
                    if subtract_union is not None and not subtract_union.isEmpty():
                        try:
                            subtract_union = subtract_union.buffer(0.05, 8)
                        except Exception:
                            pass
            except Exception:
                subtract_union = None

        # Local catalogue profile (same values as utils.attr_enrich.DUCT_PROFILE)
        _PROFILES = {
            "Feeder": {"ways": 4, "duct_type": "4-Way HDPE"},
            "Distribution": {"ways": 2, "duct_type": "2-Way HDPE"},
        }
        prof = _PROFILES.get(profile_key, _PROFILES["Distribution"])
        ways = int(prof.get("ways", 4 if profile_key == "Feeder" else 2))

        fld_id = first_field_case_insensitive(
            cables_lyr, ["cable_id", "CABLE_ID", "id", "fid"])
        fld_pdp = first_field_case_insensitive(
            cables_lyr, ["PDP_IDS", "pdp_ids", "PDP_ID", "pdp_id"])
        fld_poly = first_field_case_insensitive(
            cables_lyr, ["POLYGON_ID", "polygon_id"])

        feats = []          # list of QgsFeature (valid geometry only)
        fid_to_idx = {}
        idx = QgsSpatialIndex()
        fld_ctype = (first_field_case_insensitive(cables_lyr, ["CABLE_TYPE", "cable_type"])
                     if skip_cable_types else None)
        skipped = 0
        for f in cables_lyr.getFeatures():
            g = f.geometry()
            if not g or g.isEmpty():
                continue
            if fld_ctype is not None:
                ct = f[fld_ctype]
                if ct is not None and str(ct).strip() in skip_cable_types:
                    skipped += 1
                    continue
            fid_to_idx[f.id()] = len(feats)
            feats.append(f)
            idx.addFeature(f)
        if skipped:
            try:
                feedback.pushInfo(
                    f"{profile_key} route ducts: skipped {skipped} "
                    f"{'/'.join(skip_cable_types)} cable(s) — carried by "
                    f"Drop_Ducts, not the route duct.")
            except Exception:
                pass
        if not feats:
            return None

        # --- Union-find: club cables whose geometries touch within TOL m ---
        TOL = 0.5
        parent = list(range(len(feats)))

        def _find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def _union(a, b):
            ra, rb = _find(a), _find(b)
            if ra != rb:
                parent[rb] = ra

        for i, f in enumerate(feats):
            g = f.geometry()
            bb = g.buffer(TOL, 8).boundingBox()
            for fid in idx.intersects(bb):
                j = fid_to_idx.get(fid)
                if j is None or j <= i:
                    continue
                try:
                    if g.distance(feats[j].geometry()) <= TOL:
                        _union(i, j)
                except Exception:
                    pass

        groups = defaultdict(list)
        for i in range(len(feats)):
            groups[_find(i)].append(i)

        fields = QgsFields()
        for nm, t in (
            ("DUCT_TYPE", QMetaType.Type.QString),
            ("capacity_total", QMetaType.Type.Int),
            ("ways_used", QMetaType.Type.Int),
            ("cables_carried", QMetaType.Type.QString),
            ("pdp_ids", QMetaType.Type.QString),
            ("POLYGON_ID", QMetaType.Type.QString),
            ("length_m", QMetaType.Type.Double),
            ("REVIEW", QMetaType.Type.Int),
            ("INFRA_STATUS", QMetaType.Type.QString),
        ):
            fields.append(QgsField(nm, t))

        # Per-route layer keeps the run-level schema; the published component
        # adds the aggregate columns describing what it merged.
        run_fields = QgsFields(fields)
        for nm, t in (
            ("DUCT_ID", QMetaType.Type.QString),
            ("N_DUCTS", QMetaType.Type.Int),
            ("WAYS_TOTAL", QMetaType.Type.Int),
            ("CLUBS", QMetaType.Type.Int),
            ("BUNDLE_LEN_M", QMetaType.Type.Double),
        ):
            fields.append(QgsField(nm, t))

        try:
            sink, out_id = QgsProcessingUtils.createFeatureSink(
                out_uri, context, fields, QgsWkbTypes.MultiLineString, crs)
        except Exception as e:
            try:
                feedback.reportError(f"Route duct sink failed: {e}")
            except Exception:
                pass
            return None
        if sink is None:
            return None

        # Per-route ducts go to their own layer when one is requested: the
        # chamber stage counts "distinct ducts" passing a junction, and one
        # component per tier would collapse that count.
        runs_sink, runs_id = None, None
        if runs_uri:
            try:
                runs_sink, runs_id = QgsProcessingUtils.createFeatureSink(
                    runs_uri, context, run_fields, QgsWkbTypes.MultiLineString, crs)
            except Exception:
                runs_sink, runs_id = None, None

        bins = []            # (geom, cable_ids, pdp_ids, poly_ids, n_cables)
        made = 0
        flag_cnt = 0
        for members in groups.values():
            # A corridor may carry more cables than the duct has ways: emit
            # several {ways}-way ducts along the same route, keeping cables
            # that co-route longest together, instead of one oversized duct.
            for bin_ in self._chunk_into_ducts(members, feats, ways):
                geoms = []
                cable_ids = []
                pdp_set = []
                poly_set = []
                for i in bin_:
                    f = feats[i]
                    g = f.geometry()
                    if g and not g.isEmpty():
                        geoms.append(g)
                    if fld_id is not None:
                        v = f.attribute(fld_id)
                        if v is not None:
                            cable_ids.append(str(v))
                    if fld_pdp is not None:
                        v = f.attribute(fld_pdp)
                        if v is not None:
                            pdp_set.append(str(v))
                    if fld_poly is not None:
                        v = f.attribute(fld_poly)
                        if v is not None:
                            poly_set.append(str(v))
                if not geoms:
                    continue
                from ..utils.geometry_ops import unary_union_geoms as _uug
                ug = _uug(geoms)
                if not ug or ug.isEmpty():
                    continue
                # Distribution: strip the drop legs (footway → object) so they
                # stay exclusively in the Drop_Ducts layer instead of being
                # duplicated inside the distribution duct.
                if subtract_union is not None and not subtract_union.isEmpty():
                    try:
                        ug_trimmed = ug.difference(subtract_union)
                        if ug_trimmed is not None and not ug_trimmed.isEmpty():
                            ug = ug_trimmed
                    except Exception:
                        pass
                n_cab = len(cable_ids)
                bins.append((ug, cable_ids, pdp_set, poly_set, n_cab))
                if runs_sink is not None:
                    nf = QgsFeature(run_fields)
                    nf.setGeometry(ug)
                    nf["DUCT_TYPE"] = prof.get("duct_type", "4-Way HDPE" if profile_key == "Feeder" else "2-Way HDPE")
                    nf["capacity_total"] = ways
                    nf["ways_used"] = n_cab    # always <= ways by construction
                    nf["cables_carried"] = ",".join(cable_ids)
                    nf["pdp_ids"] = ",".join(dict.fromkeys(pdp_set))
                    nf["POLYGON_ID"] = ",".join(dict.fromkeys(poly_set))
                    nf["length_m"] = round(float(ug.length()), 2)
                    nf["REVIEW"] = 0
                    nf["INFRA_STATUS"] = "Proposed"
                    runs_sink.addFeature(nf, QgsFeatureSink.FastInsert)
                made += 1
                if n_cab > ways:
                    flag_cnt += 1

        # ── ONE component per tier, ducts on similar routes CLUBBED ───────
        # The bins above are parallel ducts along the SAME streets: a corridor
        # with more cables than the profile holds is built as several {ways}-way
        # ducts laid side by side.  Published as laid, the same street would be
        # drawn many times over (measured on the Berlin run: 87% of the
        # distribution duct metres sit within 1 m of another duct — 148 runs
        # covering a 6.3 km corridor).  The component therefore carries the
        # CLUBBED corridor: every duct on the same route dissolved into ONE
        # line per street.
        #
        # Nothing is lost: the material quantity — the sum of the parallel
        # runs actually laid — is kept in BUNDLE_LEN_M (the BOQ bills that),
        # N_DUCTS says how many parallel ducts a corridor needs, WAYS_TOTAL
        # how many ways they provide, and CLUBS how many distinct routes were
        # clubbed.  Chambers do not split the component: they are spliced into
        # it afterwards and the chamber-bounded sections are recorded in
        # SECTIONS_JSON.
        from ..utils.geometry_ops import unary_union_geoms as _uug_club
        agg_len = 0.0          # sum of the parallel runs = material metres
        corridor_len = 0.0
        agg_used = 0
        agg_cables, agg_pdps, agg_polys = [], [], []
        bin_geoms = []
        for ug, cable_ids, pdp_set, poly_set, n_cab in bins:
            bin_geoms.append(ug)
            try:
                agg_len += float(ug.length())
            except Exception:
                pass
            agg_used += n_cab
            agg_cables.extend(cable_ids)
            agg_pdps.extend(pdp_set)
            agg_polys.extend(poly_set)

        club_geom = _uug_club(bin_geoms) if bin_geoms else None
        if club_geom is not None and not club_geom.isEmpty():
            corridor_len = float(club_geom.length())
            nf = QgsFeature(fields)
            nf.setGeometry(club_geom)
            nf["DUCT_TYPE"] = prof.get("duct_type", "4-Way HDPE" if profile_key == "Feeder" else "2-Way HDPE")
            nf["capacity_total"] = ways
            nf["ways_used"] = int(agg_used)
            nf["cables_carried"] = ",".join(agg_cables)
            nf["pdp_ids"] = ",".join(dict.fromkeys(agg_pdps))
            nf["POLYGON_ID"] = ",".join(dict.fromkeys(agg_polys))
            nf["length_m"] = round(corridor_len, 2)
            nf["BUNDLE_LEN_M"] = round(agg_len, 2)
            nf["N_DUCTS"] = len(bins)
            nf["WAYS_TOTAL"] = int(ways) * len(bins)
            nf["CLUBS"] = len(groups)
            nf["REVIEW"] = 1 if flag_cnt else 0
            nf["INFRA_STATUS"] = "Proposed"
            nf["DUCT_ID"] = f"{profile_key.upper()}-DUCT-001"
            sink.addFeature(nf, QgsFeatureSink.FastInsert)

        if sink:
            del sink
        if runs_sink:
            del runs_sink
        feedback.pushInfo(
            f"✅ Route ducts ({profile_key}): {made} x {ways}-way duct run(s) from "
            f"{len(feats)} cables over {len(groups)} route group(s) "
            f"(cables split into {ways}-way ducts, {flag_cnt} oversized) → clubbed "
            f"into ONE {corridor_len:,.1f} m corridor component "
            f"({len(groups)} route(s), {len(bins)} parallel run(s), "
            f"{int(ways) * len(bins)} ways, {agg_len:,.1f} m of duct material).")
        return out_id

    @staticmethod
    def _chunk_into_ducts(members, feats, ways):
        """Split a corridor's cable indices into bins of <= `ways` cables.

        Greedy: each bin starts from the longest remaining cable, then fills
        with the cables sharing the most length with the bin so far — so the
        cables that co-route longest end up in the same duct. Bins never
        exceed the duct's way count, so no duct is ever oversized.
        """
        remaining = list(members)
        bins = []
        while remaining:
            seed = max(remaining, key=lambda i: (
                feats[i].geometry().length() if feats[i].geometry() else 0.0))
            bin_ = [seed]
            remaining.remove(seed)
            bin_geom = feats[seed].geometry()
            while len(bin_) < ways and remaining:
                best, best_share = None, 0.0
                for j in remaining:
                    g = feats[j].geometry()
                    if not g or g.isEmpty():
                        best, best_share = j, float("inf")
                        break
                    try:
                        sh = bin_geom.intersection(g).length()
                    except Exception:
                        sh = 0.0
                    if sh > best_share:
                        best_share, best = sh, j
                if best is None:
                    break
                bin_.append(best)
                remaining.remove(best)
                try:
                    bin_geom = bin_geom.combine(feats[best].geometry())
                except Exception:
                    pass
            bins.append(bin_)
        return bins

    def processAlgorithm(self, p, context, feedback):
        # Resolve parent output URIs up front
        out_feeder_uri = self.parameterAsOutputLayer(p, self.O_FEEDER, context)
        out_distr_uri  = self.parameterAsOutputLayer(p, self.O_DISTR,  context)
        out_drop_uri   = self.parameterAsOutputLayer(p, self.O_DROP,   context)
        # Optional per-route duct outputs (chamber-stage inputs).
        runs_feeder_uri = (self.parameterAsOutputLayer(p, self.O_FEEDER_RUNS, context)
                           if p.get(self.O_FEEDER_RUNS) else None)
        runs_dist_uri = (self.parameterAsOutputLayer(p, self.O_DIST_RUNS, context)
                         if p.get(self.O_DIST_RUNS) else None)

        # >>> ADD THE HELPER RIGHT HERE <<<
        from qgis.core import QgsVectorLayer, QgsProject, QgsProcessingUtils
        # (QgsProcessingFeatureSource import is optional; not all builds expose it)
        # from qgis.core import QgsProcessingFeatureSource

        def _as_layer_any(param_key, *, fallback_names=None):
            """Return a valid QgsVectorLayer from a Processing parameter, trying:
               1) parameterAsVectorLayer
               2) parameterAsSource -> native:savefeatures to memory layer
               3) parameterAsString -> resolve by layer ID/name, or open as OGR path
               4) fallback_names -> find in current project by (partial) name(s)
            """
            # 1) Direct layer
            lyr = self.parameterAsVectorLayer(p, param_key, context)
            if lyr is not None and lyr.isValid():
                return lyr

            # 2) Feature source -> memory layer
            try:
                src = self.parameterAsSource(p, param_key, context)
            except Exception:
                src = None
            if src is not None:
                try:
                    mem = processing.run(
                        "native:savefeatures",
                        {"INPUT": src, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                        context=context, feedback=feedback
                    )["OUTPUT"]
                    if mem and mem.isValid():
                        return mem
                except Exception:
                    pass

            # 3) String: layer id/name or file path
            try:
                s = self.parameterAsString(p, param_key, context)
            except Exception:
                s = ""
            s = (s or "").strip()
            if s:
                # a) try mapLayerFromString (layer id or name)
                try:
                    cand = QgsProcessingUtils.mapLayerFromString(s, context)
                    if cand and cand.isValid():
                        return cand
                except Exception:
                    pass
                # b) exact name in project
                try:
                    byname = QgsProject.instance().mapLayersByName(s)
                    if byname:
                        return byname[0]
                except Exception:
                    pass
                # c) try as OGR path/URI
                try:
                    lyr2 = QgsVectorLayer(s, os.path.basename(s) or "resolved", "ogr")
                    if lyr2.isValid():
                        return lyr2
                except Exception:
                    pass

            # 4) Fallback by partial name(s)
            if fallback_names:
                try:
                    names_lc = [n.lower() for n in fallback_names]
                    for lyr3 in QgsProject.instance().mapLayers().values():
                        nm = (lyr3.name() or "").lower()
                        if any(tag in nm for tag in names_lc):
                            return lyr3
                except Exception:
                    pass

            return None
        # <<< END OF HELPER >>>

        # --- FEEDER ---
        feedback.pushInfo("⚙️ Running embedded Feeder algorithm …")
        feeder = AlgFeederDuctsNoSplit()
        feeder.initAlgorithm()  # <<< IMPORTANT: restore this

        net_lyr = _as_layer_any(self.P_NETWORK, fallback_names=["Feeder_Trench_Final","Final_Trenches","Feeder_Trench"])
        mfg_lyr = _as_layer_any(self.P_MFG,     fallback_names=["MFG_Point","MFG","mfg_point"])
        pdp_lyr = _as_layer_any(self.P_PDP,     fallback_names=["PDP","PDPs","clean_pdps","assigned_pdps"])


        def _lname(L): return (L.name() if L else "None")
        feedback.pushInfo(f"Feeder resolve → network={_lname(net_lyr)}; mfg={_lname(mfg_lyr)}; pdp={_lname(pdp_lyr)}")

        if any(x is None for x in (net_lyr, mfg_lyr, pdp_lyr)):
            missing = [n for n, x in (("network", net_lyr), ("mfg", mfg_lyr), ("pdp", pdp_lyr)) if x is None]
            raise QgsProcessingException(f"Missing required layers after resolve: {', '.join(missing)}.")

        # Auto-detect canonical ID fields (field pickers removed from the UI)
        pdp_id_fld = first_field_case_insensitive(pdp_lyr, ["PDP_ID", "pdp_id", "pdp"]) or ""
        mfg_id_fld = first_field_case_insensitive(mfg_lyr, ["MFG_ID", "mfg_id", "mfg"]) or ""
        feedback.pushInfo(f"Auto-detected ID fields → PDP: '{pdp_id_fld}', MFG: '{mfg_id_fld}'")

        feeder_params = {
            feeder.L_NET:   net_lyr,
            feeder.L_MFG:   mfg_lyr,
            feeder.L_PDP:   pdp_lyr,
            feeder.F_PDPID: pdp_id_fld,
            feeder.F_MFGID: mfg_id_fld,
            feeder.O_DUCTS: out_feeder_uri,
            feeder.ADD_STYLE: False,

            # >>> ensure non-zero numeric params <<<
            feeder.SNAP_TOL: 1.5,      # meters
            feeder.NODE_TOL: 0.5,      # meters  (must be > 0)
            feeder.END_EPS:  0.25,     # meters
            feeder.INT_EPS:  0.25,     # meters
            feeder.INC_TRUNK: True,
            feeder.MAX_K:     4,
        }

        # Route-based feeder ducts: when the Feeder_Cable layer is available
        # (planned from Final_Trenches), emit ONE 4-way duct per connected
        # route carrying the cables on it, instead of prefix-branch bundling.
        feeder_route_done = False
        feeder_cables = _as_layer_any(self.P_FEEDER_CABLES,
                                      fallback_names=["Feeder_Cable", "Feeder_Cables"])
        if feeder_cables is not None and feeder_cables.featureCount() > 0:
            try:
                _rid = self._build_route_ducts(
                    feeder_cables, out_feeder_uri, "Feeder",
                    net_lyr.crs(), context, feedback,
                    runs_uri=runs_feeder_uri)
                feeder_route_done = _rid is not None
            except Exception as e:
                try:
                    feedback.reportError(
                        f"Route-based feeder ducts failed ({e}); falling back to bundling algorithm.")
                except Exception:
                    pass
                feeder_route_done = False
        if not feeder_route_done:
            feeder.processAlgorithm(feeder_params, context, feedback)

        # --- DROP DUCTS (small connections: pseudo object point → object) ---
        feedback.pushInfo("⚙️ Building small drop ducts (pseudo → object) …")
        pseudo_lyr = _as_layer_any(self.P_PSEUDO)
        garden_lyr = _as_layer_any(self.P_GARDEN)
        obj_lyr = self.parameterAsVectorLayer(p, self.P_OBJECTS, context) or self.parameterAsSource(p, self.P_OBJECTS, context)
        try:
            out_coupler_uri = self.parameterAsOutputLayer(p, self.O_COUPLE, context)
            out_drop_id, out_coupler_id = self._build_drop_ducts(
                p, context, feedback, out_drop_uri,
                garden_lyr, pseudo_lyr, obj_lyr, net_lyr.crs(),
                coupler_out_spec=out_coupler_uri)
        except Exception as e:
            try:
                feedback.reportError(f"Drop ducts failed: {e}")
            except Exception:
                pass
            out_drop_id = None
            out_coupler_id = None

        # --- DISTRIBUTION ---
        feedback.pushInfo("⚙️ Running embedded Distribution algorithm …")

        side_l = (self.parameterAsVectorLayer(p, self.P_SIDE_L, context) or self.parameterAsSource(p, self.P_SIDE_L, context)) or _find_layer_by_partial_name(["sidewalk left","footway left","sidewalk_l"])
        side_r = (self.parameterAsVectorLayer(p, self.P_SIDE_R, context) or self.parameterAsSource(p, self.P_SIDE_R, context)) or _find_layer_by_partial_name(["sidewalk right","footway right","sidewalk_r"])
        final_tan = (self.parameterAsVectorLayer(p, self.P_FINAL, context) or self.parameterAsSource(p, self.P_FINAL, context)) or (self.parameterAsVectorLayer(p, self.P_NETWORK, context) or self.parameterAsSource(p, self.P_NETWORK, context))
        if self.parameterAsVectorLayer(p, self.P_FINAL, context) is None:
            feedback.pushInfo("ℹ️ Using feeder network as tangent trenches for distribution.")

        distr = AlgDistributionDucts()
        distr.initAlgorithm() 

        # ---- Preflight diagnostics (so you see EXACTLY what's missing) ----
        pdp_lyr = self.parameterAsVectorLayer(p, self.P_PDP, context) or self.parameterAsSource(p, self.P_PDP, context)
        # obj_lyr was already resolved above (drop-duct build).
        # Prefer pseudo object/HH points (on the footway) as distribution duct
        # endpoints so ducts stop at the property line; the small drop ducts
        # (from garden trenches) then connect pseudo → object.
        # NOTE: with pseudo endpoints the distribution duct's hh_ids field
        # carries the pseudo feature ids — the real address linkage lives in
        # the Drop_Ducts layer (ADDR_ID / HH_ID).
        dist_objs = pseudo_lyr if (pseudo_lyr is not None and pseudo_lyr.featureCount() > 0) else obj_lyr
        if dist_objs is pseudo_lyr:
            feedback.pushInfo("Distribution duct endpoints → pseudo object points (ducts stop at the footway).")
        else:
            feedback.pushInfo("Distribution duct endpoints → object points (no pseudo layer provided; ducts run to the objects).")
        # Auto-detect canonical fields on the resolved layers
        hh_fld   = first_field_case_insensitive(dist_objs, ["ADDR_ID", "addr_id", "HH_ID", "hh_id", "address_id", "id"]) or "" if dist_objs else ""
        pdp_on_p = first_field_case_insensitive(pdp_lyr, ["PDP_ID", "pdp_id", "pdp"]) or "" if pdp_lyr else ""
        pdp_on_h = first_field_case_insensitive(dist_objs, ["PDP_ID", "pdp_id"]) or "" if dist_objs else ""
        feedback.pushInfo(f"Distribution auto-detected fields → HH id: '{hh_fld}', PDP id on PDPs: '{pdp_on_p}', PDP id on HH: '{pdp_on_h}'")

        # --- replace the whole validation block with this ---
        def _field_exists(lyr, fld):
            return fld in [f.name() for f in lyr.fields()] if lyr and fld else False

        errs = []
        if pdp_lyr is None:  errs.append("Distribution: PDP_POINTS layer is NULL (check the input).")
        if dist_objs is None:  errs.append("Distribution: endpoint points layer (pseudo or objects) is NULL (check the input).")
        if side_l is None or side_r is None:
            try:
                feedback.pushWarning("Distribution: Sidewalk L/R not provided — will build graph from tangent/feeder only and default side labels.")
            except Exception:
                pass
            
        if not pdp_on_p:     errs.append("Distribution: PDP ID field on PDPs is empty.")
        if not pdp_on_h:     errs.append("Distribution: PDP ID field on endpoint points (pseudo/objects) is empty.")
        if not hh_fld:       errs.append("Distribution: HH ID field on endpoint points (pseudo/objects) is empty.")

        if pdp_lyr and not _field_exists(pdp_lyr, pdp_on_p):
            errs.append(f"Distribution: PDP_POINTS is missing field '{pdp_on_p}'.")
        if dist_objs and not _field_exists(dist_objs, pdp_on_h):
            errs.append(f"Distribution: endpoint points (pseudo/objects) is missing field '{pdp_on_h}'.")
        if dist_objs and not _field_exists(dist_objs, hh_fld):
            errs.append(f"Distribution: endpoint points (pseudo/objects) is missing field '{hh_fld}'.")

        if errs:
            # Warn instead of raising, then skip this stage safely.
            try:
                for e in errs:
                    feedback.pushWarning(f"{e} Skipping Distribution stage.")
            except Exception:
                # Fallback logging if feedback is unavailable
                try:
                    for e in errs:
                        QgsMessageLog.logMessage(f"{e} Skipping Distribution stage.", "OneClick", 1)
                except Exception:
                    pass
            
            # Return empty outputs so upstream steps and the demo can continue
            return {
                self.O_FEEDER: out_feeder_uri,
                self.O_DISTR:  None,
                self.O_DROP:   out_drop_id,
            }


        # ---- Now run child algorithm writing DIRECTLY to parent output ----
        distr_params = {
            distr.L_PDP:        pdp_lyr,
            distr.L_HH:         dist_objs,
            distr.L_LEFT:       side_l,
            distr.L_RIGHT:      side_r,
            distr.L_TAN:        final_tan,
            distr.F_PDP_ON_PDP: pdp_on_p,
            distr.F_HH_ID:      hh_fld,
            distr.F_PDP_ON_HH:  pdp_on_h,
            distr.CRS_TGT:      QgsCoordinateReferenceSystem(self.DEFAULT_CRS_AUTHID),
            distr.ADD_STYLE:    False,
            distr.O_DUCTS:      out_distr_uri,
        }
        
        # Guard: required inputs present?
        _missing = [k for k, v in {
            "PDP": pdp_lyr, "HH": dist_objs, "LEFT": side_l, "RIGHT": side_r, "TAN": final_tan
        }.items() if v is None]
        if _missing:
            for m in _missing:
                try:
                    feedback.pushWarning(f"Distribution: missing {m}; skipping Distribution stage.")
                except Exception:
                    pass
            return {
                self.O_FEEDER: locals().get("out_feeder_uri", None),
                self.O_DISTR:  None,
                self.O_DROP:   locals().get("out_drop_id", None),
            }
        
        # Route-based distribution ducts: when the Distribution_Cable layer is
        # available, emit ONE 2-way duct per connected co-route carrying the
        # cables on it (bundled), instead of per-side per-PDP groups.
        dist_route_done = False
        dist_cables = _as_layer_any(self.P_DIST_CABLES,
                                    fallback_names=["Distribution_Cable", "Distribution_Cables"])
        if dist_cables is not None and dist_cables.featureCount() > 0:
            try:
                # The distribution trunk cables are laid ON the spine spans
                # (the trench geometry itself). The garden-leg DROP cables
                # (CABLE_TYPE = "Drop") merely TAP the trunk at the footway
                # point, so the clubber's 0.5 m tolerance would club them into
                # the trunk's duct and pull the whole drop leg into the
                # distribution duct — the rule is that drop legs live only in
                # Drop_Ducts, so the drops are excluded by tier.
                _rid = self._build_route_ducts(
                    dist_cables, out_distr_uri, "Distribution",
                    QgsCoordinateReferenceSystem(self.DEFAULT_CRS_AUTHID),
                    context, feedback,
                    subtract_lyr=None,
                    runs_uri=runs_dist_uri,
                    skip_cable_types=("Drop", "Garden"))
                dist_route_done = _rid is not None
            except Exception as e:
                try:
                    feedback.reportError(
                        f"Route-based distribution ducts failed ({e}); falling back to grouping algorithm.")
                except Exception:
                    pass
                dist_route_done = False
        if dist_route_done:
            return {
                self.O_FEEDER: locals().get("out_feeder_uri", None),
                self.O_DISTR:  out_distr_uri,
                self.O_DROP:   locals().get("out_drop_id", None),
                self.O_COUPLE: locals().get("out_coupler_id", None),
                self.O_FEEDER_RUNS: runs_feeder_uri,
                self.O_DIST_RUNS:   runs_dist_uri,
            }

        # Safe run
        try:
            distr.processAlgorithm(distr_params, context, feedback)
        except QgsProcessingException as e:
            try:
                feedback.reportError(f"Distribution failed (Processing): {e}")
            except Exception:
                pass
            return {
                self.O_FEEDER: locals().get("out_feeder_uri", None),
                self.O_DISTR:  None,
                self.O_DROP:   locals().get("out_drop_id", None),
            }
        except Exception as e:
            try:
                feedback.reportError(f"Distribution failed (unexpected): {e}")
            except Exception:
                pass
            return {
                self.O_FEEDER: locals().get("out_feeder_uri", None),
                self.O_DISTR:  None,
                self.O_DROP:   locals().get("out_drop_id", None),
                self.O_COUPLE: locals().get("out_coupler_id", None),
            }

        # Success: return URIs so Processing auto-loads the layer(s)
        return {
            self.O_FEEDER: out_feeder_uri,
            self.O_DISTR:  out_distr_uri,
            self.O_DROP:   out_drop_id,
            self.O_COUPLE: locals().get("out_coupler_id", None),
            self.O_FEEDER_RUNS: runs_feeder_uri,
            self.O_DIST_RUNS:   runs_dist_uri,
        }


# ----------------------------------------------------------------------
# Back-compat alias so older imports don't break:
# Some provider code does: from HLDPlanning.algorithms.duct_layer import AlgDucts
# We alias AlgDucts -> DuctLayer to satisfy that import gracefully.
# ----------------------------------------------------------------------
try:
    class AlgDucts(DuctLayer):
        """Legacy alias for backward compatibility + constant passthrough for callers that do `from ... import AlgDucts as Duct`."""

        # --- Feeder algorithm parameter keys expected by callers ---
        SNAP_TOL = AlgFeederDuctsNoSplit.SNAP_TOL          # "SNAP_TOLERANCE_M"
        NODE_TOL = AlgFeederDuctsNoSplit.NODE_TOL          # "NODE_SNAP_TOL_M"
        END_EPS  = AlgFeederDuctsNoSplit.END_EPS           # "ENDPOINT_EPS"
        INT_EPS  = AlgFeederDuctsNoSplit.INT_EPS           # "INTERSECT_EPS"
        INC_TRUNK= AlgFeederDuctsNoSplit.INC_TRUNK         # "INCLUDE_TRUNK"
        MAX_K    = AlgFeederDuctsNoSplit.MAX_K             # "MAX_PDPS_PER_DUCT"
        ADD_STYLE= AlgFeederDuctsNoSplit.ADD_STYLE         # "ADD_STYLED_TO_PROJECT"

        # IDs sometimes referenced by wrappers
        F_PDPID  = AlgFeederDuctsNoSplit.F_PDPID           # "FIELD_PDP_ID"
        F_MFGID  = AlgFeederDuctsNoSplit.F_MFGID           # "FIELD_MFG_ID"

        # --- Wrapper (this file) parameter keys so callers can use Duct.P_* / Duct.O_* ---
        P_NETWORK = DuctLayer.P_NETWORK
        P_MFG     = DuctLayer.P_MFG
        P_PDP     = DuctLayer.P_PDP
        P_PDP_ID  = DuctLayer.P_PDP_ID
        P_MFG_ID  = DuctLayer.P_MFG_ID
        P_OBJECTS = DuctLayer.P_OBJECTS
        P_HH_ID   = DuctLayer.P_HH_ID
        P_OBJ_PDP = DuctLayer.P_OBJ_PDP
        P_SIDE_L  = DuctLayer.P_SIDE_L
        P_SIDE_R  = DuctLayer.P_SIDE_R
        P_FINAL   = DuctLayer.P_FINAL
        P_CRS     = DuctLayer.P_CRS
        P_PSEUDO  = DuctLayer.P_PSEUDO
        P_GARDEN  = DuctLayer.P_GARDEN
        O_FEEDER  = DuctLayer.O_FEEDER
        O_DISTR   = DuctLayer.O_DISTR
        O_DROP    = DuctLayer.O_DROP
except Exception:
    # If DuctLayer wasn't defined for some reason, avoid crashing module import
    pass



# Explicit exports for clarity in dir()/from-imports (typed to keep linters happy)
from typing import List as _List  # noqa: F401
__all__: _List[str] = ["AlgFeederDuctsNoSplit", "AlgDistributionDucts", "DuctLayer", "AlgDucts"]
