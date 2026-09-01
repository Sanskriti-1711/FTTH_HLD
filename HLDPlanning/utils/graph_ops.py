# -*- coding: utf-8 -*-
import math
from typing import Optional, Dict, Any
from qgis.core import QgsVectorLayer, QgsGeometry, QgsPointXY
from .geom_basic import geom_ok as _geom_ok

def add_lines_to_graph(G, layer: Optional[QgsVectorLayer], step_m: float, eps_val: float, qkey_func,
                       weight_scale: float = 1.0, edge_attrs: Optional[Dict[str, Any]] = None):
    """
    Densifies each line in 'layer' by 'step_m' and adds segments to graph 'G'.
    'qkey_func' must return a quantized key tuple for a QgsPointXY (x, y).

    Args:
        weight_scale: Multiplier for the edge weight (e.g., 0.1 for brownfield reuse).
        edge_attrs: Optional dict of extra attributes to store on each edge
                    (e.g., asset_id, infra_type).
    """
    if not layer or layer.featureCount() == 0:
        return
    for feat in layer.getFeatures():
        g = feat.geometry()
        if not _geom_ok(g):
            continue
        g = g.densifyByDistance(step_m)
        lines = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        for ln in lines:
            for i in range(len(ln) - 1):
                p1, p2 = QgsPointXY(ln[i]), QgsPointXY(ln[i + 1])
                a, b = qkey_func(p1, eps_val), qkey_func(p2, eps_val)
                if a == b:
                    continue
                dist = math.hypot(p1.x() - p2.x(), p1.y() - p2.y())
                if dist > 0:
                    weight = dist * weight_scale
                    if edge_attrs:
                        G.add_edge(a, b, weight=weight, **edge_attrs)
                    else:
                        G.add_edge(a, b, weight=weight)


def add_brownfield_edges_to_graph(G, registry, step_m: float, eps_val: float, qkey_func,
                                   base_weight: float = 0.1,
                                   connect_tol: float = 5.0) -> int:
    """
    Add brownfield line assets from a BrownfieldRegistry to the graph.

    Each edge carries:
      - weight (scaled: short = more attractive for reuse)
      - asset_id (brownfield registry id)
      - infra_type (duct / fibre / trench)
      - has_capacity (bool)

    When 'connect_tol' > 0, every brownfield segment endpoint that lies within
    'connect_tol' metres of an *existing* graph node (sidewalk / tangent) is
    bridged to that node with a cheap connector edge.  This lets corridors that
    are offset from the road network (survey GPS error, drawing offsets) still
    participate in routing instead of being silently ignored because their
    quantized keys never coincide with the graph grid.

    Returns:
        Number of brownfield segment edges added (connectors are not counted).
    """
    count = 0
    survey_count = 0  # mandatory survey paths (weight=0)
    brownfield_count = 0  # preferred brownfield (weight=0.1)

    # Coarse spatial index of the graph nodes that exist BEFORE injection, so
    # connector edges only ever link to real graph nodes (sidewalks/tangents)
    # and never to other brownfield-only nodes.
    pre_nodes = None
    node_grid = None
    cell = connect_tol if (connect_tol and connect_tol > 0) else 0.0
    if cell > 0 and G.number_of_nodes():
        pre_nodes = set(G.nodes())
        node_grid = {}
        for n in pre_nodes:
            node_grid.setdefault((int(n[0] // cell), int(n[1] // cell)), []).append(n)

    def _nearest_graph_node(pt):
        """Nearest pre-existing graph node within 'connect_tol', or None."""
        if node_grid is None:
            return None
        cx, cy = int(pt[0] // cell), int(pt[1] // cell)
        best, best_d = None, connect_tol
        # With a cell size == connect_tol, any node within connect_tol of the
        # query point lies in the 3x3 neighbourhood of the query cell.
        for ix in (cx - 1, cx, cx + 1):
            for iy in (cy - 1, cy, cy + 1):
                for n in node_grid.get((ix, iy), ()):
                    d = math.hypot(pt[0] - n[0], pt[1] - n[1])
                    if d <= best_d:
                        best, best_d = n, d
        return best

    _conn_cache = {}

    for edge in registry.iter_graph_edges(step_m=step_m, base_weight=base_weight):
        a = qkey_func(QgsPointXY(edge.u[0], edge.u[1]), eps_val)
        b = qkey_func(QgsPointXY(edge.v[0], edge.v[1]), eps_val)
        if a == b:
            continue
        dist = math.hypot(edge.u[0] - edge.v[0], edge.u[1] - edge.v[1])
        if dist <= 0:
            continue
        G.add_edge(a, b,
                   weight=edge.weight * dist,
                   asset_id=edge.asset_id,
                   infra_type=edge.infra_type,
                   has_capacity=True,
                   verify_status=edge.verify_status)
        count += 1
        if edge.use_mode == "survey":
            survey_count += 1
        else:
            brownfield_count += 1

        # Bridge this segment's endpoints to the nearest pre-existing graph
        # node within connect_tol so slightly-offset corridors get reused.
        if pre_nodes is not None:
            for key, pt in ((a, edge.u), (b, edge.v)):
                if key in pre_nodes:
                    continue  # already touches the graph exactly
                if key not in _conn_cache:
                    _conn_cache[key] = _nearest_graph_node(pt)
                target = _conn_cache[key]
                if target is None:
                    continue
                cdist = math.hypot(pt[0] - target[0], pt[1] - target[1])
                # Survey mandatory paths get zero-weight connectors so the
                # algorithm can enter/exit the forced corridor freely.
                conn_weight = 0.0 if edge.use_mode == "survey" else base_weight * cdist
                G.add_edge(key, target, weight=conn_weight)
    return count

def snap_to_nodes(pt: QgsPointXY, nodes, max_dist: float):
    """Return (nearest_node, distance) within 'max_dist', or (None, inf) if none."""
    nearest, best = None, float("inf")
    for n in nodes:
        d = math.hypot(pt.x() - n[0], pt.y() - n[1])
        if d < best and d <= max_dist:
            best, nearest = d, n
    return nearest, best
