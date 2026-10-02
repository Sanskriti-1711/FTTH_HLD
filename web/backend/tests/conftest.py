"""Backend test configuration.

The area-fetch progress registry mirrors itself to disk so a page polling it
survives an engine restart.  A test run must not: one run's downloads would be
read back by the next, and ``area_fetch_state()`` would report a stored `failed`
for a bbox a test expects to be `unknown`.

Set before the modules under test import ``osm_source``, which is what reading
this conftest at collection time guarantees.
"""
import os

os.environ.setdefault("HLD_AREA_FETCH_STATE", "memory")
