# utils/geometry_ops.py
from typing import List, Optional
from qgis.core import QgsGeometry

def unary_union_geoms(geoms: List[QgsGeometry]) -> Optional[QgsGeometry]:
    """
    Safe unary union for a list of geometries, with combine() fallback.
    """
    geoms = [g for g in geoms if g and not g.isEmpty()]
    if not geoms:
        return None
    try:
        return QgsGeometry.unaryUnion(geoms)
    except Exception:
        merged = geoms[0]
        for g in geoms[1:]:
            try:
                merged = merged.combine(g)
            except Exception:
                # best-effort merge; keep going
                pass
        return merged
