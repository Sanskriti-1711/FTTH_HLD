"""Backend test configuration.

The area-fetch progress registry mirrors itself to disk so a page polling it
survives an engine restart.  A test run must not: one run's downloads would be
read back by the next, and ``area_fetch_state()`` would report a stored `failed`
for a bbox a test expects to be `unknown`.

Set before the modules under test import ``osm_source``, which is what reading
this conftest at collection time guarantees.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("HLD_AREA_FETCH_STATE", "memory")

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest  # noqa: E402

import osm_source  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_overpass_health():
    """No test inherits a mirror's liveness from the test before it.

    The engine remembers which Overpass mirrors answered for a short while, so
    without this a test that expects a probe round would find the previous
    test's answer still fresh and skip the probe -- and the tests would
    depend on each other's order.
    """
    osm_source._OVERPASS_HEALTH.clear()
    yield
    osm_source._OVERPASS_HEALTH.clear()
