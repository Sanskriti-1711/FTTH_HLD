"""The area preview's reference layers come from the area's own OSM store.

Railways, waterways, trees and protected areas used to be read only from the
curated ``gis.osm_*`` tables, which are loaded by hand for one area.  A preview
of any other area therefore drew them empty, and the permit rules that read them
recorded a gap instead of a crossing.  These tests pin the three halves of the
fix: the area download fetches the layers, they are kept in their own store
tables, and the preview prefers that store while keeping the curated table as a
fallback.
"""
import osm_source


def _polygon():
    return {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}


def _way(osm_id, tags, coords=None):
    coords = coords or [(0, 0), (1, 1)]
    return {
        "type": "way", "id": osm_id, "tags": tags,
        "geometry": [{"lon": c[0], "lat": c[1]} for c in coords],
    }


def _node(osm_id, tags):
    return {"type": "node", "id": osm_id, "tags": tags, "lon": 0.0, "lat": 0.0}


# ---------------------------------------------------------------------------
# The area download carries the layers
# ---------------------------------------------------------------------------

def test_the_area_download_fetches_the_reference_layers():
    groups = osm_source._OVERPASS_GROUPS
    assert groups["railways"] == 'way["railway"]'
    assert groups["waterways"] == 'way["waterway"]'
    # Individual trees are nodes; `natural=tree_row` is a different feature.
    assert groups["trees"] == 'node["natural"="tree"]'


def test_every_fetch_group_has_a_human_phase_label():
    """The page shows the phase while a group downloads, so a group with no
    label would surface as the raw `fetching_<group>` state."""
    for group in osm_source._OVERPASS_GROUPS:
        assert osm_source._FETCH_PHASE_LABELS.get(f"fetching_{group}"), group


def test_the_store_ddl_creates_a_table_for_every_reference_layer():
    ddl = osm_source._OSM_DDL.format(schema=osm_source.OSM_SCHEMA)
    for table in ("railways", "waterways", "trees", "protected_areas"):
        assert f"{osm_source.OSM_SCHEMA}.{table} (" in ddl
        assert f"{osm_source.OSM_SCHEMA}_{table}_geom_idx" in ddl


def test_the_extract_version_was_bumped_for_the_new_layers():
    """A v3 extract holds none of the new layers, so serving it as a cache hit
    would draw exactly the empty layers this change exists to fill."""
    assert osm_source._EXTRACT_VERSION >= 4


# ---------------------------------------------------------------------------
# Classification: one table each, and the landuse layer is left alone
# ---------------------------------------------------------------------------

def test_railways_waterways_trees_and_protected_areas_land_in_their_own_tables():
    classified = osm_source.classify_elements([
        _way(1, {"railway": "rail", "name": "Main line"}),
        _way(2, {"waterway": "river", "name": "Avon"}),
        _node(3, {"natural": "tree", "leaf_type": "broadleaved"}),
        _way(4, {"boundary": "protected_area", "name": "Reserve"},
             [(0, 0), (1, 0), (1, 1), (0, 0)]),
    ])
    assert len(classified["railways"]) == 1
    assert len(classified["waterways"]) == 1
    assert len(classified["trees"]) == 1
    assert len(classified["protected_areas"]) == 1
    # The named column is the tag, carried through rather than renamed.
    assert classified["railways"][0][2] == "rail"
    assert classified["waterways"][0][2] == "river"
    assert classified["trees"][0][3] == "broadleaved"
    assert classified["protected_areas"][0][5] == "Reserve"


def test_a_tree_node_is_not_stored_as_a_landuse_point():
    """A tree carries `natural`, which would otherwise send every tree into the
    landuse table -- where the aerial-zone derivation expects area geometry."""
    classified = osm_source.classify_elements([_node(3, {"natural": "tree"})])
    assert len(classified["trees"]) == 1
    assert classified["landuse"] == []


def test_a_waterway_that_is_also_a_landuse_stays_a_landuse():
    """A riverbank carries `natural=water` and is already in the landuse layer;
    storing the waterway must not take it out of there."""
    classified = osm_source.classify_elements(
        [_way(2, {"waterway": "riverbank", "natural": "water"})])
    assert len(classified["waterways"]) == 1
    assert len(classified["landuse"]) == 1


def test_a_protected_area_stays_in_the_landuse_layer():
    classified = osm_source.classify_elements(
        [_way(4, {"boundary": "protected_area"})])
    assert len(classified["protected_areas"]) == 1
    assert len(classified["landuse"]) == 1


def test_every_reference_row_matches_its_table_column_count():
    """A row with the wrong arity fails the INSERT, so pin it here instead."""
    classified = osm_source.classify_elements([
        _way(1, {"railway": "rail"}),
        _way(2, {"waterway": "river"}),
        _node(3, {"natural": "tree"}),
        _way(4, {"boundary": "protected_area"}),
    ])
    for table in ("railways", "waterways", "trees", "protected_areas"):
        rows = classified[table]
        assert rows, f"{table} classified nothing"
        expected = len(osm_source._TABLE_COLUMNS[table])
        for row in rows:
            assert len(row) == expected, f"{table} row arity {len(row)} != {expected}"


# ---------------------------------------------------------------------------
# The preview: area store first, curated table as the fallback
# ---------------------------------------------------------------------------

def test_each_reference_layer_names_both_an_area_and_a_curated_table():
    for layer, (area_table, curated) in osm_source._INPUT_LAYER_TABLES.items():
        assert curated, layer
        if area_table is not None:
            # The area table is the store table, and it needs its columns
            # declared for the preview to expose them as properties.
            assert area_table == layer, layer
            assert area_table in osm_source._AREA_LAYER_COLUMNS, layer


def test_boundaries_are_not_read_from_the_area_store():
    """Administrative boundaries are OSM relations and the area fetcher builds
    geometry for nodes and ways only, so boundaries have no area layer."""
    assert osm_source._INPUT_LAYER_TABLES["boundaries"][0] is None


def test_the_preview_reads_the_area_store_before_the_curated_table(monkeypatch):
    monkeypatch.setattr(osm_source, "_reference_layer_exists", lambda table: True)
    seen = []

    def fake_query(sql, params=()):
        seen.append(sql)
        if f"FROM {osm_source.OSM_SCHEMA}.railways " in sql:
            return [{
                "osm_id": 7,
                "geom_json": '{"type": "LineString", "coordinates": [[0, 0], [1, 1]]}',
                "tags": {"railway": "rail", "name": "Main line"},
                "railway": "rail", "name": "Main line", "ref": None,
                "service": None, "usage": None, "bridge": None, "tunnel": None,
            }]
        if "FROM gis.osm_railway " in sql:
            return [{"id": 99, "geom_json": "{}", "properties": {"name": "curated"}}]
        return []

    monkeypatch.setattr(osm_source, "_query", fake_query)

    out = osm_source.input_layer_geojson(_polygon(), "railways")

    assert out["feature_count"] == 1
    props = out["features"][0]["properties"]
    assert props["name"] == "Main line"
    assert props["railway"] == "rail"
    assert props["source_table"] == f"{osm_source.OSM_SCHEMA}.railways"
    assert props["source_id"] == 7
    # The area store answered, so the curated table was not consulted.
    assert not any("gis.osm_railway" in sql for sql in seen)


def test_the_preview_falls_back_to_the_curated_table_when_the_area_store_is_empty(monkeypatch):
    monkeypatch.setattr(osm_source, "_reference_layer_exists", lambda table: True)

    def fake_query(sql, params=()):
        if f"FROM {osm_source.OSM_SCHEMA}.railways " in sql:
            return []
        if "FROM gis.osm_railway " in sql:
            return [{
                "id": 99,
                "geom_json": '{"type": "LineString", "coordinates": [[0, 0], [1, 1]]}',
                "properties": {"name": "curated"},
            }]
        return []

    monkeypatch.setattr(osm_source, "_query", fake_query)

    out = osm_source.input_layer_geojson(_polygon(), "railways")

    assert out["feature_count"] == 1
    assert out["features"][0]["properties"]["source_table"] == "gis.osm_railway"


def test_a_row_with_jsonb_tags_and_a_row_with_text_tags_read_the_same():
    """The tag set is what a planner inspects, and jsonb arrives as a dict from
    one driver and as text from another; both must survive."""
    assert osm_source._row_tags({"name": "Avon"}) == {"name": "Avon"}
    assert osm_source._row_tags('{"name": "Avon"}') == {"name": "Avon"}
    assert osm_source._row_tags(None) == {}


def test_the_area_read_quotes_every_column(monkeypatch):
    """`natural` is a PostgreSQL reserved word, so an unquoted column list is a
    syntax error -- checked against the database directly.  The trees and
    landuse reads must quote their identifiers."""
    monkeypatch.setattr(osm_source, "_reference_layer_exists", lambda table: True)
    seen = []

    def fake_query(sql, params=()):
        seen.append(sql)
        return []

    monkeypatch.setattr(osm_source, "_query", fake_query)
    osm_source._area_reference_features("trees", _polygon())

    select = next(sql for sql in seen if f"FROM {osm_source.OSM_SCHEMA}.trees" in sql)
    assert '"natural"' in select
    assert ", natural," not in select
