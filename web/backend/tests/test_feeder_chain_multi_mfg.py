"""The feeder chain is verified per assigned MFG on multi-exchange designs.

Both feeder diagnostics once assumed ONE exchange area: the duct verifier
scored every PDP against MFG[0], and the cable graph-healer treated every
non-main MFG as an island. This suite checks the correct contract instead:
PDP.MFG_ID is authoritative, a PDP must reach that specific MFG's duct root,
and it must physically lie on the duct within the snap tolerance.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_feeder_chain_multi_mfg.py -v
"""

import pathlib
import sys

import pytest

_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.utils import attr_enrich  # noqa: E402


class _FB(object):
    def __init__(self):
        self.lines = []

    def pushInfo(self, message):
        self.lines.append(str(message))

    def pushWarning(self, message):
        self.lines.append("WARN " + str(message))


def _pt(x, y):
    from osgeo import ogr

    def build():
        g = ogr.Geometry(ogr.wkbPoint)
        g.AddPoint(x, y)
        return g
    return build


def _line(a, b):
    from osgeo import ogr

    def build():
        g = ogr.Geometry(ogr.wkbLineString)
        g.AddPoint(*a)
        g.AddPoint(*b)
        return g
    return build


def _mk(path, geom_type, rows, fields):
    """rows = [(geometry builder, {field: value})]"""
    ogr = pytest.importorskip("osgeo.ogr")
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    lyr = ds.CreateLayer("layer", geom_type=geom_type)
    for name, ftype in fields:
        lyr.CreateField(ogr.FieldDefn(name, ftype))
    for build, props in rows:
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(build())
        for k, v in props.items():
            f.SetField(k, v)
        lyr.CreateFeature(f)
    ds = None


def _run(tmp_path, mfgs, pdps, ducts):
    """mfgs = [(x,y,id)], pdps = [(x,y,id,mfg_id)], ducts = [(a,b,mfg_id,pdp_ids)]"""
    from osgeo import ogr

    _mk(tmp_path / "MFG.gpkg", ogr.wkbPoint,
        [(_pt(x, y), {"MFG_ID": mid}) for x, y, mid in mfgs],
        [("MFG_ID", ogr.OFTString)])
    _mk(tmp_path / "PDPs.gpkg", ogr.wkbPoint,
        [(_pt(x, y), {"PDP_ID": pid, "MFG_ID": mid})
         for x, y, pid, mid in pdps],
        [("PDP_ID", ogr.OFTString), ("MFG_ID", ogr.OFTString)])
    _mk(tmp_path / "Feeder_Ducts.gpkg", ogr.wkbLineString,
        [(_line(a, b), {"mfg_id": mid, "pdp_ids": ids}) for a, b, mid, ids in ducts],
        [("mfg_id", ogr.OFTString), ("pdp_ids", ogr.OFTString)])
    return str(tmp_path)


def _two_mfg_run(tmp_path):
    """Two exchange areas, each with its own duct and one PDP at its far end.

    Nothing joins the two lines, and that is correct — they are different
    exchange areas.  The old verifier scored this 1/2 reached and warned
    that the chain was broken.
    """
    return _run(
        tmp_path,
        mfgs=[(0, 0, "MFG00001"), (1000, 0, "MFG00002")],
        pdps=[(500, 0, "PDP00001", "MFG00001"),
              (1500, 0, "PDP00002", "MFG00002")],
        ducts=[((0, 0), (500, 0), "MFG00001", "PDP00001"),
               ((1000, 0), (1500, 0), "MFG00002", "PDP00002")],
    )


def test_each_mfg_reaches_its_own_pdps(tmp_path):
    out = _two_mfg_run(tmp_path)
    fb = _FB()
    rep = attr_enrich.verify_duct_continuity(out, fb)

    assert rep["pdp_total"] == 2
    assert rep["pdp_reached"] == 2, rep["pdp_stranded"]
    assert rep["pdp_stranded"] == []
    assert rep["feeder_parts"] == 2
    assert rep["mfg_parts"] == 2
    assert not any("chain is broken" in m for m in fb.lines)
    assert any("assigned MFG reaches 2/2 PDP(s)" in m for m in fb.lines)


def test_assigned_mfg_roots_can_share_a_connected_duct(tmp_path):
    """Two assigned MFGs on one shared duct: one part, both roots valid."""
    out = _run(
        tmp_path,
        mfgs=[(0, 0, "MFG00001"), (1000, 0, "MFG00002")],
        pdps=[(500, 0, "PDP00001", "MFG00001"),
              (1500, 0, "PDP00002", "MFG00002")],
        ducts=[((0, 0), (1500, 0), "MFG00001", "PDP00001,PDP00002")],
    )
    rep = attr_enrich.verify_duct_continuity(out, _FB())
    assert rep["feeder_parts"] == 1
    assert rep["pdp_reached"] == 2


def test_pdp_on_a_network_with_no_mfg_is_stranded(tmp_path):
    """The fix must not turn the check into a rubber stamp."""
    out = _run(
        tmp_path,
        mfgs=[(0, 0, "MFG00001")],
        pdps=[(500, 0, "PDP00001", "MFG00001"),
              (9500, 0, "PDP00009", "MFG00001")],
        ducts=[((0, 0), (500, 0), "MFG00001", "PDP00001"),
               ((9000, 0), (9500, 0), "MFG00009", "PDP00009")],
    )
    fb = _FB()
    rep = attr_enrich.verify_duct_continuity(out, fb)
    assert rep["pdp_total"] == 2
    assert rep["pdp_reached"] == 1
    assert rep["pdp_stranded"] == ["PDP00009"]
    assert any("chain is broken" in m for m in fb.lines)


def test_pdp_off_the_duct_network_is_reported_separately(tmp_path):
    """A PDP nowhere near the ducts is "off the network", not a broken chain."""
    out = _run(
        tmp_path,
        mfgs=[(0, 0, "MFG00001")],
        pdps=[(500, 0, "PDP00001", "MFG00001"),
              (9000, 9000, "PDP00009", "MFG00001")],
        ducts=[((0, 0), (500, 0), "MFG00001", "PDP00001")],
    )
    fb = _FB()
    rep = attr_enrich.verify_duct_continuity(out, fb)
    assert rep["pdp_reached"] == 1
    assert rep["pdp_off_network"] == ["PDP00009"]
    assert "PDP00009" in rep["pdp_stranded"]
    assert any("not on the duct network" in m for m in fb.lines)
    assert not any("chain is broken" in m for m in fb.lines)


def test_pdp_reaching_a_different_mfg_is_still_stranded(tmp_path):
    """Any-root reach is insufficient when a PDP has an assigned MFG."""
    out = _run(
        tmp_path,
        mfgs=[(0, 0, "MFG00001"), (1000, 0, "MFG00002")],
        pdps=[(500, 0, "PDP00001", "MFG00002")],
        ducts=[((0, 0), (500, 0), "MFG00001", "PDP00001")],
    )
    fb = _FB()
    rep = attr_enrich.verify_duct_continuity(out, fb)
    assert rep["pdp_reached"] == 0
    assert rep["pdp_stranded"] == ["PDP00001"]
    assert any("MFG_ID with no feeder-duct root" in m for m in fb.lines)


def test_pdp_halfway_along_a_duct_counts_as_on_it(tmp_path):
    """Nodes are only segment endpoints — a mid-span PDP is still on the net."""
    out = _run(
        tmp_path,
        mfgs=[(0, 0, "MFG00001")],
        pdps=[(700, 0, "PDP00001", "MFG00001")],
        ducts=[((0, 0), (1000, 0), "MFG00001", "PDP00001")],
    )
    rep = attr_enrich.verify_duct_continuity(out, _FB())
    assert rep["pdp_reached"] == 1
    assert rep["pdp_off_network"] == []


def test_anchor_uses_nearest_segment_component_not_nearest_endpoint(tmp_path):
    """A nearby unrelated endpoint cannot steal an on-span PDP's component."""
    out = _run(
        tmp_path,
        mfgs=[(0, 0, "MFG00001"), (500, 1, "MFG00002")],
        pdps=[(500, 0, "PDP00001", "MFG00001")],
        ducts=[((0, 0), (1000, 0), "MFG00001", "PDP00001"),
               ((500, 1), (510, 1), "MFG00002", "")],
    )
    rep = attr_enrich.verify_duct_continuity(out, _FB(),
                                               include_distribution=False)
    assert rep["pdp_reached"] == 1
    assert rep["pdp_stranded"] == []


def test_distribution_index_reports_off_duct_couplers(tmp_path):
    """The indexed large-run check retains exact point-to-segment distances."""
    from osgeo import ogr

    out = _run(
        tmp_path,
        mfgs=[(0, 0, "MFG00001")],
        pdps=[(1000, 0, "PDP00001", "MFG00001")],
        ducts=[((0, 0), (1000, 0), "MFG00001", "PDP00001")],
    )
    _mk(tmp_path / "Coupleurs.gpkg", ogr.wkbPoint,
        [(_pt(500, 0), {"DUCT_UID": "near"}),
         (_pt(500, 10), {"DUCT_UID": "off"})],
        [("DUCT_UID", ogr.OFTString)])
    _mk(tmp_path / "Distribution_Ducts.gpkg", ogr.wkbLineString,
        [(_line((0, 0), (1000, 0)), {})], [])

    rep = attr_enrich.verify_duct_continuity(out)
    assert rep["coupler_total"] == 2
    assert rep["couplers_off"] == 1
    assert rep["coupler_worst_m"] == 10.0
