"""Tests for deterministic, contiguous multi-MFG service-area partitioning."""

from __future__ import annotations

import unittest

from HLDPlanning.utils.mfg_partition import partition_mfg_areas


class MfgPartitionTests(unittest.TestCase):
    def test_large_contiguous_region_is_split_within_hard_cap(self):
        loads = {f"P{i:02d}": 900 for i in range(10)}
        edges = [(f"P{i:02d}", f"P{i + 1:02d}") for i in range(9)]

        groups = partition_mfg_areas(loads, edges, target_hh=4000, max_hh=5000)

        self.assertEqual(sum(group["hh_count"] for group in groups), 9000)
        self.assertGreater(len(groups), 1)
        self.assertTrue(all(group["hh_count"] <= 5000 for group in groups))
        by_polygon = {
            polygon: group["mfg_id"]
            for group in groups
            for polygon in group["polygon_keys"]
        }
        self.assertEqual(set(by_polygon), set(loads))
        for group in groups:
            members = set(group["polygon_keys"])
            reached = {min(members, key=str)}
            while True:
                expanded = reached | {
                    right for left, right in edges
                    if left in reached and right in members
                } | {
                    left for left, right in edges
                    if right in reached and left in members
                }
                if expanded == reached:
                    break
                reached = expanded
            self.assertEqual(reached, members)

    def test_disconnected_components_are_not_joined(self):
        groups = partition_mfg_areas(
            {"A": 2400, "B": 2100, "X": 2600},
            [("A", "B")],
            target_hh=4000,
            max_hh=5000,
        )
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group["hh_count"] <= 5000 for group in groups))
        self.assertEqual({frozenset(group["polygon_keys"]) for group in groups},
                         {frozenset(("A", "B")), frozenset(("X",))})

    def test_single_indivisible_polygon_over_cap_is_flagged_for_review(self):
        groups = partition_mfg_areas({"A": 5200, "B": 100}, [("A", "B")])
        oversized = next(group for group in groups if group["polygon_keys"] == ("A",))
        self.assertEqual(oversized["capacity_status"], "OVER_CAPACITY")
        self.assertEqual(oversized["review"], 1)
        self.assertTrue(oversized["capacity_warning"])

    def test_assignments_are_deterministic(self):
        loads = {f"P{i:02d}": 1000 for i in range(9)}
        edges = [(f"P{i:02d}", f"P{i + 1:02d}") for i in range(8)]
        first = partition_mfg_areas(loads, edges)
        second = partition_mfg_areas(dict(reversed(list(loads.items()))), reversed(edges))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
