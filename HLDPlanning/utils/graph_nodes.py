# utils/graph_nodes.py
from typing import Dict, Tuple, Optional
from qgis.core import (
    QgsVectorLayer, QgsField, QgsFields, QgsFeature, QgsGeometry, QgsPointXY,
    QgsSpatialIndex, QgsWkbTypes
)
from qgis.PyQt.QtCore import QVariant

def build_node_index_from_graph(G, crs_authid: str, snap_dist: float):
    """
    Creates a memory point layer and a spatial index for graph nodes.
    Returns (node_layer, idx, nid2node, snap_dist).
    """
    node_layer = QgsVectorLayer(f"Point?crs={crs_authid}", "_nodes", "memory")
    dp = node_layer.dataProvider()
    dp.addAttributes([QgsField("nid", QVariant.Int)])
    node_layer.updateFields()

    nid2node: Dict[int, Tuple[float,float]] = {}
    feats = []
    for i, (x, y) in enumerate(G.nodes):
        f = QgsFeature(node_layer.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        f["nid"] = i
        feats.append(f)
        nid2node[i] = (x, y)
    if feats:
        dp.addFeatures(feats)
        node_layer.updateExtents()
    idx = QgsSpatialIndex(node_layer.getFeatures())
    return node_layer, idx, nid2node, snap_dist

def nearest_node(pt: QgsPointXY, node_layer: QgsVectorLayer, idx: QgsSpatialIndex,
                 nid2node: Dict[int, Tuple[float,float]], snap_dist: float) -> Optional[Tuple[float,float]]:
    """
    Returns (x,y) of nearest node (approx NN with bbox prefilter), or None.
    """
    if not nid2node:
        return None
    rect = QgsGeometry.fromPointXY(pt).buffer(snap_dist, 8).boundingBox()
    cands = [nid2node[node_layer.getFeature(fid)["nid"]] for fid in idx.intersects(rect)]
    if not cands:
        cands = list(nid2node.values())[:1000] if len(nid2node) > 1000 else list(nid2node.values())
    best, bestd2 = None, float("inf")
    for (x, y) in cands:
        dx, dy = pt.x() - x, pt.y() - y
        d2 = dx*dx + dy*dy
        if d2 < bestd2:
            best, bestd2 = (x, y), d2
    return best
