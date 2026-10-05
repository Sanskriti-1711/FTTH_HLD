"""Build an aerial-zone layer for the Berlin project from OSM landuse.

Aerial zones = land where a trench is not permitted / not economic: public
green space, forest, nature reserves, wetland, farmland. The designer treats a
house drop leg inside one of these as an **aerial** drop (and never excavates).

    python tmp/make_aerial_zones.py <project_outputs_dir> <osm_landuse.shp> <out.geojson>
"""
import os
import sys
from collections import Counter

from osgeo import ogr, osr

# OSM landuse/natural classes where underground construction is restricted.
# NOTE: roadside classes (grass, scrub, garden, recreation_ground) are
# deliberately excluded — in OSM they are mostly verges and tiny strips, and
# including them blankets the whole AOI.
RESTRICTED = {
    "park", "forest", "nature_reserve", "meadow", "wetland", "wood",
    "farmland", "farm", "orchard", "vineyard", "cemetery", "allotments",
}
# Ignore slivers: a real restricted area is at least this big.
MIN_AREA_M2 = 2000.0
EPSG = 25833


def aoi_bbox(project_dir, buffer_m=400.0):
    xs, ys = [], []
    for name in ("PDPs.geojson", "Objects.geojson", "MFG.geojson", "Polygons.geojson"):
        path = os.path.join(project_dir, name)
        if not os.path.isfile(path):
            continue
        ds = ogr.Open(path)
        if ds is None:
            continue
        lyr = ds.GetLayer(0)
        src = lyr.GetSpatialRef()
        dst = osr.SpatialReference()
        dst.ImportFromEPSG(EPSG)
        if src is not None:
            try:
                src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            except Exception:
                pass
        dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        tr = osr.CoordinateTransformation(src, dst) if src is not None else None
        for f in lyr:
            g = f.GetGeometryRef()
            if g is None or g.IsEmpty():
                continue
            g = g.Clone()
            if tr is not None:
                g.Transform(tr)
            minx, maxx, miny, maxy = g.GetEnvelope()
            xs += [minx, maxx]
            ys += [miny, maxy]
        ds = None
    if not xs:
        raise SystemExit("no project layers found in " + project_dir)
    return (min(xs) - buffer_m, max(xs) + buffer_m,
            min(ys) - buffer_m, max(ys) + buffer_m)


def main():
    project_dir, landuse, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    bbox = aoi_bbox(project_dir)
    print("AOI bbox (EPSG:%d) %.0f x %.0f m" % (EPSG, bbox[1] - bbox[0], bbox[3] - bbox[2]))

    ds = ogr.Open(landuse)
    if ds is None:
        raise SystemExit("cannot open " + landuse)
    lyr = ds.GetLayer(0)
    src = lyr.GetSpatialRef()
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(EPSG)
    try:
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)

    drv = ogr.GetDriverByName("GeoJSON")
    if os.path.exists(out_path):
        drv.DeleteDataSource(out_path)
    out = drv.CreateDataSource(out_path)
    ol = out.CreateLayer("Aerial_Zones", dst, ogr.wkbMultiPolygon)
    ol.CreateField(ogr.FieldDefn("ZONE_ID", ogr.OFTString))
    ol.CreateField(ogr.FieldDefn("FCLASS", ogr.OFTString))
    ol.CreateField(ogr.FieldDefn("AREA_HA", ogr.OFTReal))
    defn = ol.GetLayerDefn()

    seen = Counter()
    kept = 0
    ol.StartTransaction()
    for f in lyr:
        cls = (f.GetField("fclass") or "").strip()
        seen[cls] += 1
        if cls not in RESTRICTED:
            continue
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        g = g.Clone()
        g.Transform(tr)
        minx, maxx, miny, maxy = g.GetEnvelope()
        if maxx < bbox[0] or minx > bbox[1] or maxy < bbox[2] or miny > bbox[3]:
            continue
        clip = ogr.Geometry(ogr.wkbPolygon)
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in ((bbox[0], bbox[2]), (bbox[1], bbox[2]),
                     (bbox[1], bbox[3]), (bbox[0], bbox[3]),
                     (bbox[0], bbox[2])):
            ring.AddPoint_2D(x, y)
        clip.AddGeometry(ring)
        g = g.Intersection(clip)
        if g is None or g.IsEmpty():
            continue
        if g.GetArea() < MIN_AREA_M2:
            continue
        if g.GetGeometryName() == "POLYGON":
            multi = ogr.Geometry(ogr.wkbMultiPolygon)
            multi.AddGeometry(g)
            g = multi
        ft = ogr.Feature(defn)
        kept += 1
        ft.SetField("ZONE_ID", "AZ-%05d" % kept)
        ft.SetField("FCLASS", cls)
        ft.SetField("AREA_HA", round(g.GetArea() / 10000.0, 3))
        ft.SetGeometry(g)
        ol.CreateFeature(ft)
    ol.CommitTransaction()
    out = None
    print("kept %d zone(s) from %d landuse feature(s)" % (kept, sum(seen.values())))
    print("classes present:", seen.most_common(12))
    print("wrote", out_path)


if __name__ == "__main__":
    main()
