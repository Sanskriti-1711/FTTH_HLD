"""A distribution duct must CONTAIN the cable it is built from (rule D5).

The duct stage runs after the cable stage and derives each region's duct from
that region's cables, so the duct can never be shorter than its own cable.
`_corridor_for` broke that: it replaces the cable geometry with a *selection* of
trench spans (same POLYGON_ID, within 0.5 m of the cable, plus the span nearest
each pseudo-HH), and every span the selection drops left cable with no duct
under it.

Measured on run 0f2bc48d (2026-11-02, Lozells): 85.6 % of the distribution cable
length sat within 5 cm of a distribution duct, 57.9 % of the remainder was more
than 10 m from any duct, and 108 of the 408 Distribution-tier trench spans
carried less than half a duct. Feeder cables were 99.8 % on their 4-way ducts
and drop cables 100 % on their drop ducts — only distribution was short.

The fix unites the bin's cables back into the duct after the corridor and tap
passes, so containment holds by construction.

Run under the QGIS interpreter, from the repo root:

    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py \\
        HLD_Planning_01/tests/test_distribution_duct_contains_its_cable.py
"""

from __future__ import annotations

import pytest

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType

from HLDPlanning.algorithms.duct_layer import DuctLayer

pytestmark = pytest.mark.usefixtures("qgis_app")

CRS_AUTHID = "EPSG:3857"

CABLE = [(0.0, 0.0), (100.0, 0.0)]
# The corridor only knows the first half of the route: a span the cable rides
# but the selection does not return.
CORRIDOR = [(0.0, 0.0), (50.0, 0.0)]


class _Fb:
    """Minimal feedback stand-in that keeps the messages it was given."""

    def __init__(self):
        self.info = []
        self.errors = []

    def pushInfo(self, msg):
        self.info.append(str(msg))

    def reportError(self, msg):
        self.errors.append(str(msg))

    def pushWarning(self, msg):
        self.info.append(str(msg))

    def text(self):
        return "\n".join(self.info + self.errors)


def _line_layer(geoms, name, fields, values):
    lyr = QgsVectorLayer(f"LineString?crs={CRS_AUTHID}", name, "memory")
    assert lyr.isValid()
    pr = lyr.dataProvider()
    pr.addAttributes([QgsField(fn, ft) for fn, ft in fields])
    lyr.updateFields()
    feats = []
    for coords in geoms:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPolylineXY(
            [QgsPointXY(x, y) for x, y in coords]))
        for fname, value in values.items():
            f[fname] = value
        feats.append(f)
    pr.addFeatures(feats)
    lyr.updateExtents()
    return lyr


def _cable_layer(coords, cable_type="Distribution"):
    return _line_layer(
        [coords], "cables",
        [("cable_id", QMetaType.Type.QString),
         ("CABLE_TYPE", QMetaType.Type.QString),
         ("PDP_IDS", QMetaType.Type.QString),
         ("POLYGON_ID", QMetaType.Type.QString)],
        {"cable_id": "C-1", "CABLE_TYPE": cable_type,
         "PDP_IDS": "PDP00001", "POLYGON_ID": "POLY00001"},
    )


def _corridor_layer(coords):
    return _line_layer(
        [coords], "Final_Trenches",
        [("TRENCH_ID", QMetaType.Type.QString),
         ("POLYGON_ID", QMetaType.Type.QString),
         ("TRENCH_TIER", QMetaType.Type.QString)],
        {"TRENCH_ID": "TR-000001", "POLYGON_ID": "POLY00001",
         "TRENCH_TIER": "Distribution"},
    )


@pytest.fixture(autouse=True)
def _no_brownfield(monkeypatch):
    """These runs have no existing-infrastructure registry.

    `_build_route_ducts` stamps reuse onto every duct it writes; with no
    registry loaded `BrownfieldLineIndex.build()` returns None and there is no
    reuse to record.  Stubbing it here keeps the test about containment instead
    of about the brownfield loader, which has its own suite.
    """
    import HLDPlanning.utils.reuse as reuse_mod

    monkeypatch.setattr(reuse_mod.BrownfieldLineIndex, "build",
                        classmethod(lambda cls, *a, **k: None))
    monkeypatch.setattr(reuse_mod, "stamp_feature", lambda *a, **k: None)
    return None


def _ducts(path):
    lyr = QgsVectorLayer(str(path), "ducts", "ogr")
    assert lyr.isValid(), "the duct stage wrote no layer"
    geoms = [f.geometry() for f in lyr.getFeatures()]
    return lyr, geoms


def test_a_distribution_duct_covers_the_span_the_corridor_dropped(tmp_path, _no_brownfield):
    """The corridor returns half the cable's route; the duct must still carry it."""
    cables = _cable_layer(CABLE)
    corridor = _corridor_layer(CORRIDOR)
    out = str(tmp_path / "Distribution_Ducts.gpkg")
    fb = _Fb()

    DuctLayer()._build_route_ducts(
        cables, out, "Distribution",
        QgsCoordinateReferenceSystem(CRS_AUTHID), QgsProcessingContext(), fb,
        corridor_lyr=corridor,
    )

    lyr, geoms = _ducts(out)
    assert lyr.featureCount() == 1, "one region, one duct"
    duct = QgsGeometry.unaryUnion(geoms)
    # The duct reaches the far end of its own cable...
    assert duct.distance(QgsGeometry.fromPointXY(QgsPointXY(100.0, 0.0))) < 0.05
    # ...and covers it end to end, not just the half the corridor offered.
    assert duct.length() == pytest.approx(100.0, abs=0.1)
    leftover = duct.difference(QgsGeometry.fromPolylineXY(
        [QgsPointXY(x, y) for x, y in CABLE]).buffer(0.05, 8))
    assert leftover.length() < 0.5, f"duct leaves the cable by {leftover.length():.2f}"


def test_the_fix_reports_what_the_corridor_would_have_lost(tmp_path):
    """The run log says how much duct the containment union had to add."""
    fb = _Fb()
    DuctLayer()._build_route_ducts(
        _cable_layer(CABLE), str(tmp_path / "Distribution_Ducts.gpkg"),
        "Distribution", QgsCoordinateReferenceSystem(CRS_AUTHID),
        QgsProcessingContext(), fb, corridor_lyr=_corridor_layer(CORRIDOR),
    )

    text = fb.text()
    assert "duct/cable containment: 1 duct(s)" in text
    assert "50.0 m" in text


def test_a_duct_never_shrinks_below_a_cable_the_corridor_covers(tmp_path):
    """When the corridor already spans the cable, the union changes nothing."""
    fb = _Fb()
    out = str(tmp_path / "Distribution_Ducts.gpkg")
    DuctLayer()._build_route_ducts(
        _cable_layer(CABLE), out, "Distribution",
        QgsCoordinateReferenceSystem(CRS_AUTHID), QgsProcessingContext(), fb,
        corridor_lyr=_corridor_layer(CABLE),
    )

    lyr, geoms = _ducts(out)
    assert lyr.featureCount() == 1
    assert QgsGeometry.unaryUnion(geoms).length() == pytest.approx(100.0, abs=0.1)
    assert "duct/cable containment" not in fb.text()
