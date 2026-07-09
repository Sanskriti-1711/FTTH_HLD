# utils/geometry_ops.py
from typing import List, Optional
from qgis.core import QgsGeometry


def unary_union_geoms(geoms: List[QgsGeometry]) -> Optional[QgsGeometry]:
    """
    Safe unary union for a list of geometries.
    Returns None if the input list is empty or all geometries are empty.
    """
    geoms = [g for g in geoms if g and not g.isEmpty()]
    if not geoms:
        return None
    try:
        return QgsGeometry.unaryUnion(geoms)
    except Exception:
        # unaryUnion can fail on complex self-intersecting inputs;
        # fall back to sequential combine()
        merged = geoms[0]
        for g in geoms[1:]:
            try:
                merged = merged.combine(g)
            except Exception:
                pass
        return merged
