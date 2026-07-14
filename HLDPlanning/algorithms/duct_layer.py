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
    QgsVectorLayer,
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
            defaultValue=4, minValue=2, maxValue=16
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

        net_fix = processing.run("native:fixgeometries",
                                 {"INPUT": net, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                                 context=context, feedback=feedback)["OUTPUT"]
        net_single = processing.run("native:multiparttosingleparts",
                                    {"INPUT": net_fix, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                                    context=context, feedback=feedback)["OUTPUT"]

        try:
            inter_pts = processing.run("native:lineintersections",
                {"INPUT": net_single, "INTERSECT": net_single, "INPUT_FIELDS": [], "INTERSECT_FIELDS": [], "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                context=context, feedback=feedback)["OUTPUT"]
        except Exception:
            inter_pts = processing.run("qgis:lineintersections",
                {"INPUT": net_single, "INTERSECT": net_single, "INPUT_FIELDS": [], "INTERSECT_FIELDS": [], "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                context=context, feedback=feedback)["OUTPUT"]

        inter_pts = processing.run("native:deleteduplicategeometries",
                                   {"INPUT": inter_pts, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                                   context=context, feedback=feedback)["OUTPUT"]

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
                    fb["part"]      = f"branch{bi}_duct{duct_idx}"
                    fb["color"]     = branch_color
                    fb["edge_cnt"]  = int(len(far_suf))
                    fb["length_m"]  = float(far_len)
                    fb["duct_idx"]  = int(duct_idx)
                    sink.addFeature(fb, QgsFeatureSink.FastInsert)
                    made += 1
                    duct_idx += 1

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
    O_FEEDER  = "OUT_FEEDER_DUCTS"
    O_DISTR   = "OUT_DISTRIBUTION_DUCTS"

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
        self.addParameter(QgsProcessingParameterVectorDestination(self.O_FEEDER, "Feeder_Ducts"))
        self.addParameter(QgsProcessingParameterVectorDestination(self.O_DISTR, "Distribution_Ducts"))

    def processAlgorithm(self, p, context, feedback):
        # Resolve parent output URIs up front
        out_feeder_uri = self.parameterAsOutputLayer(p, self.O_FEEDER, context)
        out_distr_uri  = self.parameterAsOutputLayer(p, self.O_DISTR,  context)

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
        feeder.processAlgorithm(feeder_params, context, feedback)
        
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
        obj_lyr = self.parameterAsVectorLayer(p, self.P_OBJECTS, context) or self.parameterAsSource(p, self.P_OBJECTS, context)
        # Auto-detect canonical fields on the resolved layers
        hh_fld   = first_field_case_insensitive(obj_lyr, ["ADDR_ID", "addr_id", "HH_ID", "hh_id", "address_id", "id"]) or "" if obj_lyr else ""
        pdp_on_p = first_field_case_insensitive(pdp_lyr, ["PDP_ID", "pdp_id", "pdp"]) or "" if pdp_lyr else ""
        pdp_on_h = first_field_case_insensitive(obj_lyr, ["PDP_ID", "pdp_id"]) or "" if obj_lyr else ""
        feedback.pushInfo(f"Distribution auto-detected fields → HH id: '{hh_fld}', PDP id on PDPs: '{pdp_on_p}', PDP id on HH: '{pdp_on_h}'")

        # --- replace the whole validation block with this ---
        def _field_exists(lyr, fld):
            return fld in [f.name() for f in lyr.fields()] if lyr and fld else False

        errs = []
        if pdp_lyr is None:  errs.append("Distribution: PDP_POINTS layer is NULL (check the input).")
        if obj_lyr is None:  errs.append("Distribution: OBJECT_POINTS layer is NULL (check the input).")
        if side_l is None or side_r is None:
            try:
                feedback.pushWarning("Distribution: Sidewalk L/R not provided — will build graph from tangent/feeder only and default side labels.")
            except Exception:
                pass
            
        if not pdp_on_p:     errs.append("Distribution: PDP ID field on PDPs is empty.")
        if not pdp_on_h:     errs.append("Distribution: PDP ID field on HH/Objects is empty.")
        if not hh_fld:       errs.append("Distribution: HH ID field on HH/Objects is empty.")

        if pdp_lyr and not _field_exists(pdp_lyr, pdp_on_p):
            errs.append(f"Distribution: PDP_POINTS is missing field '{pdp_on_p}'.")
        if obj_lyr and not _field_exists(obj_lyr, pdp_on_h):
            errs.append(f"Distribution: OBJECT_POINTS is missing field '{pdp_on_h}'.")
        if obj_lyr and not _field_exists(obj_lyr, hh_fld):
            errs.append(f"Distribution: OBJECT_POINTS is missing field '{hh_fld}'.")

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
                self.O_DISTR:  None
            }


        # ---- Now run child algorithm writing DIRECTLY to parent output ----
        distr_params = {
            distr.L_PDP:        pdp_lyr,
            distr.L_HH:         obj_lyr,
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
            "PDP": pdp_lyr, "HH": obj_lyr, "LEFT": side_l, "RIGHT": side_r, "TAN": final_tan
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
            }
        except Exception as e:
            try:
                feedback.reportError(f"Distribution failed (unexpected): {e}")
            except Exception:
                pass
            return {
                self.O_FEEDER: locals().get("out_feeder_uri", None),
                self.O_DISTR:  None,
            }
        
        # Success: return URIs so Processing auto-loads the layer(s)
        return {
            self.O_FEEDER: out_feeder_uri,
            self.O_DISTR:  out_distr_uri,
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
        O_FEEDER  = DuctLayer.O_FEEDER
        O_DISTR   = DuctLayer.O_DISTR
except Exception:
    # If DuctLayer wasn't defined for some reason, avoid crashing module import
    pass



# Explicit exports for clarity in dir()/from-imports (typed to keep linters happy)
from typing import List as _List  # noqa: F401
__all__: _List[str] = ["AlgFeederDuctsNoSplit", "AlgDistributionDucts", "DuctLayer", "AlgDucts"]
