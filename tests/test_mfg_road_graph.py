"""PyQGIS checks for MFG reach measured from the cabinet road anchor."""

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer

from HLDPlanning.algorithms.network_layer import _mfg_road_distance_matrix


def _rectangle(x1, y1, x2, y2):
    ring = [
        QgsPointXY(x1, y1), QgsPointXY(x2, y1),
        QgsPointXY(x2, y2), QgsPointXY(x1, y2),
        QgsPointXY(x1, y1),
    ]
    return QgsGeometry.fromPolygonXY([ring])


def _roads(lines):
    layer = QgsVectorLayer("LineString?crs=EPSG:25832", "roads", "memory")
    features = []
    for coords in lines:
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPolylineXY(
            [QgsPointXY(x, y) for x, y in coords]
        ))
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def test_road_distance_includes_served_polygon_access_but_not_anchor_access():
    roads = _roads([[(0, 0), (2000, 0)]])
    polygons = {
        "anchor": _rectangle(490, 90, 510, 110),
        "served": _rectangle(1490, 990, 1510, 1010),
    }

    locations, distances = _mfg_road_distance_matrix(
        polygons, roads, max_road_m=3000,
    )

    assert set(locations) == {"anchor", "served"}
    assert distances[("anchor", "served")] == 2000
    assert distances[("served", "anchor")] == 1100


def test_disconnected_roads_do_not_create_a_false_mfg_route():
    roads = _roads([
        [(0, 0), (500, 0)],
        [(1000, 0), (1500, 0)],
    ])
    polygons = {
        "west": _rectangle(90, 90, 110, 110),
        "east": _rectangle(1390, 90, 1410, 110),
    }

    locations, distances = _mfg_road_distance_matrix(
        polygons, roads, max_road_m=3000,
    )

    assert set(locations) == {"west", "east"}
    assert ("west", "east") not in distances
    assert ("east", "west") not in distances
