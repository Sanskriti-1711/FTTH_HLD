"""Every component's attribute table states the MFG that owns it.

The MFG is a service area, so the components built for it have to say so —
otherwise the BOM, the permit pack and the map can only group by region, not by
the catchment the region was allocated to. ``PDPs.MFG_ID`` carries the
allocation; this pass carries it onto everything else, by evidence and never by
overwrite. These tests pin both halves: what gets filled, and what is left
alone.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("osgeo")

from osgeo import ogr, osr  # noqa: E402

from HLDPlanning.utils import attr_enrich  # noqa: E402


SRS = osr.SpatialReference()
SRS.ImportFromEPSG(25833)


class _Feedback:
    def __init__(self):
        self.info = []

    def pushInfo(self, msg):
        self.info.append(str(msg))

    def pushWarning(self, msg):
        self.info.append("WARNING: %s" % msg)

    def isCanceled(self):
        return False


def _write_layer(run_dir, fname, geom_type, fields, rows):
    """One GPKG layer: ``fields`` are (name, type) and ``rows`` (geom, values)."""
    path = os.path.join(run_dir, fname)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    lyr = ds.CreateLayer(fname[:-5], SRS, geom_type)
    for name, ftype in fields:
        lyr.CreateField(ogr.FieldDefn(name, ftype))
    for geom, values in rows:
        ft = ogr.Feature(lyr.GetLayerDefn())
        if geom is not None:
            ft.SetGeometry(geom)
        for name, value in values.items():
            ft.SetField(name, value)
        lyr.CreateFeature(ft)
    ds = None


def _point(x, y):
    g = ogr.Geometry(ogr.wkbPoint)
    g.AddPoint_2D(x, y)
    return g


def _square(x0, y0, size):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((x0, y0), (x0 + size, y0), (x0 + size, y0 + size),
                 (x0, y0 + size), (x0, y0)):
        ring.AddPoint_2D(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    return poly


def _line(x0, y0, x1, y1):
    line = ogr.Geometry(ogr.wkbLineString)
    line.AddPoint_2D(x0, y0)
    line.AddPoint_2D(x1, y1)
    return line


def _field_values(path, field):
    ds = ogr.Open(path, 0)
    lyr = ds.GetLayer(0)
    i = lyr.GetLayerDefn().GetFieldIndex(field)
    assert i >= 0, f"{os.path.basename(path)} has no {field} field"
    out = [str(ft.GetField(i) or "") for ft in lyr]
    ds = None
    return out


def _run_fixture(run_dir, **overrides):
    """Two MFGs, each owning one region, plus components that name neither.

    Region A (POLY00001) is MFG00001 and region B (POLY00022) is MFG00002 — the
    same shape as North Edgbaston, where one large catchment surrounds one small
    one, so a spatial guess between them would be wrong and the id evidence has
    to be the thing that decides.
    """
    poly_fields = [("POLYGON_ID", ogr.OFTString), ("MFG", ogr.OFTString)]
    _write_layer(run_dir, "Polygons.gpkg", ogr.wkbPolygon, poly_fields, [
        (_square(0, 0, 200), {"POLYGON_ID": "POLY00001"}),
        (_square(500, 0, 40), {"POLYGON_ID": "POLY00022"}),
    ])
    pdp_fields = [("POLYGON_ID", ogr.OFTString), ("PDP_ID", ogr.OFTString),
                  ("MFG_ID", ogr.OFTString)]
    _write_layer(run_dir, "PDPs.gpkg", ogr.wkbPoint, pdp_fields, [
        (_point(100, 100), {"POLYGON_ID": "POLY00001", "PDP_ID": "PDP00001",
                            "MFG_ID": "MFG00001"}),
        (_point(520, 20), {"POLYGON_ID": "POLY00022", "PDP_ID": "PDP00002",
                           "MFG_ID": "MFG00002"}),
    ])
    obj_fields = [("ADDR_ID", ogr.OFTString), ("MFG_ID", ogr.OFTString)]
    _write_layer(run_dir, "Objects.gpkg", ogr.wkbPoint, obj_fields, [
        (_point(10, 10), {"ADDR_ID": "ADDR0001", "MFG_ID": "MFG00001"}),
        (_point(510, 10), {"ADDR_ID": "ADDR0002", "MFG_ID": "MFG00002"}),
    ])
    # A trench per region: the bridge for components that name no region.
    trench_fields = [("TRENCH_ID", ogr.OFTString), ("MFG_ID", ogr.OFTString)]
    _write_layer(run_dir, "Final_Trenches.gpkg", ogr.wkbLineString, trench_fields, [
        (_line(0, 5, 200, 5), {"TRENCH_ID": "TR-1", "MFG_ID": "MFG00001"}),
        (_line(500, 5, 540, 5), {"TRENCH_ID": "TR-2", "MFG_ID": "MFG00002"}),
    ])
    # A chamber that names its region, one that only names its PDP, and one that
    # names neither but is built on region B's trench.
    chamber_fields = [("STRUCT_ID", ogr.OFTString), ("POLYGON_ID", ogr.OFTString),
                      ("PDP_ID", ogr.OFTString)]
    _write_layer(run_dir, "Chambers.gpkg", ogr.wkbPoint, chamber_fields, [
        (_point(100, 100), {"STRUCT_ID": "CH-1", "POLYGON_ID": "POLY00001"}),
        (_point(520, 20), {"STRUCT_ID": "CH-2", "PDP_ID": "PDP00002"}),
        (_point(530, 5), {"STRUCT_ID": "CH-3"}),
    ])
    # Distribution ducts never carried the tag at all on North Edgbaston.
    duct_fields = [("duct_idx", ogr.OFTInteger), ("POLYGON_ID", ogr.OFTString)]
    _write_layer(run_dir, "Distribution_Ducts.gpkg", ogr.wkbLineString, duct_fields, [
        (_line(0, 5, 200, 5), {"duct_idx": 1, "POLYGON_ID": "POLY00001,POLY00022"}),
        (_line(500, 5, 540, 5), {"duct_idx": 2}),
    ])
    if overrides.get("poles"):
        _write_layer(run_dir, "Poles.gpkg", ogr.wkbPoint,
                     [("POLE_ID", ogr.OFTString), ("EQUIPMENT", ogr.OFTString)], [
                         (_point(530, 5), {"POLE_ID": "PL-1",
                                           "EQUIPMENT": "PDP00002"}),
                         (_point(1000, 1000), {"POLE_ID": "PL-2"}),
                     ])
    if overrides.get("tagged"):
        # Already states a tag the pass must not argue with, even though its
        # region says otherwise.
        _write_layer(run_dir, "Coupleurs.gpkg", ogr.wkbPoint,
                     [("coupler_id", ogr.OFTString), ("POLYGON_ID", ogr.OFTString),
                      ("MFG_ID", ogr.OFTString)], [
                         (_point(100, 100), {"coupler_id": "C-1",
                                             "POLYGON_ID": "POLY00001",
                                             "MFG_ID": "MFG00002"}),
                     ])
    n = attr_enrich.stamp_mfg_identity(run_dir, _Feedback())
    return n


def test_regions_and_pdps_ship_their_own_mfg(tmp_path):
    """Polygons and PDPs each state the MFG they were allocated to."""
    run_dir = str(tmp_path)
    _run_fixture(run_dir)

    # 'MFG' is the polygon stage's legacy short column and was blank on all 45
    # North Edgbaston regions; it is filled from the same allocation.
    assert _field_values(os.path.join(run_dir, "Polygons.gpkg"), "MFG_ID") == [
        "MFG00001", "MFG00002"]
    assert _field_values(os.path.join(run_dir, "Polygons.gpkg"), "MFG") == [
        "MFG00001", "MFG00002"]
    assert _field_values(os.path.join(run_dir, "PDPs.gpkg"), "MFG_ID") == [
        "MFG00001", "MFG00002"]


def test_a_component_is_attributed_by_evidence_not_by_geometry(tmp_path):
    """Region, then PDP, then the trench it is built on — and blank if none."""
    run_dir = str(tmp_path)
    _run_fixture(run_dir, poles=True)

    assert _field_values(os.path.join(run_dir, "Chambers.gpkg"), "MFG_ID") == [
        "MFG00001",     # by its region
        "MFG00002",     # by its PDP only
        "MFG00002",     # by neither, but built on MFG00002's trench
    ]
    # A pole by its equipment PDP, and one a kilometre from everything stays
    # blank rather than being attributed to a catchment it is nowhere near.
    assert _field_values(os.path.join(run_dir, "Poles.gpkg"), "MFG_ID") == [
        "MFG00002", ""]


def test_a_span_naming_two_regions_is_given_the_one_it_runs_in(tmp_path):
    """A duct crossing both catchments gets the one it actually occupies.

    Nearly the whole catchment is inside the other one here, so a spatial guess
    would answer MFG00001 for a span lying wholly in region B; the region list
    has to decide it by overlap.
    """
    run_dir = str(tmp_path)
    _run_fixture(run_dir)

    assert _field_values(os.path.join(run_dir, "Distribution_Ducts.gpkg"),
                         "MFG_ID") == ["MFG00001", "MFG00002"]


def test_an_existing_tag_is_never_overwritten(tmp_path):
    """The layer knows its own component better than a post-pass does."""
    run_dir = str(tmp_path)
    _run_fixture(run_dir, tagged=True)

    assert _field_values(os.path.join(run_dir, "Coupleurs.gpkg"), "MFG_ID") == [
        "MFG00002"]


def test_the_pass_is_safe_to_run_twice(tmp_path):
    """A second pass finds nothing to add and changes nothing."""
    run_dir = str(tmp_path)
    first = _run_fixture(run_dir)
    assert first > 0
    before = {f: _field_values(os.path.join(run_dir, f), "MFG_ID")
              for f in ("Polygons.gpkg", "Chambers.gpkg", "Distribution_Ducts.gpkg")}

    second = attr_enrich.stamp_mfg_identity(run_dir, _Feedback())

    assert second == 0
    for f, values in before.items():
        assert _field_values(os.path.join(run_dir, f), "MFG_ID") == values
