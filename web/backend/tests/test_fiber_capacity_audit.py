"""Audit selected fibre ladder attributes from a persisted full HLD run."""

from __future__ import annotations

import pathlib
import sys
import unittest

from osgeo import ogr

HLD_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(HLD_ROOT))

from HLDPlanning.utils.cable_capacity import (  # noqa: E402
    DROP_FIBER_LADDER,
    DROP_FIBER_MAX,
    RESERVED_SPARE_FIBERS,
    drop_capacity_warning,
    drop_fiber_capacity,
)


class PersistedCableCapacityAuditTests(unittest.TestCase):
    def test_documented_ladder_boundaries_include_reserved_spares(self):
        self.assertEqual(DROP_FIBER_LADDER, (12, 24, 48, 72, 96, 144, 288))
        self.assertEqual(RESERVED_SPARE_FIBERS, 2)
        expected = {
            0: 12, 10: 12, 11: 24, 22: 24, 23: 48, 46: 48,
            47: 72, 70: 72, 71: 96, 94: 96, 95: 144,
            142: 144, 143: 288, 286: 288,
        }
        for households, capacity in expected.items():
            with self.subTest(households=households):
                self.assertEqual(drop_fiber_capacity(households), capacity)
                self.assertIsNone(drop_capacity_warning(households))
        self.assertIsNone(drop_fiber_capacity(287))
        self.assertIn("286 HH", drop_capacity_warning(287))

    def test_full_hld_run_cables_follow_ladder_and_capacity_status(self):
        run_dir = HLD_ROOT / "tmp" / "full_hld_ladder_audit_20260929b"
        if not run_dir.is_dir():
            self.skipTest("isolated full HLD run has not been generated")
        total = 0
        drop_count = 0
        for layer_name in ("Distribution_Cable", "Aerial_Cable"):
            path = run_dir / f"{layer_name}.gpkg"
            if not path.exists():
                continue
            ds = ogr.Open(str(path), 0)
            self.assertIsNotNone(ds, f"could not read {path}")
            layer = ds.GetLayer(0)
            for feature in layer:
                cable_type = str(feature.GetField("CABLE_TYPE") or "").lower()
                if cable_type not in ("drop", "aerial"):
                    continue
                total += 1
                drop_count += cable_type == "drop"
                hh = int(feature.GetField("HH_COUNT") or 0)
                capacity = int(feature.GetField("FIBER_COUNT") or 0)
                self.assertIn(capacity, DROP_FIBER_LADDER)
                self.assertGreaterEqual(capacity, min(DROP_FIBER_LADDER[0], hh + RESERVED_SPARE_FIBERS))
                status = feature.GetField("CAPACITY_STATUS")
                if hh > DROP_FIBER_MAX - RESERVED_SPARE_FIBERS:
                    self.assertEqual(capacity, DROP_FIBER_MAX)
                    self.assertEqual(status, "OVER_CAPACITY")
                    self.assertEqual(feature.GetField("REVIEW"), 1)
                    self.assertLessEqual(feature.GetField("UTIL_PCT"), 100.0)
                    self.assertTrue(feature.GetField("CAPACITY_WARNING"))
                else:
                    self.assertEqual(status, "OK")
                    self.assertEqual(feature.GetField("REVIEW"), 0)
            ds = None
        self.assertGreater(drop_count, 0, "full HLD did not publish physical service-location drops")
        self.assertGreaterEqual(total, drop_count)


if __name__ == "__main__":
    unittest.main()
