"""Contract checks for publishing MFG service-area polygons end to end."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_mfg_service_area_is_wired_from_network_output_to_engine_and_map():
    oneclick = (ROOT / "HLD_Planning_01/HLDPlanning/algorithms/oneclick.py").read_text()
    engine = (ROOT / "HLD_Planning_01/web/backend/main.py").read_text()
    postgis = (ROOT / "HLD_Planning_01/web/backend/postgis.py").read_text()
    django = (ROOT / "fiber-backend/ftth_hld/config.py").read_text()
    frontend = (ROOT / "fiber-fe/js/ftth-map.js").read_text()

    assert "OUT_MFG_AREAS: \"MFG_Service_Areas.gpkg\"" in oneclick
    assert "self._NET_MFG_AREAS: self._dest(parameters, self.OUT_MFG_AREAS" in oneclick
    assert '"mfg_service_areas", "MFG_Service_Areas.gpkg", "MFG_Service_Areas.geojson"' in engine
    assert '"mfg_service_areas": "mfg_service_areas"' in postgis
    # Whitespace- and quote-insensitive: this is a wiring contract, not a
    # formatting one, and the Ruff pass already re-quoted this literal once.
    assert re.search(
        r"['\"]mfg_service_areas['\"]\s*:\s*\(\s*['\"]MFG_Service_Areas['\"]", django
    )
    assert "mfg_service_areas:" in frontend
