"""CBT-area subdivision: caps, coverage, and contiguity (pure Python)."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from HLDPlanning.utils.cbt_areas import split_into_cbt_groups  # noqa: E402


def _street(n, hh_each, step=10.0):
    """n premises in a straight line, `step` metres apart, `hh_each` homes each."""
    return [(f"b{i:03d}", i * step, 0.0, hh_each) for i in range(n)]


def _homes(groups, members):
    by_key = {k: hh for k, _x, _y, hh in members}
    return [sum(by_key[k] for k in g) for g in groups]


def test_small_area_is_one_cbt():
    members = _street(4, 3)  # 12 HP <= 16
    groups = split_into_cbt_groups(members, cap=16)
    assert len(groups) == 1
    assert sorted(groups[0]) == sorted(k for k, *_ in members)


def test_every_group_within_cap_and_every_premises_once():
    members = _street(20, 2)  # 40 HP
    groups = split_into_cbt_groups(members, cap=16)
    assert all(h <= 16 for h in _homes(groups, members))
    flat = [k for g in groups for k in g]
    assert sorted(flat) == sorted(k for k, *_ in members)
    assert len(flat) == len(set(flat))


def test_64_home_pdp_area_gives_at_most_four_cbts():
    members = _street(16, 4)  # 64 HP, the PDP-area cap
    groups = split_into_cbt_groups(members, cap=16)
    assert len(groups) == 4
    assert _homes(groups, members) == [16, 16, 16, 16]


def test_single_big_building_is_its_own_cbt():
    members = [("only", 0.0, 0.0, 20)]  # one building, 20 HP > cap
    assert split_into_cbt_groups(members, cap=16) == [["only"]]


def test_big_building_is_not_split_across_cbts():
    members = [("a", 0.0, 0.0, 3), ("big", 10.0, 0.0, 20), ("c", 20.0, 0.0, 3)]
    groups = split_into_cbt_groups(members, cap=16)
    owners = [g for g in groups if "big" in g]
    assert len(owners) == 1
    assert sum(1 for g in groups for k in g if k == "big") == 1


def test_groups_are_contiguous_along_the_street():
    members = _street(12, 2)  # 24 HP along x
    groups = split_into_cbt_groups(members, cap=16)
    index = {k: i for i, (k, *_r) in enumerate(members)}
    for g in groups:
        idx = sorted(index[k] for k in g)
        assert idx == list(range(idx[0], idx[-1] + 1)), "a CBT must be one run along the street"


def test_is_deterministic():
    members = _street(9, 3)
    assert split_into_cbt_groups(members, cap=16) == split_into_cbt_groups(list(members), cap=16)


def test_empty_area_gives_no_groups():
    assert split_into_cbt_groups([], cap=16) == []


def test_rejects_non_positive_cap():
    try:
        split_into_cbt_groups(_street(2, 1), cap=0)
    except ValueError:
        return
    raise AssertionError("cap=0 must raise")
