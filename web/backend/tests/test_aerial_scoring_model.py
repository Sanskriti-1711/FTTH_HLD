"""The docx's point model: "Instead of many IF conditions, assign points".

``docs/aerial planning.docx`` offers a scoring model beside the drop decision
chain, with its own weights and its own three bands:

    No duct available +30 / Existing poles +25 / Rocky terrain +20 /
    Concrete road +15 / NH crossing +20 / Low customer density +15 /
    Permit delay >60 days +15 / Flood area +10 /
    Spare duct available -50 / Metro city core -30 /
    High customer density -20 / Pole loading >80% -50

    Score < 20  -> UG
    Score 20-50 -> Cost comparison
    Score > 50  -> Aerial

The rules live in ``HLDPlanning/design/aerial_feasibility.py`` (pure Python, no
QGIS, no GDAL), so they are tested directly here — including the part that
matters most in practice: a fact nobody measured scores NOTHING and says so,
because an unreadable pole layer must never be read as "no pole".

Run from the engine backend dir:

    cd HLD_Planning_01/web/backend
    python -m pytest tests/test_aerial_scoring_model.py -v
"""

import pathlib
import sys

import pytest

# HLDPlanning/ lives above HLD_Planning_01/web/backend/tests
_HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(_HLD_ROOT))

from HLDPlanning.design import aerial_feasibility as af  # noqa: E402


# ── the table itself ────────────────────────────────────────────────────────

def test_the_weights_are_the_docx_weights():
    assert af.SCORE_BY_FACT == {
        "no_duct": 30.0, "existing_poles": 25.0, "rocky_terrain": 20.0,
        "concrete_road": 15.0, "nh_crossing": 20.0, "low_density": 15.0,
        "permit_delay": 15.0, "flood_area": 10.0, "spare_duct": -50.0,
        "metro_core": -30.0, "high_density": -20.0, "pole_loading_high": -50.0,
    }


def test_the_bands_are_the_docx_bands():
    assert af.SCORE_UG_BELOW == 20.0
    assert af.SCORE_AERIAL_ABOVE == 50.0
    assert af.POLE_LOADING_LIMIT == 0.80


@pytest.mark.parametrize("fact,points", [
    ("existing_poles", 25.0), ("rocky_terrain", 20.0), ("concrete_road", 15.0),
    ("nh_crossing", 20.0), ("permit_delay", 15.0), ("flood_area", 10.0),
])
def test_each_line_scores_on_its_own(fact, points):
    kwargs = {
        "existing_poles": {"existing_poles": True},
        "rocky_terrain": {"terrain": "rock"},
        "concrete_road": {"concrete_road": True},
        "nh_crossing": {"crosses_nh": True},
        "permit_delay": {"permit_delay_days": 61.0},
        "flood_area": {"terrain": "wetland"},
    }[fact]
    result = af.drop_score(**kwargs)
    assert result.score == points
    assert fact in result.points


def test_the_duct_line_is_one_of_two():
    assert af.drop_score(spare_duct=False).score == 30.0   # no duct available
    assert af.drop_score(spare_duct=True).score == -50.0   # a spare way wins
    assert "no_duct" in af.drop_score(spare_duct=False).points
    assert "no_duct" not in af.drop_score(spare_duct=True).points


def test_rock_and_water_are_priced_apart():
    assert af.drop_score(terrain="rock").points == {"rocky_terrain": 20.0}
    assert af.drop_score(terrain="wetland").points == {"flood_area": 10.0}
    assert af.drop_score(terrain="sand").points == {}
    assert af.drop_score(terrain="SCREE").points == {"rocky_terrain": 20.0}


def test_a_terrain_that_is_not_rock_or_flood_is_named_in_the_notes():
    result = af.drop_score(terrain="sand")
    assert any("sand is not rocky or flood" in n for n in result.notes)


# ── unmeasured facts ────────────────────────────────────────────────────────

def test_an_empty_call_scores_nothing_and_explains_every_line():
    result = af.drop_score()
    assert result.score == 0.0
    assert result.points == {}
    assert result.decision == "ug"
    # one note per fact line, so a reviewer can see what was never asked
    joined = " | ".join(result.notes)
    for fact in ("duct", "pole route", "pole loading", "terrain",
                 "NH crossing", "concrete road", "customer density",
                 "permit delay", "metro city core"):
        assert fact in joined, fact


def test_an_unmeasured_fact_is_never_read_as_false():
    # the failure this guards: an unreadable pole layer must not silently cost
    # a drop the 25 points that an existing pole line is worth
    unmeasured = af.drop_score(existing_poles=None)
    measured_no = af.drop_score(existing_poles=False)
    assert unmeasured.score == measured_no.score == 0.0
    assert unmeasured.points == {}
    assert any("not measured" in n for n in unmeasured.notes)


def test_a_missing_pole_route_is_named_as_a_mandatory_condition():
    result = af.drop_score(existing_poles=False)
    assert any("mandatory condition" in n for n in result.notes)


# ── thresholds the docx states ──────────────────────────────────────────────

def test_density_below_the_low_threshold_scores():
    assert af.drop_score(density_km=19.9).points == {"low_density": 15.0}
    assert af.drop_score(density_km=20.0).points == {}
    assert af.drop_score(density_km=100.0).points == {}


def test_high_density_needs_a_threshold_the_docx_does_not_state():
    # unset: the line is reported, never guessed
    unset = af.drop_score(density_km=500.0)
    assert unset.points == {}
    assert any("no high threshold is set" in n for n in unset.notes)

    # set: it scores, and the two lines are exclusive
    assert af.drop_score(density_km=80.0,
                         density_high_min=60.0).points == {
        "high_density": -20.0}
    between = af.drop_score(density_km=40.0, density_high_min=60.0)
    assert between.points == {}
    assert any("between the low and high thresholds" in n
               for n in between.notes)


def test_permit_delay_is_over_the_limit_not_at_it():
    assert af.drop_score(permit_delay_days=90.0).points == {
        "permit_delay": 15.0}
    assert af.drop_score(permit_delay_days=61.0).points == {
        "permit_delay": 15.0}
    assert af.drop_score(permit_delay_days=60.0).points == {}
    assert af.drop_score(permit_delay_days=90.0,
                         permit_limit_days=120.0).points == {}


def test_pole_loading_at_or_above_the_limit_costs_fifty():
    assert af.drop_score(pole_loading_pct=80.0).points == {
        "pole_loading_high": -50.0}
    assert af.drop_score(pole_loading_pct=79.9).points == {}
    # the limit is the docx's, but a project may set its own
    assert af.drop_score(pole_loading_pct=90.0,
                         pole_loading_limit=1.0).points == {}


def test_metro_core_is_a_declared_fact():
    assert af.drop_score(metro_core=True).points == {"metro_core": -30.0}
    assert af.drop_score(metro_core=False).points == {}
    assert any("not declared" in n for n in af.drop_score().notes)


# ── the bands ───────────────────────────────────────────────────────────────

def test_the_decision_bands():
    assert af.decision_for_score(19.9) == "ug"
    assert af.decision_for_score(20.0) == "ug"          # middle band is UG
    assert af.decision_for_score(50.0) == "ug"          # at the limit too
    assert af.decision_for_score(50.1) == "aerial"
    assert af.decision_for_score(-50.0) == "ug"


def test_the_middle_band_is_the_docx_cost_comparison():
    # "Score 20-50 -> Cost comparison": built aerial only when the project has
    # done that comparison itself
    assert af.decision_for_score(35.0) == "ug"
    assert af.decision_for_score(35.0, compare_aerial=True) == "aerial"
    # the switch must not reach outside its own band
    assert af.decision_for_score(10.0, compare_aerial=True) == "ug"
    assert af.decision_for_score(60.0, compare_aerial=True) == "aerial"


def test_points_add_up_across_the_table():
    result = af.drop_score(existing_poles=True, terrain="rock",
                           crosses_nh=True, permit_delay_days=90.0)
    assert result.score == 25.0 + 20.0 + 20.0 + 15.0
    assert result.decision == "aerial"
    # strongest first, so a log line names what actually carried the leg
    assert result.firing == ["existing_poles", "rocky_terrain", "nh_crossing",
                             "permit_delay"]


def test_a_full_negative_stack_goes_underground():
    result = af.drop_score(spare_duct=True, existing_poles=True,
                           pole_loading_pct=95.0, metro_core=True)
    assert result.score == -50.0 - 50.0 - 30.0 + 25.0
    assert result.decision == "ug"


def test_the_score_is_rounded_to_two_places():
    assert af.drop_score(existing_poles=True,
                         density_km=1.0).score == 40.0


# ── the small predicates the callers use ────────────────────────────────────

@pytest.mark.parametrize("fclass", ["motorway", "TRUNK", "motorway_link",
                                    "trunk_link"])
def test_national_highways(fclass):
    assert af.is_nh_class(fclass) is True


@pytest.mark.parametrize("fclass", ["primary", "secondary", "residential",
                                    "service", "", None])
def test_other_classes_are_not_national_highways(fclass):
    assert af.is_nh_class(fclass) is False


@pytest.mark.parametrize("surface", ["concrete", "CONCRETE", "concrete:plates",
                                     "concrete:lanes"])
def test_concrete_surfaces(surface):
    assert af.is_concrete_surface(surface) is True


@pytest.mark.parametrize("surface", ["asphalt", "paving_stones", "gravel", "",
                                     None])
def test_other_surfaces_are_not_concrete(surface):
    assert af.is_concrete_surface(surface) is False


def test_terrain_split_is_case_insensitive_and_exclusive():
    assert af.score_terrain("ROCK") == "rocky_terrain"
    assert af.score_terrain("marsh") == "flood_area"
    assert af.score_terrain("sand") == ""
    assert af.score_terrain(None) == ""
