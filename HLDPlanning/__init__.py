# -*- coding: utf-8 -*-
"""
HLDPlanning QGIS plugin package initializer.

Ensures the GDAL Processing provider is registered before any plugin
algorithms (e.g., One Click HLD Planner) run. This makes the plugin
resilient to user profiles where the GDAL provider is disabled.
"""

from typing import Optional

__all__ = ["classFactory", "__version__"]
__version__ = "1.2.0"  # GDAL auto-register w/ fallback & retention

# Keep a global handle to avoid provider being garbage-collected
_gdal_provider_ref: Optional[object] = None


def _ensure_gdal_provider_loaded() -> bool:
    """
    Make sure the Processing 'gdal' provider is available.
    Returns True if available or successfully registered, else False.
    """
    try:
        from qgis.core import QgsApplication  # type: ignore
        reg = QgsApplication.processingRegistry()
        if reg.providerById("gdal"):
            return True

        # Try primary import path (QGIS-bundled)
        try:
            from processing.algs.gdal.GdalAlgorithmProvider import (  # type: ignore
                GdalAlgorithmProvider,
            )
        except Exception:
            # Fallback path name used on some distros/builds
            from processing_gdal.gdalprovider import (  # type: ignore
                GdalAlgorithmProvider,
            )

        global _gdal_provider_ref
        _gdal_provider_ref = GdalAlgorithmProvider()
        reg.addProvider(_gdal_provider_ref)

        # Verify it registered
        return reg.providerById("gdal") is not None

    except Exception:
        return False


def classFactory(iface):
    """QGIS plugin entry point."""
    # Try to ensure GDAL provider is up before loading the plugin
    ok = _ensure_gdal_provider_loaded()

    # Best-effort logging (don't hard-fail on logging issues)
    try:
        from qgis.core import QgsMessageLog  # type: ignore
        if ok:
            QgsMessageLog.logMessage(
                "GDAL Processing provider is active (auto-checked by HLDPlanning).",
                "HLDPlanning",
                0,
            )
        else:
            QgsMessageLog.logMessage(
                "GDAL Processing provider could not be auto-enabled. "
                "If algorithms using gdal:* fail, enable it via "
                "Settings → Options → Processing → Providers → GDAL.",
                "HLDPlanning",
                2,
            )
    except Exception:
        pass

    # Load and return the main plugin
    from .plugin import HLDPlanningPlugin  # type: ignore
    return HLDPlanningPlugin(iface)
