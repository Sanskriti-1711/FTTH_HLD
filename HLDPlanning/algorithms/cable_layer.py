# -*- coding: utf-8 -*-
"""
Cable Builder — Feeder Cable (copy/style) + grouped Distribution Cable branches
• Copies Feeder Trench to Feeder Cable.
• Builds ONE grouped Distribution Cable per same-footway group: joins each object's
  Distribution Trench segment (PDP → footway point) with its Garden Trench
  segment (object → footway point) into a single PDP → object cable, tagged
  with the object's addr_id and household count (hhs).

Parameter surface slimmed 2026-07-03: only the three trench layers remain.
PDP fields are auto-detected (PDP_ID/pdp_id), CRS is the pipeline standard
EPSG:25833, snapping/linemerge/dedupe run with the fixed DEFAULT_* values,
and the styling/add-to-project cosmetics plus the dead OUT_MERGED_INPUTS
output were removed.
"""

import math
from collections import defaultdict

from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterFeatureSink, QgsProcessingException,
    QgsFeatureSink, QgsFields, QgsField, QgsWkbTypes,
    QgsFeature, QgsProcessingUtils, QgsSymbol, QgsGeometry,
    QgsCoordinateReferenceSystem, QgsPointXY, QgsSpatialIndex, QgsVectorLayer,
)
from qgis import processing

# --- utils imports ---
from ..utils.string_utils import normalize_key
from ..utils.fields import first_field_case_insensitive
from ..utils.layer_ops import (
    fix_geometries,
    reproject_if_needed,
    subset_by_id,
    snap_layer,
    linemerge_layer,
    find_first_alg,
)
from ..utils.geom import (
    round_key_xy, geom_substring, path_len, lcp_len, edges_to_geom,
    merge_contiguous_runs,
)
from ..utils.graph import add_edge, dijkstra_with_parents, reconstruct_path
from ..utils.snap import snap_point_create_virtual


def _polyline_of(geom: QgsGeometry):
    """Return the first polyline (list of QgsPointXY) of a line geometry."""
    if geom is None or geom.isEmpty():
        return None
    if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.LineGeometry:
        return None
    if QgsWkbTypes.isMultiType(geom.wkbType()):
        parts = geom.asMultiPolyline()
        return parts[0] if parts else None
    pts = geom.asPolyline()
    return pts or None


def _pts_close(a, b, tol=0.01):
    return abs(a.x() - b.x()) < tol and abs(a.y() - b.y()) < tol


def _concat_polylines(*polys, tol=0.01):
    """Concatenate polylines (lists of QgsPointXY), dropping shared endpoints."""
    out = []
    for pts in polys:
        if not pts:
            continue
        if out and _pts_close(pts[0], out[-1], tol):
            pts = pts[1:]
        if pts:
            out.extend(pts)
    return out


def _geom_tail_xy(geom):
    """Return the last vertex of a (multi)polyline as 'x,y' or ''."""
    if not geom or geom.isEmpty():
        return ""
    try:
        if geom.isMultipart():
            parts = geom.asMultiPolyline()
            pt = parts[-1][-1] if parts else None
        else:
            pl = geom.asPolyline()
            pt = pl[-1] if pl else None
        if pt is not None:
            return f"{pt.x():.2f},{pt.y():.2f}"
    except Exception:
        pass
    return ""


def _join_object_cable(dist_geom: QgsGeometry, garden_geom: QgsGeometry, proj_geom=None):
    """
    Build one continuous PDP → object cable.

    ``dist_geom`` runs pseudo-PDP → footway point (Distribution Trench);
    ``garden_geom`` runs object → footway point (Garden Trench). They share
    the footway endpoint, so the joined polyline is
    ``dist_points + reversed(garden_points)[1:]``.

    When the PDP sits back from the street, ``proj_geom`` is the PDP →
    pseudo-PDP projection line and is prepended (PDP → pseudo-PDP → footway
    → object).
    """
    dpts = _polyline_of(dist_geom)
    gpts = _polyline_of(garden_geom)
    if not dpts or not gpts:
        return None
    # The garden runs object → footway; reverse it to footway → object.
    g_rev = list(reversed(gpts))
    parts = [dpts, g_rev]
    if proj_geom is not None:
        ppts = _polyline_of(proj_geom)
        if ppts:
            parts.insert(0, ppts)  # PDP → pseudo-PDP
    joined = _concat_polylines(*parts)
    if len(joined) < 2:
        return None
    return QgsGeometry.fromPolylineXY(joined)


def _heal_route_graph(adj, edge_geom, edge_len, anchor_nodes, must_reach_nodes,
                      max_bridge_m=10.0):
    """Join route-tree islands that hold a PDP back onto the MFG component.

    The trench network is one corridor set on the ground, but the router's
    graph only links two pieces when they land on the same node key.  Where
    two source pieces meet within a few centimetres — a noding artefact — the
    graph can split into islands, and a PDP sitting on one is silently left
    out of the feeder plan (observed: 6 of 31 PDPs, ~500 premises, with no
    error anywhere).

    This bridges every island that holds an anchor back onto the component
    holding the MFG, at the closest pair of nodes, so the feeder really does
    run from the MFG to every PDP.  Only gaps up to ``max_bridge_m`` are
    healed: a genuinely isolated island (a trench nobody connects to the MFG)
    is left alone and reported instead of being linked by an invented line.

    Returns (n_bridges, bridged_metres, [unreachable node keys]).
    """
    if not adj:
        return 0, 0.0, list(must_reach_nodes)

    def components():
        comp, cid = {}, 0
        for start in adj:
            if start in comp:
                continue
            stack = [start]
            comp[start] = cid
            while stack:
                u = stack.pop()
                for v, _sid, _w in adj.get(u, ()):
                    if v not in comp:
                        comp[v] = cid
                        stack.append(v)
            cid += 1
        return comp

    comp = components()
    counts = defaultdict(int)
    for a in anchor_nodes:
        if a in comp:
            counts[comp[a]] += 1
    if not counts:
        return 0, 0.0, list(must_reach_nodes)
    main = max(counts.items(), key=lambda kv: kv[1])[0]

    n_bridges, bridged_m = 0, 0.0
    while True:
        islands = {}
        for n in must_reach_nodes:
            if n in comp and comp[n] != main:
                islands.setdefault(comp[n], 0)
        if not islands:
            break
        main_nodes = [n for n, c in comp.items() if c == main]
        # Heal the island with the shortest possible gap first, so every
        # bridge is the smallest link that makes the network usable.
        best = None          # (gap, island_cid, island_node, main_node)
        for cid in islands:
            island_nodes = [n for n, c in comp.items() if c == cid]
            for a in island_nodes:
                for b in main_nodes:
                    gap = math.hypot(a[0] - b[0], a[1] - b[1])
                    if best is None or gap < best[0]:
                        best = (gap, cid, a, b)
        if best is None or best[0] > max_bridge_m:
            break
        gap, cid, a, b = best
        link = QgsGeometry.fromPolylineXY([QgsPointXY(b[0], b[1]),
                                           QgsPointXY(a[0], a[1])])
        add_edge(adj, edge_geom, edge_len, b, a, link)
        bridged_m += gap
        n_bridges += 1
        for n, c in comp.items():
            if c == cid:
                comp[n] = main

    unreachable = [n for n in must_reach_nodes
                   if n in comp and comp[n] != main]
    unreachable += [n for n in must_reach_nodes if n not in comp]
    return n_bridges, bridged_m, unreachable


class AlgCableBuilderAll(QgsProcessingAlgorithm):
    # --- Inputs ---
    FEEDER_SRC   = "FEEDER_TRENCH"
    GARDEN_L     = "GARDEN_TRENCHES"
    DISTR_L      = "DISTR_TRENCHES"
    PDP_PROJ     = "PDP_PROJECTIONS"   # optional: PDP→footway/Service lines

    # --- Outputs ---
    O_FEEDER     = "OUT_FEEDER_CABLE"
    O_DIST       = "OUT_DISTRIBUTION_CABLE"

    # --- Fixed defaults (formerly UI parameters) ---
    DEFAULT_CRS_AUTHID = "EPSG:25833"
    DEFAULT_SNAP_M     = 0.5      # snap tolerance (m) within PDP group
    DEFAULT_DO_MERGE   = True     # merge contiguous lines (linemerge)
    DEFAULT_DO_DEDUPE  = True     # remove duplicate geometries
    DEFAULT_DIST_COLOR = "#0000ff"
    DEFAULT_DIST_WIDTH = 0.8
    SAME_FOOTWAY_TOL_M = 0.5
    RESERVED_SPARE_FIBERS = 2
    # Distribution sizing (docs/stages/HLD.md §Duct & cable rules): the shared
    # trunk is sized from the households riding it (never below the 48F
    # distribution floor); the one-to-one garden-leg DROP cable is a 12F
    # cable, because it serves exactly one premise.
    DIST_FIBER_MIN    = 48
    GARDEN_FIBER_COUNT = 12
    # Connection-type values written on the layer and read back by the duct
    # stage to keep the drop legs out of the distribution duct.
    CONN_TRUNK = "Trunk on spine span"
    CONN_DROP  = "Drop (garden leg)"
    CABLE_TYPE_TRUNK = "Distribution"
    CABLE_TYPE_DROP  = "Drop"

    # --- Shared feeder-cable planning (three-trunk policy) ---
    FEEDER_LADDER = (12, 24, 48, 72, 96, 144, 288)
    # Trunk sizing: carry at most 70% of the cable size (≥30% spare).
    FEEDER_SPARE_RATIO = 0.7
    # User spec: THREE capacity-balanced trunk cables leave every MFG and
    # pick up PDPs until the trunk is exhausted.  A trunk is exhausted when
    # it reaches EITHER limit, whichever comes first:
    #   • capacity — 70% of the largest ladder cable (201 of 288F), or
    #   • route length — 1,000 m of laid cable.
    # Remaining PDPs spill onto a 4th+ trunk so none are ever dropped.
    FEEDER_TRUNKS_PER_MFG = 3
    FEEDER_LENGTH_LIMIT_M = 1000.0
    # Hard capacity cap per trunk: 70% of the largest ladder cable.
    FEEDER_TRUNK_MAX_DEMAND = int(FEEDER_SPARE_RATIO * 288)
    # Graph/snap parameters for the feeder route tree (mirror duct_layer feeder)
    SNAP_TOL = 1.5
    NODE_TOL = 0.5
    END_EPS  = 0.25
    INT_EPS  = 0.25

    # --- New optional inputs (shared feeder planning) ---
    FINAL_TRENCH = "FINAL_TRENCHES"
    PDP_POINTS   = "PDP_POINTS"
    MFG_POINTS   = "MFG_POINTS"

    # ------------------------- UI -------------------------
    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.FEEDER_SRC, "Feeder Trench (source layer)", [QgsProcessing.TypeVectorAnyGeometry]
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.GARDEN_L, "Garden Trenches (lines; PDP_ID auto-detected)", [QgsProcessing.TypeVectorLine]
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.DISTR_L, "Distribution Trenches (lines; PDP_ID auto-detected)", [QgsProcessing.TypeVectorLine]
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.PDP_PROJ, "PDP→Footway projections (optional)", [QgsProcessing.TypeVectorLine],
            optional=True,
        ))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.O_FEEDER, "Feeder Cable"
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.O_DIST, "Distribution Cable (per object)", QgsProcessing.TypeVectorLine
        ))

        # Optional shared-feeder inputs.  When all three are provided the
        # feeder cable is PLANNED from the Final_Trenches route tree (PDPs
        # clubbed onto shared cables, sized by splitter demand with 40%
        # spare); otherwise the old Feeder_Trench copy behaviour is kept.
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.FINAL_TRENCH, "Final Trenches (route tree; shared feeder planning)",
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.PDP_POINTS, "PDP Points (SPLIT_CNT demand; shared feeder planning)",
            [QgsProcessing.TypeVectorPoint], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.MFG_POINTS, "MFG Points (shared feeder planning)",
            [QgsProcessing.TypeVectorPoint], optional=True,
        ))

    # ---------------- shared feeder planning ----------------

    def _pdp_demand(self, pdp_lyr, fid, f_pdp):
        """Feeder-fibre demand for a PDP: splitter module count (SPLIT_CNT).
        Fallbacks: SPL_PORTS//64 → ceil(HH/32) → 1 (never 0)."""
        f = pdp_lyr.getFeature(fid) if hasattr(pdp_lyr, "getFeature") else None
        if f is None:
            return 1
        fld_cnt = first_field_case_insensitive(pdp_lyr, ["SPLIT_CNT", "split_cnt"])
        if fld_cnt:
            try:
                v = int(f[fld_cnt])
                if v and v > 0:
                    return v
            except Exception:
                pass
        fld_ports = first_field_case_insensitive(pdp_lyr, ["SPL_PORTS", "spl_ports"])
        if fld_ports:
            try:
                v = int(f[fld_ports])
                if v and v > 0:
                    return max(1, v // 64)
            except Exception:
                pass
        fld_hh = first_field_case_insensitive(pdp_lyr, ["HH", "hh"])
        if fld_hh:
            try:
                hh = int(float(f[fld_hh] or 0))
                if hh and hh > 0:
                    return max(1, int(math.ceil(hh / 32.0)))
            except Exception:
                pass
        return 1

    def _ladder_size(self, demand):
        """Smallest standard cable size whose 70% (30%-spare) capacity holds demand."""
        for s in self.FEEDER_LADDER:
            if demand <= self.FEEDER_SPARE_RATIO * s:
                return s
        return self.FEEDER_LADDER[-1]

    def _group_feeder_cables(self, paths, demands, pid_mfg, edge_geom, edge_len,
                             pdp_label):
        """Split an MFG's PDPs across THREE capacity-balanced trunk cables.

        Input:
          paths     : pid -> (edge_list, length)  — MFG→PDP routes on the
                      Final_Trenches graph (full path from MFG)
          demands   : pid -> splitter-module demand (feeder fibres)
          pid_mfg   : pid -> MFG label

        Strategy (user spec: 'three cables start at the MFG and cover the
        PDPs until they reach their length'):
          - Every MFG starts FEEDER_TRUNKS_PER_MFG (3) trunk cables.  The
            trunks are CAPACITY-BALANCED, not direction-sectored: each PDP
            (longest route first) goes to the feasible trunk carrying the
            least demand, so the three fill together instead of one
            hoarding the load.
          - A trunk takes no more PDPs once it reaches EITHER limit, which
            ever comes first:
              • capacity — 70% of the largest ladder cable (201 of 288F), so
                every trunk keeps ≥30% spare, or
              • route length — 1,000 m of laid cable (shared corridors
                counted once over the union of its member routes).
          - PDPs left over when all three are exhausted open a 4th+ trunk,
            so nothing is dropped.
          - Shared corridors are drawn once; divergences become tap-off
            branch parts of the same cable feature.

        Returns a list of cable dicts with keys:
          cable_id, members (full edge lists MFG→each PDP), demand, size,
          pdp_ids, splice_of, splice_pt, mfg_id, polygon_id, trunk_no,
          length, length_capped.
        """
        ladder = self.FEEDER_LADDER
        max_demand = self.FEEDER_TRUNK_MAX_DEMAND      # 201 = 70% of 288F
        len_limit = self.FEEDER_LENGTH_LIMIT_M

        def size_for(demand):
            """Smallest ladder size whose 70% capacity covers `demand`."""
            for s in ladder:
                if demand <= self.FEEDER_SPARE_RATIO * s:
                    return s
            return ladder[-1]

        def route_len(segs):
            """Laid length of a trunk: each edge counted once over the union."""
            return sum(edge_len.get(s, 0.0) for s in segs)

        # 1) Partition PDPs by MFG.
        by_mfg = defaultdict(list)
        for pid in paths:
            by_mfg[pid_mfg.get(pid, "")].append(pid)

        cables = []
        cid = 1
        for mfg, pids in by_mfg.items():
            # Longest routes first so the trunks fill evenly along their span.
            pids.sort(key=lambda q: paths[q][1], reverse=True)
            trunks = [
                {"members": [], "demand": 0, "pdp_ids": [],
                 "segs": set(), "length": 0.0, "hit_length_limit": False}
                for _ in range(self.FEEDER_TRUNKS_PER_MFG)
            ]
            for q in pids:
                d = max(1, int(demands.get(q, 1) or 1))
                q_segs = set(paths[q][0])
                # Feasible = keeps BOTH limits; emptiest feasible trunk wins.
                best = None
                for t in sorted(trunks, key=lambda tt: tt["demand"]):
                    if t["demand"] + d > max_demand:
                        continue
                    proj_segs = t["segs"] | q_segs
                    proj_len = route_len(proj_segs)
                    if proj_len > len_limit:
                        # This trunk was closed out by the length cap, not by
                        # capacity — record it so the attribute table can tell
                        # the two apart.
                        t["hit_length_limit"] = True
                        continue
                    best = (t, proj_segs, proj_len)
                    break
                if best is None:
                    # All three exhausted — open another trunk for the
                    # remainder rather than dropping the PDP.
                    nt = {"members": [], "demand": 0, "pdp_ids": [],
                          "segs": set(), "length": 0.0, "hit_length_limit": False}
                    trunks.append(nt)
                    best = (nt, q_segs, route_len(q_segs))
                t, proj_segs, proj_len = best
                t["members"].append(list(paths[q][0]))
                t["demand"] += d
                t["pdp_ids"].append(pdp_label.get(q, str(q)))
                t["segs"] = proj_segs
                t["length"] = proj_len

            for idx, t in enumerate(trunks, start=1):
                if not t["members"]:
                    continue
                cables.append({
                    "cable_id": cid,
                    "members": t["members"],
                    "demand": t["demand"],
                    "size": size_for(t["demand"]),
                    "pdp_ids": t["pdp_ids"],
                    "splice_of": "",
                    "splice_pt": "",
                    "mfg_id": mfg,
                    "polygon_id": "",
                    "trunk_no": idx,
                    "length": t["length"],
                    "length_capped": 1 if t.get("hit_length_limit") else 0,
                })
                cid += 1
        # Orphan PDPs without any routable path never appear in `paths`, so
        # nothing is silently dropped — callers already filtered them.
        return cables


    def _plan_shared_feeders(self, final_tr, pdp_pts, mfg_pts, context, feedback):
        """Build clubbed feeder cables from the Final_Trenches route tree.

        Returns (QgsFields, [QgsFeature]) or raises on unrecoverable errors
        (caller falls back to the legacy trench-copy path).
        """
        import heapq as _heapq
        crs_t = QgsCoordinateReferenceSystem(self.DEFAULT_CRS_AUTHID)
        net = reproject_if_needed(fix_geometries(final_tr, context, feedback), crs_t, context, feedback)
        pdps = reproject_if_needed(fix_geometries(pdp_pts, context, feedback), crs_t, context, feedback)
        mfgs = reproject_if_needed(fix_geometries(mfg_pts, context, feedback), crs_t, context, feedback)

        f_pdp = first_field_case_insensitive(pdps, ["PDP_ID", "pdp_id", "pdp"])
        f_mfg = first_field_case_insensitive(mfgs, ["MFG_ID", "mfg_id", "mfg"])
        if not f_pdp:
            raise QgsProcessingException("Shared feeder planning: PDP layer has no PDP_ID field.")
        f_poly = first_field_case_insensitive(pdps, ["POLYGON_ID", "polygon_id"])

        # ---- Graph from Final_Trenches (mirror feeder-duct graph build) ----
        net_single = processing.run(
            "native:multiparttosingleparts",
            {"INPUT": net, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            context=context, feedback=feedback)["OUTPUT"]
        try:
            inter = processing.run(
                "native:lineintersections",
                {"INPUT": net_single, "INTERSECT": net_single,
                 "INPUT_FIELDS": [], "INTERSECT_FIELDS": [],
                 "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                context=context, feedback=feedback)["OUTPUT"]
        except Exception:
            inter = processing.run(
                "qgis:lineintersections",
                {"INPUT": net_single, "INTERSECT": net_single,
                 "INPUT_FIELDS": [], "INTERSECT_FIELDS": [],
                 "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                context=context, feedback=feedback)["OUTPUT"]
        inter = processing.run(
            "native:deleteduplicategeometries",
            {"INPUT": inter, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
            context=context, feedback=feedback)["OUTPUT"]

        seg_index = QgsSpatialIndex(net_single.getFeatures())
        fid_to_geom, fid_to_len = {}, {}
        for f in net_single.getFeatures():
            g = f.geometry()
            if not g or g.isEmpty():
                continue
            fid_to_geom[f.id()] = g
            fid_to_len[f.id()] = g.length()

        fid_breaks = defaultdict(list)
        fid_break_xy = defaultdict(dict)
        for fid, L in fid_to_len.items():
            geom = fid_to_geom[fid]
            p0 = geom.interpolate(0.0).asPoint()
            pL = geom.interpolate(L).asPoint()
            fid_breaks[fid].extend([0.0, L])
            fid_break_xy[fid][0.0] = (p0.x(), p0.y())
            fid_break_xy[fid][L] = (pL.x(), pL.y())

        snap_tol, node_tol, end_eps, int_eps = (self.SNAP_TOL, self.NODE_TOL,
                                                self.END_EPS, self.INT_EPS)

        # ── Endpoint T-nodes ───────────────────────────────────────────────
        # `native:lineintersections` reports CROSSINGS; a span whose END lands
        # exactly on another span's interior (the shape the trench stage's
        # weld/stitch pass creates when a cabinet taps a passing trench) is not
        # reliably returned.  With no break there, the route tree sees two
        # unrelated components and the feeder is judged "not reachable from any
        # MFG" even though the published trench is one welded network — that is
        # exactly what left PDP00017 and PDP00031 without a feeder cable on the
        # Berlin run.  Add a break at every endpoint that touches another span,
        # so the graph matches the geometry it was built from.
        for fid, geom in fid_to_geom.items():
            L0 = fid_to_len.get(fid, 0.0)
            if L0 <= 0 or geom is None:
                continue
            for arc_key in (0.0, L0):
                xy = fid_break_xy[fid].get(arc_key)
                if xy is None:
                    continue
                pg = QgsGeometry.fromPointXY(QgsPointXY(xy[0], xy[1]))
                rect = pg.buffer(int_eps + node_tol, 8).boundingBox()
                for other in seg_index.intersects(rect):
                    if other == fid:
                        continue
                    og = fid_to_geom.get(other)
                    if not og or og.distance(pg) > int_eps:
                        continue
                    d = og.lineLocatePoint(pg)
                    oL = fid_to_len.get(other, 0.0)
                    if d <= 1e-6 or (oL - d) <= 1e-6:
                        continue
                    fid_breaks[other].append(d)
                    fid_break_xy[other][d] = (xy[0], xy[1])

        for fp in inter.getFeatures():
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

        mfg_nodes, mfg_label = {}, {}
        for fm in mfgs.getFeatures():
            nk, _fid = snap_point_create_virtual(
                fm.geometry(), seg_index, fid_to_geom, fid_to_len,
                fid_breaks, fid_break_xy, snap_tol, node_tol, end_eps)
            if nk is not None:
                mfg_nodes[fm.id()] = (nk, _fid)
                mfg_label[fm.id()] = str(fm[f_mfg] or fm.id())

        pdp_nodes, pdp_label, pdp_poly = {}, {}, {}
        for fp in pdps.getFeatures():
            nk, _fid = snap_point_create_virtual(
                fp.geometry(), seg_index, fid_to_geom, fid_to_len,
                fid_breaks, fid_break_xy, snap_tol, node_tol, end_eps)
            if nk is not None:
                pdp_nodes[fp.id()] = (nk, _fid)
                pdp_label[fp.id()] = str(fp[f_pdp] or fp.id())
                pdp_poly[fp.id()] = str(fp[f_poly]) if f_poly else ""

        if not mfg_nodes or not pdp_nodes:
            raise QgsProcessingException(
                "Shared feeder planning: no MFG/PDP could be snapped to the network.")

        adj, edge_geom, edge_len = defaultdict(list), {}, {}
        for fid, breaks in fid_breaks.items():
            geom = fid_to_geom.get(fid)
            L = fid_to_len.get(fid, 0.0)
            if not geom or L <= 0:
                continue
            uniq = sorted(set(b for b in breaks if 0.0 <= b <= L))
            if len(uniq) < 2:
                continue
            coords_at = {}
            for d in uniq:
                c = fid_break_xy[fid].get(d)
                if c is None:
                    pt = geom.interpolate(d).asPoint()
                    c = (pt.x(), pt.y())
                coords_at[d] = c
            for i in range(len(uniq) - 1):
                d0, d1 = uniq[i], uniq[i + 1]
                if (d1 - d0) <= 1e-6:
                    continue
                p0, p1 = coords_at[d0], coords_at[d1]
                u = round_key_xy(p0[0], p0[1], node_tol)
                v = round_key_xy(p1[0], p1[1], node_tol)
                sub = geom_substring(geom, d0, d1)
                add_edge(adj, edge_geom, edge_len, u, v, sub)

        # Heal the route tree BEFORE labelling: an island that holds a PDP is
        # bridged onto the MFG component at the closest pair of nodes, so no
        # PDP can be silently dropped from the feeder plan.
        n_bridges, bridged_m, unreachable = _heal_route_graph(
            adj, edge_geom, edge_len,
            [n for (n, _f) in mfg_nodes.values()],
            [n for (n, _f) in pdp_nodes.values()],
        )
        n_pdp_total = pdps.featureCount()
        if n_bridges:
            feedback.pushInfo(
                f"Feeder route tree healed: {n_bridges} micro-bridge(s) "
                f"({bridged_m:.1f} m total) joined so every PDP is reachable.")
        if unreachable:
            feedback.pushWarning(
                f"Feeder route tree: {len(unreachable)} PDP node(s) still "
                "unreachable from the MFG after healing — these PDPs will not "
                "be served (check the trench connectivity for them).")

        # Label every node with its nearest MFG (multi-source Dijkstra).
        label_dist = {}
        heap = []
        for mfg_id, (node_k, _) in mfg_nodes.items():
            if node_k in adj:
                _heapq.heappush(heap, (0.0, str(node_k), node_k, mfg_id))
        while heap:
            dist_u, _tie, u, lab = _heapq.heappop(heap)
            if u in label_dist and dist_u > label_dist[u][0] + 1e-9:
                continue
            if u not in label_dist:
                label_dist[u] = (dist_u, lab)
            for v, seg_id, w in adj.get(u, []):
                cand = dist_u + w
                if (v not in label_dist) or (cand + 1e-9 < label_dist[v][0]) or \
                   (abs(cand - label_dist[v][0]) <= 1e-9 and str(lab) < str(label_dist[v][1])):
                    _heapq.heappush(heap, (cand, str(seg_id), v, lab))

        pdp_to_mfg = {}
        for pid, (nk, _) in pdp_nodes.items():
            if nk in label_dist:
                pdp_to_mfg[pid] = label_dist[nk][1]

        paths, demands, pid_mfg = {}, {}, {}
        skipped = []          # (label, reason)
        for pid, (nk, _) in pdp_nodes.items():
            label = pdp_label.get(pid, str(pid))
            mfg_id = pdp_to_mfg.get(pid)
            if mfg_id is None or mfg_id not in mfg_nodes:
                skipped.append((label, "not reachable from any MFG"))
                continue
            mnode = mfg_nodes[mfg_id][0]
            if mnode not in adj:
                skipped.append((label, "MFG node is off the route tree"))
                continue
            dist, parent = dijkstra_with_parents(mnode, adj)
            path = reconstruct_path(parent, nk, mnode)
            if not path:
                skipped.append((label, "no MFG→PDP path on the route tree"))
                continue
            paths[pid] = (path, path_len(edge_len, path))
            demands[pid] = self._pdp_demand(pdps, pid, f_pdp)
            pid_mfg[pid] = mfg_label[mfg_id]

        # Make the feeder's PDP coverage explicit: booking 25 of 31 PDPs used
        # to leave no trace in the log at all.
        n_unsnapped = max(0, n_pdp_total - len(pdp_nodes))
        feedback.pushInfo(
            f"Feeder routing: {len(paths)}/{n_pdp_total} PDP(s) routed from the MFG"
            + (f"; {n_unsnapped} not on the trench network (snap tolerance "
               f"{self.SNAP_TOL} m)" if n_unsnapped else ""))
        for label, reason in skipped[:20]:
            feedback.pushWarning(f"  Feeder: PDP {label} not routed — {reason}")
        if len(skipped) > 20:
            feedback.pushWarning(
                f"  Feeder: {len(skipped) - 20} further PDP(s) not routed.")

        if not paths:
            raise QgsProcessingException(
                "Shared feeder planning: no MFG→PDP paths could be routed.")

        cables = self._group_feeder_cables(
            paths, demands, pid_mfg, edge_geom, edge_len, pdp_label)

        fields = QgsFields()
        for nm, t in (
            ("cable_id", QMetaType.Type.Int),
            ("CABLE_TYPE", QMetaType.Type.QString),
            ("FIBER_COUNT", QMetaType.Type.Int),
            ("SPLIT_MODULES", QMetaType.Type.Int),
            ("UTIL_PCT", QMetaType.Type.Double),
            ("SPARE_PCT", QMetaType.Type.Double),
            ("PDP_IDS", QMetaType.Type.QString),
            ("PDP_COUNT", QMetaType.Type.Int),
            ("SPLICE_OF", QMetaType.Type.QString),
            ("SPLICE_POINT", QMetaType.Type.QString),
            ("length_m", QMetaType.Type.Double),
            ("POLYGON_ID", QMetaType.Type.QString),
            ("MFG_ID", QMetaType.Type.QString),
            ("REVIEW", QMetaType.Type.Int),
            ("TRUNK_NO", QMetaType.Type.Int),
            ("LENGTH_LIMIT_M", QMetaType.Type.Double),
            ("LENGTH_CAPPED", QMetaType.Type.Int),
        ):
            fields.append(QgsField(nm, t))

        feats = []
        for c in cables:
            # Drawn geometry: union of every member route — shared trunk
            # segments are drawn once, branch routes become separate parts
            # (tap-offs off the trunk), exactly like distribution cables.
            seen_segs, segs = set(), []
            for p in c["members"]:
                for s in p:
                    if s not in seen_segs:
                        seen_segs.add(s)
                        segs.append(s)
            # Merge the per-edge fragments into continuous runs: the cable
            # must trace one continuous trunk with tap-off branches, not
            # thousands of 1-2 m stubs (which also made the whole tree select
            # as one giant highlight).
            geom = merge_contiguous_runs(edges_to_geom(edge_geom, segs))
            if not geom or geom.isEmpty():
                continue
            nf = QgsFeature(fields)
            nf.setGeometry(geom)
            nf["cable_id"] = c["cable_id"]
            nf["CABLE_TYPE"] = "Feeder"
            nf["FIBER_COUNT"] = c["size"]
            nf["SPLIT_MODULES"] = c["demand"]
            nf["UTIL_PCT"] = round((c["demand"] / c["size"]) * 100.0, 1) if c["size"] else 0.0
            nf["SPARE_PCT"] = round(max(0.0, 100.0 - nf["UTIL_PCT"]), 1)
            nf["PDP_IDS"] = ",".join(c["pdp_ids"])
            nf["PDP_COUNT"] = len(c["pdp_ids"])
            nf["SPLICE_OF"] = c["splice_of"]
            nf["SPLICE_POINT"] = c["splice_pt"]
            nf["length_m"] = round(sum(edge_len.get(s, 0.0) for s in segs), 2)
            nf["POLYGON_ID"] = c.get("polygon_id") or ""
            nf["MFG_ID"] = c.get("mfg_id") or ""
            nf["REVIEW"] = 1 if c["demand"] > self.FEEDER_SPARE_RATIO * self.FEEDER_LADDER[-1] else 0
            # Which of the three (or overflow) trunks this cable is, and
            # whether it was closed out by the length cap or by capacity.
            nf["TRUNK_NO"] = int(c.get("trunk_no") or 0)
            nf["LENGTH_LIMIT_M"] = float(self.FEEDER_LENGTH_LIMIT_M)
            nf["LENGTH_CAPPED"] = int(c.get("length_capped") or 0)
            feats.append(nf)
        return fields, feats

    # ------------------------ run -------------------------
    def _resolve_layer(self, v, context):
        """Resolve a raw parameter value to a usable QgsVectorLayer.

        Mirrors duct_layer's robust resolver: accepts live layers, processing
        feature sources/definitions, temp layer ids/names, and OGR paths.
        Returns None when nothing resolvable is found.
        """
        # 1) Already a live QgsVectorLayer?
        try:
            if isinstance(v, QgsVectorLayer) and v.isValid():
                return v
        except Exception:
            pass
        # 2) Processing feature source / definition -> materialise to memory
        try:
            from qgis.core import QgsProcessingFeatureSource, QgsProcessingFeatureSourceDefinition
            if (isinstance(v, QgsProcessingFeatureSource) or
                    isinstance(v, QgsProcessingFeatureSourceDefinition)):
                return processing.run(
                    "native:savefeatures",
                    {"INPUT": v, "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT},
                    context=context,
                )["OUTPUT"]
        except Exception:
            pass
        # 3) Resolve by layer id/name via Processing utils
        try:
            from qgis.core import QgsProcessingUtils
            cand = QgsProcessingUtils.mapLayerFromString(str(v), context)
            if cand and cand.isValid():
                return cand
        except Exception:
            pass
        # 4) Try as OGR path/URI
        try:
            lyr = QgsVectorLayer(str(v), "resolved", "ogr")
            if lyr.isValid():
                return lyr
        except Exception:
            pass
        return None

    def processAlgorithm(self, p, context, feedback):
        feeder_src = self.parameterAsVectorLayer(p, self.FEEDER_SRC, context)
        final_tr = self._resolve_layer(p.get(self.FINAL_TRENCH), context)
        pdp_pts = self._resolve_layer(p.get(self.PDP_POINTS), context)
        mfg_pts = self._resolve_layer(p.get(self.MFG_POINTS), context)

        # Shared feeder planning: when Final_Trenches + PDP + MFG are all
        # provided, plan clubbed feeder cables (splitter demand, 40% spare,
        # tap-off branches) instead of copying the per-PDP Feeder_Trench.
        shared_plan = None   # (fields, [QgsFeature]) or None
        if final_tr is not None and pdp_pts is not None and mfg_pts is not None:
            try:
                shared_plan = self._plan_shared_feeders(
                    final_tr, pdp_pts, mfg_pts, context, feedback)
                feedback.pushInfo(
                    f"Feeder: shared planning active — {len(shared_plan[1])} planned feeder cables.")
            except Exception as e:
                feedback.reportError(f"⚠️ Shared feeder planning failed ({e}); falling back to trench copy.")
                shared_plan = None
            # An empty plan means nothing routed — fall back instead of
            # silently emitting an empty Feeder_Cable layer.
            if shared_plan is not None and not shared_plan[1]:
                feedback.pushWarning("Feeder: shared planning produced 0 cables — falling back to trench copy.")
                shared_plan = None

        if shared_plan is not None:
            f_fields, shared_feats = shared_plan
            f_wkb = QgsWkbTypes.MultiLineString
            f_crs = QgsCoordinateReferenceSystem(self.DEFAULT_CRS_AUTHID)
            sinkF, outFeederId = self.parameterAsSink(p, self.O_FEEDER, context, f_fields, f_wkb, f_crs)
            copied = 0
            for nf in shared_feats:
                sinkF.addFeature(nf, QgsFeatureSink.FastInsert)
                copied += 1
            feedback.pushInfo(f"Feeder: wrote {copied} planned cable features.")
        else:
            # Copy feeder trench → feeder cable (legacy behaviour)
            if feeder_src is None:
                raise QgsProcessingException(
                    "Feeder Trench layer is required (or provide Final_Trenches + PDP + MFG for shared planning).")
            f_fields, f_wkb, f_crs = feeder_src.fields(), feeder_src.wkbType(), feeder_src.crs()
            sinkF, outFeederId = self.parameterAsSink(p, self.O_FEEDER, context, f_fields, f_wkb, f_crs)
            copied = 0
            for f in feeder_src.getFeatures():
                nf = QgsFeature(f_fields)
                nf.setGeometry(f.geometry())
                nf.setAttributes(f.attributes())
                sinkF.addFeature(nf, QgsFeatureSink.FastInsert)
                copied += 1
            feedback.pushInfo(f"Feeder: copied {copied} features.")

        # --- Distribution build: one TRUNK cable per spine span + one DROP cable per premise ---
        # The distribution trenches are the SHARED SPINE the designer published
        # (one row per span, ``addr_id`` naming every premise it serves). The
        # trunk cable is laid IN the trench, so each span carries exactly ONE
        # trunk cable covering every premise on it — plus, separately, one
        # drop cable per premise along its garden leg (footway → house). The
        # historic build (``dist + reversed(garden)`` per house) re-laid the
        # same corridor once per premise: 25 km drawn for ~1.4 km of unique
        # geometry on Berlin.
        #
        # A trunk span with no ``addr_id`` still publishes (it carries onward
        # connectivity, e.g. towards the next PDP) with FIBER_COUNT for 0 HH.
        # A premise whose garden leg names a span through the comma-joined key
        # is matched by prefix: the span's ``addr_id`` holds every premise, so
        # the drop cable's ``PDP_ID``/``MFG_ID`` come from the garden row.
        garden = self.parameterAsVectorLayer(p, self.GARDEN_L, context)
        distr = self.parameterAsVectorLayer(p, self.DISTR_L, context)

        # Auto-detect the object linkage fields (pickers removed from the UI).
        # The Garden Trench runs object → footway point; the Distribution Trench
        # runs PDP → the SAME footway point. Both carry the object's addr_id.
        fld_g_addr = first_field_case_insensitive(garden, ["addr_id", "ADDR_ID", "hh_id", "object_id", "OBJ_ID"])
        fld_d_addr = first_field_case_insensitive(distr, ["addr_id", "ADDR_ID", "obj_id", "object_id"])
        fld_g_hhs = first_field_case_insensitive(garden, ["hhs", "hh", "HH", "households", "HOUSEHOLDS"])
        fld_g_pdp = first_field_case_insensitive(garden, ["PDP_ID", "pdp_id", "pdp_pol_id", "pDp_POL_ID"])
        fld_g_poly = first_field_case_insensitive(garden, ["POLYGON_ID", "polygon_id"])
        fld_g_mfg = first_field_case_insensitive(garden, ["MFG_ID", "mfg_id"])
        fld_d_pdp = first_field_case_insensitive(distr, ["PDP_ID", "pdp_id", "pdp_pol_id", "pDp_POL_ID"])
        if not fld_g_addr or not fld_d_addr:
            raise QgsProcessingException(
                "Garden/Distribution trenches need an addr_id (or obj_id) field to build per-object cables — run stage 04 first."
            )
        feedback.pushInfo(
            f"Auto-detected fields → Garden addr: '{fld_g_addr}', Distribution addr: '{fld_d_addr}', HH: '{fld_g_hhs or '-'}'"
        )

        crs_t = QgsCoordinateReferenceSystem(self.DEFAULT_CRS_AUTHID)

        garden_t = reproject_if_needed(fix_geometries(garden, context, feedback), crs_t, context, feedback)
        distr_t = reproject_if_needed(fix_geometries(distr, context, feedback), crs_t, context, feedback)

        # NOTE: GeoPackage field names are case-insensitive, so we cannot have
        # both 'pdp_id' and 'PDP_ID' — keep only the uppercase PDP_ID used by
        # the rest of the pipeline.
        out_fields = QgsFields()
        out_fields.append(QgsField("addr_id",    QMetaType.Type.QString))
        out_fields.append(QgsField("ADDR_IDS",   QMetaType.Type.QString))
        out_fields.append(QgsField("hhs",        QMetaType.Type.QString))
        out_fields.append(QgsField("HH_COUNT",   QMetaType.Type.Int))
        out_fields.append(QgsField("FIBER_COUNT", QMetaType.Type.Int))
        out_fields.append(QgsField("RESERVED_SPARE_FIBERS", QMetaType.Type.Int))
        out_fields.append(QgsField("AVAILABLE_FIBERS", QMetaType.Type.Int))
        out_fields.append(QgsField("CONNECTION_TYPE", QMetaType.Type.QString))
        out_fields.append(QgsField("CABLE_TYPE", QMetaType.Type.QString))
        out_fields.append(QgsField("length_m",   QMetaType.Type.Double))
        out_fields.append(QgsField("POLYGON_ID", QMetaType.Type.QString))
        out_fields.append(QgsField("PDP_ID",     QMetaType.Type.QString))
        out_fields.append(QgsField("MFG_ID",     QMetaType.Type.QString))
        sinkD, outDistId = self.parameterAsSink(p, self.O_DIST, context, out_fields, QgsWkbTypes.MultiLineString, crs_t)

        # Index distribution segments by normalized addr_id
        distr_by_addr = {}
        for f in distr_t.getFeatures():
            key = normalize_key(f[fld_d_addr])
            if key:
                distr_by_addr.setdefault(key, []).append(f)

        # Optional PDP→footway projection lines, indexed by normalized PDP id.
        proj_layer = self.parameterAsVectorLayer(p, self.PDP_PROJ, context)
        proj_by_pdp = {}
        if proj_layer is not None:
            fld_proj_pdp = first_field_case_insensitive(
                proj_layer, ["pdp_id", "PDP_ID", "pdp_pol_id", "pDp_POL_ID"])
            proj_t = reproject_if_needed(
                fix_geometries(proj_layer, context, feedback), crs_t, context, feedback)
            for f in proj_t.getFeatures():
                pid = normalize_key(f[fld_proj_pdp]) if fld_proj_pdp else None
                if pid:
                    proj_by_pdp.setdefault(pid, []).append(f.geometry())

        made = 0
        made_trunks = 0
        made_drops = 0
        # ── trunk cables: ONE per distribution (spine) span ───────────────
        # The adapter publishes the fanned rows (one row per address, same
        # span geometry) for the addr_id lookup, so trunks must be grouped by
        # SPAN IDENTITY, not iterated per row: identical geometry appearing
        # once per served address is ONE trunk cable, not N. Group key = the
        # span's identity when present (TRENCH_ID or RUN_ID + START/END
        # chamber), falling back to the WKT of the geometry itself.
        span_groups = {}
        for df in distr_t.getFeatures():
            dg = df.geometry()
            if not dg or dg.isEmpty():
                continue
            names = df.fields().names()
            tid = str(df["TRENCH_ID"]) if "TRENCH_ID" in names and df["TRENCH_ID"] not in (None, "") else None
            rid = str(df["RUN_ID"]) if "RUN_ID" in names and df["RUN_ID"] not in (None, "") else None
            sc = str(df["START_CHAMBER"]) if "START_CHAMBER" in names else None
            ec = str(df["END_CHAMBER"]) if "END_CHAMBER" in names else None
            if tid:
                key = "T:" + tid
            elif rid and (sc or ec):
                key = f"R:{rid}|{sc}|{ec}"
            else:
                key = "W:" + dg.asWkt(2)
            span_groups.setdefault(key, []).append(df)

        for members in span_groups.values():
            df = members[0]
            dg = df.geometry()
            trunk_addrs = []
            for m in members:
                trunk_addrs.extend(
                    a.strip() for a in str(m[fld_d_addr] or "").split(",") if a.strip())
            trunk_addr_keys = {normalize_key(a) for a in trunk_addrs}
            of = QgsFeature(out_fields)
            of.setGeometry(merge_contiguous_runs(dg))
            # Households riding this span: the garden legs attached to it.
            hh_values = []
            for gf in garden_t.getFeatures():
                gkey = normalize_key(gf[fld_g_addr]) if fld_g_addr else None
                if gkey not in trunk_addr_keys:
                    continue
                gg = gf.geometry()
                if not gg or gg.isEmpty():
                    continue
                try:
                    hh_values.append(float(gf[fld_g_hhs]) if fld_g_hhs and gf[fld_g_hhs] not in (None, "") else 1.0)
                except Exception:
                    hh_values.append(1.0)
            hh_count = int(sum(hh_values))
            of["addr_id"]    = trunk_addrs[0] if trunk_addrs else None
            of["ADDR_IDS"]   = ",".join(trunk_addrs)
            of["hhs"]        = str(hh_count)
            of["HH_COUNT"]   = hh_count
            of["FIBER_COUNT"] = max(self.DIST_FIBER_MIN, hh_count + self.RESERVED_SPARE_FIBERS)
            of["RESERVED_SPARE_FIBERS"] = self.RESERVED_SPARE_FIBERS
            of["AVAILABLE_FIBERS"] = max(0, of["FIBER_COUNT"] - self.RESERVED_SPARE_FIBERS - hh_count)
            of["CONNECTION_TYPE"] = self.CONN_TRUNK
            of["CABLE_TYPE"] = self.CABLE_TYPE_TRUNK
            of["length_m"]   = round(of.geometry().length(), 2)
            of["POLYGON_ID"] = (str(df["POLYGON_ID"])
                                 if "POLYGON_ID" in df.fields().names() else None)
            of["PDP_ID"]     = (normalize_key(df[fld_d_pdp]) or None) if fld_d_pdp else None
            of["MFG_ID"]     = str(df["MFG_ID"]) if "MFG_ID" in df.fields().names() else None
            sinkD.addFeature(of, QgsFeatureSink.FastInsert)
            made += 1
            made_trunks += 1

        # ── drop cables: ONE per premise along its garden leg ────────────
        # The garden leg already runs footway → house; the drop cable is that
        # geometry with the premise's attributes. It joins the trunk at the
        # footway end (which the trunk span covers) — this is what the survey
        # app and the LLD compare, per premise.
        for gf in garden_t.getFeatures():
            gg = gf.geometry()
            if not gg or gg.isEmpty():
                continue
            gpts = _polyline_of(gg)
            if len(gpts) < 2:
                continue
            addr = str(gf[fld_g_addr]) if fld_g_addr and gf[fld_g_addr] not in (None, "") else None
            try:
                hh_count = int(float(gf[fld_g_hhs])) if fld_g_hhs and gf[fld_g_hhs] not in (None, "") else 1
            except Exception:
                hh_count = 1
            pid = normalize_key(gf[fld_g_pdp]) if fld_g_pdp else None
            # The PDP→footway projection (when available) is prepended so the
            # drop cable starts at the splitter's own position instead of the
            # footway end. _join_object_cable(dist, garden, proj) joins
            # ``dist + reversed(garden)``; passing the garden leg as BOTH arms
            # with the garden reversed by the helper itself would duplicate it,
            # so the drop geometry is the garden leg (plus the projection).
            proj_geom = proj_by_pdp.get(pid, [None])[0] if pid else None
            if proj_geom is not None:
                ppts = _polyline_of(proj_geom)
                gpts_xy = [QgsPointXY(x, y) for x, y in gpts]
                geom = (QgsGeometry.fromMultiPolylineXY([ppts, gpts_xy])
                        if ppts else gg)
            else:
                geom = gg
            of = QgsFeature(out_fields)
            of.setGeometry(merge_contiguous_runs(geom))
            of["addr_id"]    = addr
            of["ADDR_IDS"]   = addr or ""
            of["hhs"]        = str(hh_count)
            of["HH_COUNT"]   = hh_count
            # A drop cable serves exactly one premise: the Garden sizing rule
            # (12F) applies instead of the 48F distribution floor.
            of["FIBER_COUNT"] = max(self.GARDEN_FIBER_COUNT, hh_count + self.RESERVED_SPARE_FIBERS)
            of["RESERVED_SPARE_FIBERS"] = self.RESERVED_SPARE_FIBERS
            of["AVAILABLE_FIBERS"] = max(0, of["FIBER_COUNT"] - self.RESERVED_SPARE_FIBERS - hh_count)
            of["CONNECTION_TYPE"] = self.CONN_DROP
            of["CABLE_TYPE"] = self.CABLE_TYPE_DROP
            of["length_m"]   = round(of.geometry().length(), 2)
            of["POLYGON_ID"] = str(gf[fld_g_poly]) if fld_g_poly else None
            of["PDP_ID"]     = pid or None
            of["MFG_ID"]     = str(gf[fld_g_mfg]) if fld_g_mfg else None
            sinkD.addFeature(of, QgsFeatureSink.FastInsert)
            made += 1
            made_drops += 1

        # Style output
        out_layer = QgsProcessingUtils.mapLayerFromString(outDistId, context)
        if out_layer:
            sym = QgsSymbol.defaultSymbol(out_layer.geometryType())
            sym.setColor(QColor(self.DEFAULT_DIST_COLOR))
            try: sym.symbolLayer(0).setWidth(self.DEFAULT_DIST_WIDTH)
            except Exception: pass
            out_layer.renderer().setSymbol(sym)

        feedback.pushInfo(
            f"Distribution: {made_trunks} spine trunk cable(s) (48F floor, "
            f"households + {self.RESERVED_SPARE_FIBERS} spare) + "
            f"{made_drops} drop cable(s) ({self.GARDEN_FIBER_COUNT}F garden "
            f"leg) = {made} total")
        return {
            self.O_FEEDER: outFeederId,
            self.O_DIST: outDistId,
        }

    # --- metadata ---
    def name(self): return "06_cable_layer"
    def displayName(self): return "Generate Cables"
    def group(self): return "06 Cable Layer"
    def groupId(self): return "06_cable_layer"
    def createInstance(self): return AlgCableBuilderAll()
