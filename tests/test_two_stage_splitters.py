"""Two-stage splitter model (CABINET_SPINE_PLAN.md, Phase 2).

A PDP area holds up to 64 homes. Each CBT serves 16 homes, so a PDP area
needs ceil(hh / 16) CBTs, fed by a primary splitter of 1:4 or 1:8.

Pure Python, no QGIS imports.
"""

from HLDPlanning.utils.splitters import plan_two_stage


def test_zero_or_bad_homes_give_empty_plan():
    for hh in (0, -3, None, "x"):
        plan = plan_two_stage(hh)
        assert plan["groups"] == 0
        assert plan["cbt_count"] == 0


def test_one_cbt_for_up_to_sixteen_homes():
    assert plan_two_stage(8)["cbt_count"] == 1
    assert plan_two_stage(16)["cbt_count"] == 1


def test_seventeen_homes_need_two_cbts():
    assert plan_two_stage(17)["cbt_count"] == 2


def test_cbt_count_is_ceiling_of_homes_over_sixteen():
    for hh in (8, 16, 17, 32, 33, 48, 64, 250):
        plan = plan_two_stage(hh)
        assert plan["cbt_count"] == -(-hh // 16), hh


def test_primary_is_smallest_size_that_covers_the_cbts():
    # 1 CBT fits a 1:4; 2 CBTs need a 1:4 too; 5 CBTs need a 1:8.
    assert plan_two_stage(16)["primary"] == 4
    assert plan_two_stage(64)["primary"] == 4   # 4 CBTs fit a 1:4
    assert plan_two_stage(65)["primary"] == 8   # 5 CBTs need a 1:8


def test_250_homes_give_two_primary_splitters_and_sixteen_cbts():
    plan = plan_two_stage(250)
    assert plan["cbt_count"] == 16
    assert plan["groups"] == 2
    assert plan["primary"] == 8
    assert plan["ports"] == 16
    assert plan["util"] == 100.0


def test_secondary_is_smallest_size_covering_the_homes_on_a_cbt():
    assert plan_two_stage(8)["secondary"] == 16
    assert plan_two_stage(250)["secondary"] == 16
