"""A drawn section of an area: the geometry the page sends and the run designs.

Why this exists: a planner needs a share of an area -- an estate, one side of a
trunk road, or simply the part that fits what the engine can run -- and before
this the only way to ask for it was to find a smaller published boundary that
happened to line up with what they meant.  A drawn polygon now replaces the
resolved one for the preview, the review layers and the run, so the three things
pinned here are that a malformed drawing is refused in words, that the section
really replaces the boundary (not sits beside it), and that the counts and the
input files are built from the section and not the whole area.

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_area_section.py -v
"""

from __future__ import annotations

import pathlib
import sys

import pytest

_BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import main  # noqa: E402
import osm_source  # noqa: E402

# ~0.4 km² in Birmingham.
SECTION = {
    "type": "Polygon",
    "coordinates": [
        [[-1.9250, 52.4740], [-1.9210, 52.4740], [-1.9210, 52.4770],
         [-1.9250, 52.4770], [-1.9250, 52.4740]]
    ],
}
PARENT = {
    "type": "Polygon",
    "coordinates": [
        [[-1.9317, 52.4703], [-1.9132, 52.4703], [-1.9132, 52.4814],
         [-1.9317, 52.4814], [-1.9317, 52.4703]]
    ],
}


def _resolution(**overrides):
    base = {
        "area": "B16 8UU",
        "matched": "B16 8UU, Birmingham, England",
        "country_code": "GB",
        "country": "United Kingdom",
        "city": "Birmingham",
        "input_type": "postcode",
        "polygon": PARENT,
        "bbox": [-1.9317, 52.4703, -1.9132, 52.4814],
        "area_km2": 1.033,
        "bbox_km2": 1.033,
        "polygon_source": "nominatim",
        "boundary_name": "B16 8UU",
        "boundary_licence": "ODbL",
        "boundary_kind": "postcode",
        "boundary_code": "B168UU",
    }
    base.update(overrides)
    return base


def test_a_drawn_polygon_replaces_the_boundary_not_sits_beside_it():
    narrowed = osm_source.narrow_to_selection(_resolution(), SECTION)

    assert narrowed["polygon"] == SECTION
    assert narrowed["bbox"] == [-1.925, 52.474, -1.921, 52.477]
    assert narrowed["polygon_source"] == "selection"
    # The parent is still named: a design on a section is a design ON something,
    # and the run's provenance has to be able to say which area it was cut from.
    assert narrowed["selection_of"] == "B16 8UU, Birmingham, England"
    assert "selected section" in narrowed["matched"]
    assert narrowed["area_km2"] < 1.033
    assert narrowed["parent_area_km2"] == 1.033


def test_no_selection_leaves_the_resolution_untouched():
    resolution = _resolution()

    assert osm_source.narrow_to_selection(resolution, None) is resolution
    assert osm_source.narrow_to_selection(resolution, "") is resolution


def test_an_open_ring_is_closed_rather_than_refused():
    open_ring = {
        "type": "Polygon",
        "coordinates": [[[-1.9250, 52.4740], [-1.9210, 52.4740], [-1.9210, 52.4770]]],
    }

    polygon = osm_source.selection_geometry(open_ring)

    assert polygon["coordinates"][0][0] == polygon["coordinates"][0][-1]
    assert len(polygon["coordinates"][0]) == 4


def test_a_feature_and_json_text_are_accepted():
    """The three callers carry the drawing differently; all three must work."""
    as_feature = {"type": "Feature", "properties": {}, "geometry": SECTION}

    assert osm_source.selection_geometry(as_feature) == SECTION
    assert osm_source.selection_geometry(__import__("json").dumps(SECTION)) == SECTION


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({"type": "Point", "coordinates": [-1.9, 52.4]}, "single polygon"),
        ({"type": "Polygon", "coordinates": [[[-1.9, 52.4], [-1.8, 52.4]]]}, "three corners"),
        ({"type": "Polygon", "coordinates": [[[1000, 52.4], [-1.8, 52.4], [-1.8, 52.5]]]}, "outside the map"),
        ({"type": "Polygon", "coordinates": []}, "no outline"),
        (
            {
                "type": "Polygon",
                "coordinates": [[[-1.9000, 52.4000], [-1.8999, 52.4000],
                                 [-1.8999, 52.4001], [-1.9000, 52.4000]]],
            },
            "too small",
        ),
    ],
)
def test_a_drawing_that_cannot_be_designed_is_refused_in_words(raw, expected):
    with pytest.raises(ValueError) as raised:
        osm_source.selection_geometry(raw)

    assert expected in str(raised.value)


def test_the_preview_counts_the_section(monkeypatch):
    """Every figure the page shows must come from the drawn polygon."""
    seen = {}

    monkeypatch.setattr(osm_source, "resolve_area", lambda *a, **k: _resolution())
    monkeypatch.setattr(osm_source, "area_inputs_state",
                        lambda area, bbox: {"ready": True, "state": {}})
    monkeypatch.setattr(osm_source, "ensure_area_data",
                        lambda area, bbox: {"source": "cache"})
    monkeypatch.setattr(osm_source, "household_register_for", lambda cc, pcs: None)
    monkeypatch.setattr(osm_source, "area_fetch_state",
                        lambda area, bbox: {"state": "ready"})
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: True)

    def rows(polygon, include_geometry=True):
        seen["polygon"] = polygon
        return [], [], {}, []

    monkeypatch.setattr(osm_source, "read_area_rows", rows)

    result = osm_source.preview_area("B16 8UU", selection=SECTION)

    assert seen["polygon"] == SECTION
    assert result["polygon"] == SECTION
    assert result["polygon_source"] == "selection"
    assert result["area_km2"] < 1.033


def test_the_run_builds_its_inputs_from_the_section(monkeypatch, tmp_path):
    seen = {}

    monkeypatch.setattr(osm_source, "resolve_area", lambda *a, **k: _resolution())
    monkeypatch.setattr(osm_source.postgis, "is_available", lambda: True)
    monkeypatch.setattr(osm_source, "ensure_area_data",
                        lambda area, bbox: {"source": "cache"})
    monkeypatch.setattr(osm_source, "household_register_for", lambda cc, pcs: None)
    monkeypatch.setattr(osm_source, "household_summary",
                        lambda premises: {"total": 1, "estimated_share": 0.0})

    def rows(polygon, include_geometry=True):
        seen["polygon"] = polygon
        return [], [], {}, [{"osm_id": "way/1", "fclass": "residential",
                             "geometry": {"type": "LineString",
                                          "coordinates": [[-1.925, 52.474],
                                                          [-1.921, 52.474]]}}]

    monkeypatch.setattr(osm_source, "read_area_rows", rows)
    monkeypatch.setattr(
        osm_source, "assemble_premises",
        lambda *a, **k: ([{"ADDR_ID": "1", "Address": "1 Test St"}],
                         {"household_register": None}),
    )
    def write(path, _payload):
        target = pathlib.Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        return str(target)

    monkeypatch.setattr(osm_source, "write_address_workbook", write)
    monkeypatch.setattr(osm_source, "write_roads_geojson", write)
    monkeypatch.setattr(osm_source, "write_landuse_geojson",
                        lambda path, polygon: None)
    monkeypatch.setattr(osm_source, "road_summary", lambda roads: {"total_km": 1.0})
    monkeypatch.setattr(osm_source, "pavement_carriers", lambda roads: {
        "rows": [], "replaced_osm_ids": [], "derived_km": 0.0, "mapped_km": 0.0,
        "total_km": 0.0, "pieces": 0, "tied": 0, "nodings": 0, "crossings": 0,
        "arterials": False,
    })
    built = osm_source.build_inputs(
        "proj", "B16 8UU", str(tmp_path), country_code="GB",
        input_type="postcode", postcode="B16 8UU", selection=SECTION,
    )

    assert seen["polygon"] == SECTION
    assert built["selection_of"] == "B16 8UU, Birmingham, England"
    assert built["selection_km2"] < 1.033
    assert built["area_km2"] < 1.033


def test_the_endpoints_forward_the_drawn_polygon(monkeypatch):
    """A selection that stops at the API layer never reaches the geometry."""
    seen = {}

    def preview(area, **kwargs):
        seen["preview"] = kwargs.get("selection")
        return {"area": area, "bbox": []}

    monkeypatch.setattr(osm_source, "preview_area", preview)
    monkeypatch.setattr(osm_source, "ensure_area_data_background",
                        lambda area, bbox: {"state": "queued"})

    main.resolve_area_endpoint({"area": "B16 8UU", "polygon": SECTION})

    assert seen["preview"] == SECTION
