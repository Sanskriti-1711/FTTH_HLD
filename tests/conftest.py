"""Shared fixtures for tests that exercise the QGIS plugin algorithms.

These tests import `qgis.core`, a compiled module built for CPython 3.12. They
therefore only run under the QGIS interpreter, not the Anaconda/venv one the
Django backend uses. Run them from the repo root with:

    ./HLD_Planning_01/tools/qgis_python.cmd \
        HLD_Planning_01/tools/run_qgis_tests.py

A `QgsApplication` must exist for geometry operations to work; it is created
once per session in headless (`GUIenabled=False`) mode.
"""

from __future__ import annotations

import os

import pytest

QGIS_PREFIX = r"C:\Program Files\QGIS 3.44.6\apps\qgis"


@pytest.fixture(scope="session")
def qgis_app():
    """Initialise a headless QgsApplication for the whole test session."""
    from qgis.core import QgsApplication

    if QgsApplication.instance():
        return QgsApplication.instance()

    if os.path.isdir(QGIS_PREFIX):
        QgsApplication.setPrefixPath(QGIS_PREFIX, True)
    app = QgsApplication([], False)
    app.initQgis()
    return app
