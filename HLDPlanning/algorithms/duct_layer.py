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

# A distribution duct is laid in the region's TRENCH corridor. Selecting the
# corridor by POLYGON_ID can pick up a mis-tagged run, so the corridor is only
# accepted while it is still near the cables that ride it (see _corridor_for).
_CORRIDOR_SNAP_TOL_M = 25.0


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

    def _corridor_for(self, corridor_lyr, poly_ids, fallback_geom, tol_m=0.5,
                      tap_lyr=None, tap_cap_m=25.0, feedback=None):
        """The TRENCH corridor a distribution duct is laid in.

        A distribution duct is built *in the trench*, so its geometry has to be
        the trench the drop legs actually tap, not the cable's merged spine: the
        spine stops short of the footway points, which is why only 102 of 292
        pseudo-HH points (the couplers) sat on a distribution duct.

        Selects the corridor features carrying the same POLYGON_ID(s) and unions
        them; when the selection is empty (or the layer carries no polygon tag)
        it falls back to the cable geometry, and when it is merely far from the
        cables (a mis-tagged run) the fallback wins too — a duct must stay on
        the route its cables are on.
        """
        if corridor_lyr is None or corridor_lyr.featureCount() == 0:
            return fallback_geom
        names = corridor_lyr.fields().names()
        f_poly = next((n for n in ("POLYGON_ID", "polygon_id") if n in names), None)
        f_tier = next((n for n in ("TRENCH_TIER", "trench_tier") if n in names), None)
        wanted = {str(v).strip().upper() for v in (poly_ids or []) if str(v).strip()}
        if not wanted or f_poly is None:
            return fallback_geom
        cand = []
        for cf in corridor_lyr.getFeatures():
            pv = str(cf[f_poly] or "").strip().upper()
            if pv not in wanted:
                continue
            if f_tier is not None:
                # Garden legs are the drop ducts' own corridor — a distribution
                # duct must not absorb them (that is the old duplicate path).
                tier = str(cf[f_tier] or "").strip().lower()
                if tier and "garden" in tier:
                    continue
            g = cf.geometry()
            if g is not None and not g.isEmpty():
                cand.append(g)
        if not cand:
            return fallback_geom

        # Keep the complete non-garden corridor for this polygon. Distribution
        # is a polygon trunk: it must remain one connected route from the PDP
        # through every pseudo-PDP point, not a collection of cable fragments.
        # Garden spans are still excluded because they belong to Drop_Ducts.
        # The final chamber pass cuts this connected trunk into the installed
        # chamber-to-chamber duct components.
        #
        # Two kinds of span qualify:
        #   (a) the span the region's cable rides (the duct must contain its
        #       own cable), and
        #   (b) the span a drop taps, i.e. the nearest span to each pseudo-HH
        #       point of the region (that is where a coupler joins it).
        # Keep the corridor the duct is actually built in — NOT the region's
        # whole distribution trench (that tripled the duct material: 2.4 km of
        # spine became 8.6 km of duct). Two kinds of span qualify:
        #   (a) the span the region's cable rides (the duct must contain its
        #       own cable), and
        #   (b) the span a drop taps, i.e. the nearest span to each pseudo-HH
        #       point of the region (that is where a coupler joins it).
        picked = []
        for g in cand:
            if fallback_geom is None or fallback_geom.isEmpty():
                continue
            try:
                if g.distance(fallback_geom) <= tol_m:
                    picked.append(g)
            except Exception:
                continue
        taps = 0
        if tap_lyr is not None:
            names_t = tap_lyr.fields().names()
            t_poly = next((n for n in ("POLYGON_ID", "polygon_id") if n in names_t), None)
            for pf in tap_lyr.getFeatures():
                pg = pf.geometry()
                if pg is None or pg.isEmpty():
                    continue
                if t_poly is not None:
                    tv = str(pf[t_poly] or "").strip().upper()
                    if tv and tv not in wanted:
                        continue
                best = None
                for g in cand:
                    try:
                        d = g.distance(pg)
                    except Exception:
                        continue
                    if best is None or d < best[0]:
                        best = (d, g)
                if best is not None and best[0] <= tap_cap_m:
                    if best[1] not in picked:
                        picked.append(best[1])
                    taps += 1
        if not picked:
            return fallback_geom
        if feedback is not None:
            feedback.pushInfo(
                "  distribution duct corridor: %d of %d region span(s) "
                "(%d drop tap(s) bound)." % (len(picked), len(cand), taps))
        try:
            from ..utils.geometry_ops import unary_union_geoms as _uug_corr
            corr = _uug_corr(picked)
        except Exception:
            return fallback_geom
        if corr is None or corr.isEmpty():
            return fallback_geom
        # Sanity: the corridor must still be the one the cables ride.
        if fallback_geom is not None and not fallback_geom.isEmpty():
            try:
                if corr.distance(fallback_geom) > 0.5:
                    return fallback_geom
            except Exception:
                pass
        return corr

    # How far apart two trench vertices may be and still count as joined when
    # a tap routes along the network. 0 = only vertices that genuinely coincide
    # (the published network's own drafting). Raising it bridges GAPS in the
    # trench layer, and every bridge is a straight line that is by definition
    # **off the trench** — so it is a last resort, measured not assumed.
    ROUTE_JOIN_TOL_M = 0.0

    # How far a vertex may sit off another span's geometry and still be docked
    # onto it. This is a *drafting* tolerance, not a gap bridge: the unioned
    # trench shares its junctions to within 0.07 m (measured on Berlin: 92 of 93
    # severed junctions are a vertex lying 0.0000-0.07 m off another span, and
    # NONE of them is a real gap), so docking costs at most a few centimetres
    # per junction and makes the graph the ONE network the trench actually is.
    # ROUTE_JOIN_TOL_M is the deliberate gap bridge and stays 0.
    ROUTE_DOCK_TOL_M = 0.25

    # A routed tap may be longer than the chord it replaces — it has to run to
    # the corner and back out to the coupler — but only by a *corner's* worth.
    # These bound that: a route longer than ``x * chord + slack`` is not the path
    # between the two points, and the caller falls back to its own rule. The cap
    # exists because the alternative is worse than a short chord: measured on
    # Berlin, a 7.5 m tap had a 193 m network route, and 79 routed taps summed
    # to 5,958.8 m of duct to replace 1,441.5 m of chord.
    ROUTE_DETOUR_MAX_X = 3.0
    ROUTE_DETOUR_SLACK_M = 30.0

    def _route_network(self, corridor_lyr):
        """The trench network as a routed graph — built once, then cached.

        Returns ``(adj, edge_geom, edge_len, node_xy, segs)`` where ``segs`` is
        ``(QgsPointXY p, QgsPointXY q, key_p, key_q)`` per segment. The lines
        are **unioned first** so that trenches crossing mid-segment are noded
        there: a crossing is where two trenches meet in reality, and without
        noding the graph would be severed at every one of them.

        Noding crossings is not enough on its own. The union gives a junction a
        vertex in the part that was split, but the span running THROUGH the
        junction keeps going without one, so a graph built on vertices alone is
        still severed there: on Berlin that left the trench looking like 93
        pieces, 92 of which touch another piece to within 1 mm (worst 0.07 m) —
        the trench was one network all along and only the graph disagreed.
        ``ROUTE_DOCK_TOL_M`` splices a vertex onto a passing span; that is what
        joins those junctions. ``ROUTE_JOIN_TOL_M`` stays 0: bridging a gap that
        is not a drafting artifact would draw duct where there is no trench.
        """
        cache = getattr(self, "_net_cache", None)
        if cache is None:
            cache = self._net_cache = {}
        ckey = id(corridor_lyr)
        if ckey in cache:
            return cache[ckey]

        geoms = []
        if corridor_lyr is not None:
            for cf in corridor_lyr.getFeatures():
                g = cf.geometry()
                if g is not None and not g.isEmpty():
                    geoms.append(g)
        net = None
        if geoms:
            try:
                from ..utils.geometry_ops import unary_union_geoms as _uug_net
                unioned = _uug_net(geoms)
                parts = []
                if unioned is not None and not unioned.isEmpty():
                    ml = unioned.asMultiPolyline()
                    if ml:
                        parts = [p for p in ml if len(p) >= 2]
                    else:
                        pl = unioned.asPolyline()
                        if pl and len(pl) >= 2:
                            parts = [pl]
                if parts:
                    adj = defaultdict(list)
                    edge_geom = {}
                    edge_len = {}
                    node_xy = {}
                    segs = []
                    for pl in parts:
                        prev = pl[0]
                        kp = round_key_xy(prev.x(), prev.y())
                        node_xy.setdefault(kp, prev)
                        for pt in pl[1:]:
                            kq = round_key_xy(pt.x(), pt.y())
                            node_xy.setdefault(kq, pt)
                            if kp != kq:
                                seg = QgsGeometry.fromPolylineXY(
                                    [QgsPointXY(prev.x(), prev.y()),
                                     QgsPointXY(pt.x(), pt.y())])
                                add_edge(adj, edge_geom, edge_len, kp, kq, seg)
                                segs.append((prev, pt, kp, kq))
                            prev = pt
                            kp = kq
                    if segs and self.ROUTE_JOIN_TOL_M > 0.0:
                        self._join_near_nodes(adj, edge_geom, edge_len,
                                              node_xy, self.ROUTE_JOIN_TOL_M)
                    if segs and self.ROUTE_DOCK_TOL_M > 0.0:
                        self._dock_nodes_to_segments(adj, edge_geom, edge_len,
                                                     node_xy, segs,
                                                     self.ROUTE_DOCK_TOL_M)

                    if segs:
                        net = (adj, edge_geom, edge_len, node_xy, segs)
            except Exception:
                net = None
        cache[ckey] = net
        return net

    @staticmethod
    def _join_near_nodes(adj, edge_geom, edge_len, node_xy, tol_m):
        """Join vertices of different components that are within ``tol_m``.

        Each join adds a straight edge of the gap's own length, which is not
        trench geometry — so keep ``tol_m`` at drafting-gap scale.
        """
        cell = max(1.0, tol_m)
        grid = defaultdict(list)
        for k, p in node_xy.items():
            grid[(int(p.x() // cell), int(p.y() // cell))].append(k)
        for k, p in list(node_xy.items()):
            gx, gy = int(p.x() // cell), int(p.y() // cell)
            best = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for m in grid.get((gx + dx, gy + dy), ()):
                        if m == k:
                            continue
                        q = node_xy[m]
                        d = math.hypot(q.x() - p.x(), q.y() - p.y())
                        if d <= tol_m and (best is None or d < best[0]):
                            best = (d, m)
            if best is not None:
                q = node_xy[best[1]]
                seg = QgsGeometry.fromPolylineXY(
                    [QgsPointXY(p.x(), p.y()), QgsPointXY(q.x(), q.y())])
                add_edge(adj, edge_geom, edge_len, k, best[1], seg)

    @classmethod
    def _dock_nodes_to_segments(cls, adj, edge_geom, edge_len, node_xy, segs,
                                tol_m):
        """Splice every vertex that lies on a passing span onto that span.

        Only spans that do not already share the vertex are considered, so a
        junction the union DID node is left alone (its own segments are 0 m
        away and would otherwise win every tie). The spliced point is joined to
        both halves of the span it landed on, which makes the two spans one,
        and to the vertex itself, which costs at most ``tol_m`` of straight
        line — the distance between them, which is the drafting slop.

        Returns the number of vertices docked (for the caller to report).
        """
        docked = 0
        for k, p in list(node_xy.items()):
            best = None
            for a, b, kp, kq in segs:
                if kp == k or kq == k:
                    continue
                d, fx, fy = DuctLayer._pt_to_segment(p.x(), p.y(), a, b)
                if d <= tol_m and (best is None or d < best[0]):
                    best = (d, fx, fy, a, b, kp, kq)
            if best is None:
                continue
            _d, fx, fy, a, b, kp, kq = best
            kx = round_key_xy(fx, fy)
            if kx == kp or kx == kq:
                continue          # already an endpoint of that span
            # When the vertex is ON the passing span (millimetres, which is the
            # usual case) the projection rounds back onto the vertex's own key:
            # the junction is then that one node, and the two spans join exactly
            # there. Only a vertex genuinely part-way along the span gets a node
            # of its own.
            if kx != k:
                node_xy[kx] = QgsPointXY(fx, fy)
            for kk, pb in ((kp, a), (kq, b)):
                if kx == kk:
                    continue
                seg = QgsGeometry.fromPolylineXY(
                    [QgsPointXY(fx, fy), QgsPointXY(pb.x(), pb.y())])
                add_edge(adj, edge_geom, edge_len, kx, kk, seg)
            if kx != k:
                seg = QgsGeometry.fromPolylineXY(
                    [QgsPointXY(p.x(), p.y()), QgsPointXY(fx, fy)])
                add_edge(adj, edge_geom, edge_len, k, kx, seg)
            docked += 1
        return docked

    @staticmethod
    def _pt_to_segment(px, py, p, q):
        """Distance from (px,py) to segment p→q, and the point on it."""
        ax, ay, bx, by = p.x(), p.y(), q.x(), q.y()
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        if L2 <= 1e-12:
            return math.hypot(px - ax, py - ay), ax, ay
        t = ((px - ax) * dx + (py - ay) * dy) / L2
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        fx, fy = ax + t * dx, ay + t * dy
        return math.hypot(px - fx, py - fy), fx, fy

    def _attach_to_network(self, net, xy, tol_m):
        """Put a point on the routed network: (node key, point) or (None, None).

        The projected point is spliced into the segment it falls on by adding
        the two sub-edges, so a route can start/end exactly there without
        re-noding — and without mutating the cached network (later calls simply
        find the same splice already present).
        """
        adj, edge_geom, edge_len, node_xy, segs = net
        px, py = xy[0], xy[1]
        best = None
        for p, q, kp, kq in segs:
            d, fx, fy = self._pt_to_segment(px, py, p, q)
            if best is None or d < best[0]:
                best = (d, fx, fy, p, q, kp, kq)
        if best is None or best[0] > tol_m:
            return None, None
        _d, fx, fy, p, q, kp, kq = best
        kx = round_key_xy(fx, fy)
        node_xy[kx] = QgsPointXY(fx, fy)
        for _kk, pb in ((kp, p), (kq, q)):
            if kx == _kk:
                continue
            seg = QgsGeometry.fromPolylineXY(
                [QgsPointXY(fx, fy), QgsPointXY(pb.x(), pb.y())])
            add_edge(adj, edge_geom, edge_len, kx, _kk, seg)
        return kx, QgsPointXY(fx, fy)

    def _trench_route(self, corridor_lyr, a_xy, b_xy, tol_m=1.0):
        """Shortest path ALONG the trench network between two points on it.

        A tap has to follow the trench (rule D10) and the coupler it reaches
        can sit on a **different trench feature** from the duct, so the
        connector is a route over the network — not a clip of one feature and
        certainly not a chord. Both ends of every tap are demonstrably on the
        network (the 295 `Pseudo_HH` points measure 0.000 m from a trench, and
        so does the duct), so a route exists whenever the two are reachable
        from each other; a coupler behind a break in the design still falls
        back, and the caller says so.

        Returns a ``QgsGeometry`` polyline from ``a_xy`` to ``b_xy``, or None.
        """
        net = self._route_network(corridor_lyr)
        if net is None:
            return None
        adj, edge_geom, _edge_len, node_xy, _segs = net
        ka, pa = self._attach_to_network(net, a_xy, tol_m)
        kb, pb = self._attach_to_network(net, b_xy, tol_m)
        if ka is None or kb is None:
            return None
        if ka == kb:
            return QgsGeometry.fromPolylineXY([pa, pb])
        _dist, parent = dijkstra_with_parents(ka, adj)
        if kb not in parent:
            return None
        seg_path = reconstruct_path(parent, kb, ka)
        if not seg_path:
            return None
        coords = [QgsPointXY(pa.x(), pa.y())]
        cur = ka
        total = 0.0
        for seg_id in seg_path:
            u, v = seg_id
            nxt = v if u == cur else u
            nx, ny = node_xy.get(nxt, (None, None))
            if nx is None:
                return None
            coords.append(QgsPointXY(nx, ny))
            e = edge_geom.get(seg_id)
            if e is not None:
                total += e.length()
            cur = nxt
        coords.append(QgsPointXY(pb.x(), pb.y()))
        # Once both endpoints are on the same connected trench network, the
        # network path is authoritative even when it is longer than the chord.
        # A chord would put duct outside the trench, which is forbidden by D10.
        path = QgsGeometry.fromPolylineXY(coords)
        return None if path.isEmpty() else path

    def _rebase_distribution_output(self, output_uri, trench_lyr, context, feedback):
        """Keep legacy PDP/pseudo grouping but draw every line on Final_Trenches.

        The legacy distribution algorithm is still the source of topology and
        attributes. Its sidewalk graph is only a planning graph; it is not the
        civil route. Rebase each resulting line between its endpoint projections
        on the actual trench network before enrichment/chamber segmentation.
        """
        if not output_uri or trench_lyr is None:
            return 0, 0
        layer = QgsProcessingUtils.mapLayerFromString(str(output_uri), context)
        if layer is None or not layer.isValid():
            layer = QgsVectorLayer(str(output_uri), "distribution_rebase", "ogr")
        if layer is None or not layer.isValid():
            return 0, 0
        changed = unresolved = 0
        if not layer.isEditable():
            layer.startEditing()
        for f in layer.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                unresolved += 1
                continue
            source_parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
            source_parts = [part for part in source_parts if len(part) >= 2]
            if not source_parts:
                unresolved += 1
                continue
            rebased_parts = []
            try:
                for part in source_parts:
                    a, b = part[0], part[-1]
                    na = nb = None
                    for tf in trench_lyr.getFeatures():
                        tg = tf.geometry()
                        if tg is None or tg.isEmpty():
                            continue
                        pa = QgsGeometry.fromPointXY(QgsPointXY(a))
                        pb = QgsGeometry.fromPointXY(QgsPointXY(b))
                        ca = tg.nearestPoint(pa)
                        cb = tg.nearestPoint(pb)
                        da, db = ca.distance(pa), cb.distance(pb)
                        if na is None or da < na[0]:
                            na = (da, ca.asPoint())
                        if nb is None or db < nb[0]:
                            nb = (db, cb.asPoint())
                    if na is None or nb is None:
                        continue
                    route = self._trench_route(
                        trench_lyr, (na[1].x(), na[1].y()),
                        (nb[1].x(), nb[1].y()), tol_m=50.0)
                    if route is not None and not route.isEmpty():
                        rebased_parts.extend(
                            route.asMultiPolyline() if route.isMultipart()
                            else [route.asPolyline()])
                if not rebased_parts:
                    unresolved += 1
                    continue
                f.setGeometry(QgsGeometry.fromMultiPolylineXY(rebased_parts))
                layer.updateFeature(f)
                changed += 1
            except Exception:
                unresolved += 1
        if changed:
            layer.commitChanges()
        if feedback:
            feedback.pushInfo(
                f"Distribution hybrid rebase: {changed} legacy route(s) moved onto "
                f"Final_Trenches; {unresolved} unresolved route(s) left for review.")
        return changed, unresolved

    def _trench_connector(self, corridor_lyr, a_xy, b_xy, tol_m=1.0):
        """The trench path between two points: routed first, one feature second.

        ``_trench_route`` follows the whole network (the general case);
        ``_trench_link`` clips a single feature (cheaper, and the only option if
        the network could not be noded). None means neither found a trench
        carrying both ends, and the caller falls back to a straight chord —
        counted and logged, never silent.
        """
        try:
            route = self._trench_route(corridor_lyr, a_xy, b_xy, tol_m)
        except Exception:
            route = None
        if route is not None:
            return route
        return self._trench_link(corridor_lyr, a_xy, b_xy, tol_m)

    def _trench_link(self, corridor_lyr, a_xy, b_xy, tol_m=1.0):
        """The TRENCH path between two points, or None when none carries both.

        A duct may only ever be drawn ON the trench network (rule D10), so a
        connector between two points of the design has to be the piece of
        trench running between them — not the straight chord between them.
        Returns the SHORTEST single trench feature that comes within ``tol_m``
        of both points, clipped to the stretch between them; None when no one
        trench carries both (the caller then falls back to its own rule).
        """
        if corridor_lyr is None:
            return None
        pa = QgsGeometry.fromPointXY(QgsPointXY(a_xy[0], a_xy[1]))
        pb = QgsGeometry.fromPointXY(QgsPointXY(b_xy[0], b_xy[1]))
        try:
            straight = math.hypot(b_xy[0] - a_xy[0], b_xy[1] - a_xy[1])
        except Exception:
            straight = 0.0
        best = None
        for cf in corridor_lyr.getFeatures():
            g = cf.geometry()
            if g is None or g.isEmpty():
                continue
            try:
                if g.distance(pa) > tol_m or g.distance(pb) > tol_m:
                    continue
                ta = g.lineLocatePoint(pa)
                tb = g.lineLocatePoint(pb)
            except Exception:
                continue
            lo, hi = (ta, tb) if ta <= tb else (tb, ta)
            if hi - lo <= 0.01:
                continue
            try:
                seg = geom_substring(g, lo, hi)
            except Exception:
                continue
            if seg is None or seg.isEmpty() or seg.length() <= 0.01:
                continue
            # Do not reject a genuine trench detour: replacing it with a chord
            # would create duct geometry where no trench exists.  The connected
            # network route is always preferable to an off-trench shortcut.
            if best is None or seg.length() < best.length():
                best = seg
        return best

    def _attach_taps(self, duct_geom, tap_lyr, poly_ids, tol_m, feedback,
                     corridor_lyr=None):
        """Extend a distribution duct so it REACHES every pseudo-HH (coupler).

        A coupler is the joint where a drop duct leaves the distribution duct
        (HLD review), so the distribution duct must physically pass through it.
        Building the duct from the trunk cables alone left it merely *near* the
        footway points (Berlin: only 102 of 292 couplers sat on a distribution
        duct, some 40 m away), because the spine does not run down every street
        the drop legs start on.

        For every pseudo point in the duct's own region that is farther than
        ``tol_m``, the duct is extended to it — **along the trench** when one
        trench carries both the duct's closest point and the coupler
        (``_trench_link``), and by a straight spur only when none does. The
        first version always drew the straight spur, and that is where the
        ducts stopped matching the trench: measured on the 2026-09-21 Berlin
        run, 2,076 m of the distribution layer (21 % of its length) was made of
        single-segment chords up to 123 m long lying 1-21 m off the network.
        The extension may run on the FEEDER corridor — allowed on purpose: the
        distribution duct is not required to keep off the feeder path, it is
        only required to reach the drop joints and stay on a trench.
        """
        if tap_lyr is None or duct_geom is None or duct_geom.isEmpty():
            return duct_geom
        names = tap_lyr.fields().names()
        f_poly = next((n for n in ("POLYGON_ID", "polygon_id")
                       if n in names), None)
        wanted = {str(v).strip().upper() for v in (poly_ids or []) if str(v).strip()}
        spurs = []
        reached = 0
        trenched = 0
        off_trench = 0
        for pf in tap_lyr.getFeatures():
            pg = pf.geometry()
            if pg is None or pg.isEmpty():
                continue
            if wanted and f_poly is not None:
                pv = str(pf[f_poly] or "").strip().upper()
                # A point with no region tag follows its own duct; one tagged
                # with a DIFFERENT region is served by that region's duct.
                if pv and pv not in wanted:
                    continue
            try:
                pt = pg.asPoint() if not pg.isMultipart() else pg.centroid().asPoint()
            except Exception:
                continue
            pgeom = QgsGeometry.fromPointXY(QgsPointXY(pt.x(), pt.y()))
            if duct_geom.distance(pgeom) <= tol_m:
                reached += 1
                continue
            try:
                near = duct_geom.nearestPoint(pgeom)
            except Exception:
                continue
            if near is None or near.isEmpty():
                continue
            try:
                npt = near.asPoint()
            except Exception:
                continue
            if npt == pt:
                continue
            link = self._trench_connector(corridor_lyr, (npt.x(), npt.y()),
                                          (pt.x(), pt.y()))
            if link is not None:
                spurs.append(link)
                trenched += 1
            else:
                # Never draw a straight off-trench shortcut.  A disconnected
                # region is reported for the next design pass instead of
                # violating D10 or silently creating a duct through premises.
                off_trench += 1
        if not spurs:
            return duct_geom
        try:
            from ..utils.geometry_ops import unary_union_geoms as _uug_tap
            merged = _uug_tap([duct_geom] + spurs)
            if merged is not None and not merged.isEmpty():
                note = ""
                if off_trench:
                    note = (f" {off_trench} had no single trench carrying both "
                            f"ends and used a straight connector.")
                feedback.pushInfo(
                    f"  distribution duct taps: {len(spurs)} tap(s) added so "
                    f"every pseudo-HH/coupler sits on a duct "
                    f"({reached} already on it; {trenched} follow the trench)."
                    + note)
                return merged
        except Exception:
            pass
        return duct_geom

    def _build_route_ducts(self, cables_lyr, out_uri, profile_key, crs,
                           context, feedback, subtract_lyr=None, runs_uri=None,
                           skip_cable_types=(), tap_lyr=None, tap_tol_m=0.5,
                           corridor_lyr=None):
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

        # ── Distribution is grouped BY REGION, not by cable proximity ─────
        # Rule D5: a distribution duct connects its own PDP to the pseudo
        # object points of ITS polygon. Grouping by proximity produced 23
        # ducts for 31 polygons — **8 regions had no duct of their own** (60
        # pseudo points, 37 of them on no duct at all) and the ducts that did
        # exist crossed into a neighbour to serve its joints (measured on the
        # 2026-09-21 Berlin run: 699.2 m of cross-serving, 2,645.9 m of the
        # layer outside the polygon its own row named). Grouping by
        # POLYGON_ID gives every region its own duct, laid from that region's
        # own cables and trench, so the region rule holds by construction
        # rather than being repaired afterwards.
        region_untagged = 0
        if profile_key == "Distribution" and fld_poly is not None:
            by_region = defaultdict(list)
            for i, f in enumerate(feats):
                v = f.attribute(fld_poly)
                pid = ""
                if v is not None:
                    pid = str(v).replace(";", ",").split(",")[0].strip().upper()
                if not pid:
                    region_untagged += 1
                by_region[pid].append(i)
            if by_region:
                groups = by_region
                try:
                    feedback.pushInfo(
                        "  distribution duct grouping: by POLYGON_ID — %d "
                        "region(s), %d cable(s) carry no region tag and are "
                        "grouped together."
                        % (len([k for k in by_region if k]), region_untagged))
                except Exception:
                    pass

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
        uncovered = 0        # pseudo points no duct reached (see the pass below)
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
                # Distribution cable rows from the legacy topology do not
                # always carry POLYGON_ID. Recover the region from the PDPs
                # recorded on the row and the pseudo-object layer before
                # choosing a corridor; otherwise _corridor_for falls back to
                # the legacy cable geometry, which can run all the way to an
                # object instead of ending at the pseudo-object trunk points.
                if profile_key == "Distribution" and not poly_set and tap_lyr is not None:
                    tap_names = tap_lyr.fields().names()
                    tap_pdp = next((n for n in tap_names
                                    if n.lower() in ("pdp_id", "pdp_ids")), None)
                    tap_poly = next((n for n in tap_names
                                     if n.lower() == "polygon_id"), None)
                    wanted_pdp = {
                        str(v).strip().upper()
                        for v in pdp_set if str(v).strip()
                    }
                    if tap_pdp and tap_poly and wanted_pdp:
                        for tf in tap_lyr.getFeatures():
                            tv = str(tf[tap_pdp] or "").strip().upper()
                            if tv in wanted_pdp:
                                pv = str(tf[tap_poly] or "").strip().upper()
                                if pv and pv not in poly_set:
                                    poly_set.append(pv)

                # Distribution: lay the duct in the region's TRENCH corridor
                # (that is where the drop legs tap it), not in the cable's
                # merged spine — then make sure it reaches every pseudo-HH
                # (coupler): see _corridor_for / _attach_taps.
                if corridor_lyr is not None:
                    ug = self._corridor_for(corridor_lyr, poly_set, ug, tap_tol_m,
                                            tap_lyr=tap_lyr, feedback=feedback)
                if tap_lyr is not None:
                    # Only pseudo-object points are valid distribution trunk
                    # endpoints. Household/object points belong exclusively to
                    # Drop_Ducts and must never extend a distribution duct.
                    ug = self._attach_taps(ug, tap_lyr, poly_set, tap_tol_m,
                                           feedback, corridor_lyr=corridor_lyr)
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

        # ── MISSING REGION SPINES: build the trunk even without a cable row ─
        # A region can have pseudo points and a valid trench corridor before the
        # cable planner has emitted a trunk cable (for example a newly created
        # polygon or a sparse PDP). Do not let that erase the region's duct:
        # the distribution rule is PDP/polygon -> every pseudo point. Build one
        # corridor duct from the region's non-garden trench spans and let the
        # same tap and chamber passes finish it.
        if profile_key == "Distribution" and corridor_lyr is not None and tap_lyr is not None:
            region_names = set()
            for _g, _ci, _pd, polys0, _nc in bins:
                region_names.update(str(v).strip().upper() for v in polys0 if str(v).strip())
            tap_poly_name = next((n for n in tap_lyr.fields().names()
                                  if n.lower() == "polygon_id"), None)
            missing_regions = set()
            for pf in tap_lyr.getFeatures():
                if tap_poly_name is None:
                    continue
                pv = str(pf[tap_poly_name] or "").strip().upper()
                if pv and pv not in region_names:
                    missing_regions.add(pv)
            for pv in sorted(missing_regions):
                region_parts = []
                for cf in corridor_lyr.getFeatures():
                    names_c = cf.fields().names()
                    poly_c = next((n for n in names_c if n.lower() == "polygon_id"), None)
                    tier_c = next((n for n in names_c if n.lower() == "trench_tier"), None)
                    if poly_c is None or str(cf[poly_c] or "").strip().upper() != pv:
                        continue
                    if tier_c and "garden" in str(cf[tier_c] or "").strip().lower():
                        continue
                    cg = cf.geometry()
                    if cg is not None and not cg.isEmpty():
                        region_parts.append(cg)
                if not region_parts:
                    continue
                from ..utils.geometry_ops import unary_union_geoms as _uug_missing
                spine = _uug_missing(region_parts)
                if spine is None or spine.isEmpty():
                    continue
                spine = self._attach_taps(spine, tap_lyr, [pv], tap_tol_m,
                                           feedback, corridor_lyr=corridor_lyr)
                pids = []
                for pf in tap_lyr.getFeatures():
                    if tap_poly_name and str(pf[tap_poly_name] or "").strip().upper() == pv:
                        for nm in tap_lyr.fields().names():
                            if nm.lower() == "pdp_id" and str(pf[nm] or "").strip():
                                pids.append(str(pf[nm]).strip().upper())
                                break
                bins.append((spine, [], list(dict.fromkeys(pids)), [pv], 0))
                if runs_sink is not None:
                    nf = QgsFeature(run_fields)
                    nf.setGeometry(spine)
                    nf["DUCT_TYPE"] = prof.get("duct_type", "2-Way HDPE")
                    nf["capacity_total"] = ways
                    nf["ways_used"] = 0
                    nf["cables_carried"] = ""
                    nf["pdp_ids"] = ",".join(dict.fromkeys(pids))
                    nf["POLYGON_ID"] = pv
                    nf["length_m"] = round(float(spine.length()), 2)
                    nf["REVIEW"] = 1
                    nf["INFRA_STATUS"] = "Proposed"
                    runs_sink.addFeature(nf, QgsFeatureSink.FastInsert)
                made += 1
            if missing_regions:
                feedback.pushInfo(
                    f"  distribution duct spines: built {len(missing_regions)} "
                    "region spine(s) that had pseudo points but no cable trunk.")

        # ── FINAL COVERAGE: every pseudo-HH must sit on SOME duct ─────────
        # The per-region pass above only attaches a point to the duct carrying
        # its own POLYGON_ID, so a point whose tag disagrees with the cable's
        # (or a region with no duct at all) stayed disconnected — Berlin: 264
        # of 292 couplers on a duct. A coupler is the drop duct's joint, so a
        # leftover point is attached to the NEAREST duct instead of none.
        if tap_lyr is not None and bins:
            names_t = tap_lyr.fields().names()
            t_poly = next((n for n in ("POLYGON_ID", "polygon_id") if n in names_t), None)
            for pf in tap_lyr.getFeatures():
                pg = pf.geometry()
                if pg is None or pg.isEmpty():
                    continue
                tag = ""
                if t_poly is not None:
                    tag = str(pf[t_poly] or "").strip().upper()
                best = None
                for i_b, (bg, _ci, _pd, _po, _nc) in enumerate(bins):
                    # Region guard: a leftover point joins a duct of ITS OWN
                    # region only. Attaching it to a neighbouring region's duct
                    # is the cross-serving rule D5 forbids — and with the tier
                    # grouped by POLYGON_ID every region has its own duct, so
                    # this pass is a safety net rather than the main path.
                    if tag and tag not in {str(p).strip().upper()
                                           for p in (_po or [])}:
                        continue
                    try:
                        d = bg.distance(pg)
                    except Exception:
                        continue
                    if best is None or d < best[0]:
                        best = (d, i_b)
                if best is None or best[0] <= tap_tol_m:
                    continue
                try:
                    near = bins[best[1]][0].nearestPoint(pg)
                    npt = near.asPoint()
                    pt = pg.asPoint()
                except Exception:
                    continue
                bg, ci, pd, po, nc = bins[best[1]]
                try:
                    from ..utils.geometry_ops import unary_union_geoms as _uug_cov
                    # The point is on the trench (measured: couplers 0.00 m from
                    # it), so the duct reaches it ALONG the trench NETWORK —
                    # routed across features where it has to be. The straight
                    # chord is the last resort, because it leaves the network.
                    link = self._trench_connector(corridor_lyr,
                                                  (npt.x(), npt.y()),
                                                  (pt.x(), pt.y()))
                    if link is None:
                        continue
                    merged = _uug_cov([bg, link])
                    if merged is not None and not merged.isEmpty():
                        bins[best[1]] = (merged, ci, pd, po, nc)
                        uncovered += 1
                        if tag:
                            po = list(po) + [tag]
                            bins[best[1]] = (merged, ci, pd, po, nc)
                except Exception:
                    continue
            if uncovered:
                feedback.pushInfo(
                    "  distribution duct coverage: %d pseudo-HH point(s) had no "
                    "duct in their own region and were attached to the nearest "
                    "one OF THEIR OWN REGION (a coupler must sit on a duct)."
                    % uncovered)

        # ── ONE FEATURE PER DUCT ──────────────────────────────────────────
        # The bins above are the ducts actually laid: a route carrying more
        # cables than the profile holds is built as several {ways}-way ducts
        # side by side.  Each bin is published as its OWN feature — that is the
        # unit the field installs, it carries its own cable list, its own
        # ways/capacity (so a 4-way feeder duct never reports the aggregate of
        # every feeder duct in the tier), and it is what the chamber pass
        # afterwards pulls chamber-to-chamber.
        #
        # Material quantity stays exact: BUNDLE_LEN_M on each row is that
        # duct's own length, so the BOQ sum is unchanged from the clubbed
        # corridor figure (verified on the Berlin run).  N_DUCTS=1 (this row
        # IS one duct), WAYS_TOTAL = the profile ways, CLUBS = how many
        # parallel ducts share this route group (a routing/viewing hint, not
        # material).
        from ..utils.geometry_ops import unary_union_geoms as _uug_club
        agg_len = 0.0          # sum of the ducts laid = material metres
        corridor_len = 0.0
        agg_used = 0
        agg_cables, agg_pdps, agg_polys = [], [], []
        bin_geoms = []
        for i_bin, (ug, cable_ids, pdp_set, poly_set, n_cab) in enumerate(bins, 1):
            bin_geoms.append(ug)
            try:
                bin_len = round(float(ug.length()), 2)
            except Exception:
                bin_len = 0.0
            agg_len += bin_len
            agg_used += n_cab
            agg_cables.extend(cable_ids)
            agg_pdps.extend(pdp_set)
            agg_polys.extend(poly_set)
            nf = QgsFeature(fields)
            nf.setGeometry(ug)
            nf["DUCT_TYPE"] = prof.get("duct_type", "4-Way HDPE" if profile_key == "Feeder" else "2-Way HDPE")
            nf["capacity_total"] = ways
            nf["ways_used"] = int(n_cab)
            nf["cables_carried"] = ",".join(cable_ids)
            nf["pdp_ids"] = ",".join(dict.fromkeys(pdp_set))
            nf["POLYGON_ID"] = ",".join(dict.fromkeys(poly_set))
            nf["length_m"] = bin_len
            nf["BUNDLE_LEN_M"] = bin_len
            nf["N_DUCTS"] = 1
            nf["WAYS_TOTAL"] = int(ways)
            nf["CLUBS"] = len(bins)
            nf["REVIEW"] = 1 if n_cab > ways else 0
            nf["INFRA_STATUS"] = "Proposed"
            nf["DUCT_ID"] = f"{profile_key.upper()}-DUCT-{i_bin:03d}"
            sink.addFeature(nf, QgsFeatureSink.FastInsert)

        club_geom = _uug_club(bin_geoms) if bin_geoms else None
        if club_geom is not None and not club_geom.isEmpty():
            corridor_len = float(club_geom.length())

        if sink:
            del sink
        if runs_sink:
            del runs_sink
        feedback.pushInfo(
            f"✅ Route ducts ({profile_key}): {made} x {ways}-way duct(s) from "
            f"{len(feats)} cables over {len(groups)} route group(s) "
            f"(cables split into {ways}-way ducts, {flag_cnt} oversized) → "
            f"{len(bins)} duct feature(s) spanning a {corridor_len:,.1f} m "
            f"corridor, {agg_len:,.1f} m of duct material "
            f"({int(ways)} ways each, {agg_used} ways used).")
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
        # The approved legacy distribution design is the strict PDP→pseudo-HH
        # graph below. The newer cable-route clubber changes the topology and
        # does not match the operator's reference output (project
        # 5e26084f...). Keep it available for experiments, but do not select it
        # for the production HLD output.
        USE_ROUTE_BASED_DISTRIBUTION = False
        dist_cables = _as_layer_any(self.P_DIST_CABLES,
                                    fallback_names=["Distribution_Cable", "Distribution_Cables"])
        if USE_ROUTE_BASED_DISTRIBUTION and dist_cables is not None and dist_cables.featureCount() > 0:
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
                    skip_cable_types=("Drop", "Garden"),
                    # Every pseudo-HH point (where a coupler joins the drop
                    # duct) must sit ON the distribution duct, and the duct is
                    # free to co-route on the feeder path to get there.
                    tap_lyr=pseudo_lyr, tap_tol_m=0.5,
                    # ...and the duct is laid in the region's trench corridor,
                    # which is the surface the couplers sit on.
                    corridor_lyr=net_lyr)
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
            # Preserve the legacy PDP/pseudo grouping, then replace only its
            # sidewalk geometry with routes on the actual trench network.
            self._rebase_distribution_output(
                out_distr_uri, net_lyr, context, feedback)
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
