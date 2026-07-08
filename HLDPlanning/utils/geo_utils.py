# utils/geo_utils.py
try:
    from shapely.geometry import Point
except Exception:
    # Object layer guards for GeoPandas/Shapely already exist, this is just a stub
    Point = None  # type: ignore

def valid_xy(x, y) -> bool:
    try:
        lx, ly = float(x), float(y)
        return -180.0 <= lx <= 180.0 and -90.0 <= ly <= 90.0
    except Exception:
        return False

def geom_or_placeholder(lon, lat, include_all: bool):
    if lon is not None and lat is not None and Point:
        return Point(float(lon), float(lat))
    return Point(0.0, 85.0) if include_all and Point else None
