# -*- coding: utf-8 -*-
import os
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from qgis.core import QgsApplication

from .provider import HLDPlanningProvider


def _tr(s: str) -> str:
    # Safe translate helper (class is not a QObject)
    return QCoreApplication.translate("HLDPlanningPlugin", s)


class HLDPlanningPlugin:
    """
    Lightweight wrapper that registers/unregisters the processing provider.
    Also exposes a convenient menu/toolbar action to open the One-Click workflow.
    """
    # Canonical id of the End-to-End ("One Click") algorithm. Must match
    # EndToEndPipelineAlgorithm.name() ("end_to_end_pipeline") under the
    # "hldplanning" provider — see algorithms/oneclick.py.
    ONECLICK_ALG_ID = "hldplanning:end_to_end_pipeline"

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action_oneclick = None

    def _make_icon(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icons", "hldplanning.svg")
        try:
            return QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        except Exception:
            return QIcon()

    def initProcessing(self):
        """Register the Processing provider.

        QGIS calls this for BOTH the desktop GUI and headless runs
        (``qgis_process``) when ``metadata.txt`` declares
        ``hasProcessingProvider=yes``. Registering here — rather than only in
        ``initGui`` — is what makes the algorithms available to qgis_process
        and CI. Guarded so repeated calls do not double-register.
        """
        if self.provider is not None:
            return
        self.provider = HLDPlanningProvider(icon=self._make_icon())
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        # Ensure the provider is registered (no-op if initProcessing already ran).
        self.initProcessing()

        icon = self._make_icon()

        # Optional: handy launcher for the one-click algorithm
        self.action_oneclick = QAction(_tr("One Click – HLD Planner"), self.iface.mainWindow())
        self.action_oneclick.setIcon(icon)
        self.action_oneclick.setToolTip(_tr("Open the one-click HLD Planning workflow"))
        self.action_oneclick.triggered.connect(self.run_oneclick)
        self.iface.addToolBarIcon(self.action_oneclick)
        self.iface.addPluginToMenu(_tr("&HLD Planning"), self.action_oneclick)

    def unload(self):
        # Remove the launcher action
        if self.action_oneclick is not None:
            try:
                self.iface.removeToolBarIcon(self.action_oneclick)
                self.iface.removePluginMenu(_tr("&HLD Planning"), self.action_oneclick)
            finally:
                self.action_oneclick = None

        # Unregister the provider
        if self.provider is not None:
            try:
                QgsApplication.processingRegistry().removeProvider(self.provider)
            finally:
                self.provider = None

    # Convenience runner for Processing dialog
    def run_oneclick(self):
        # NOTE: the id MUST be the algorithm's real id. The previous value
        # ("hldplanning:oneclick_hld_planner") was a stale draft name, so the
        # toolbar/menu button opened nothing and failed silently.
        try:
            from qgis import processing
            processing.execAlgorithmDialog(self.ONECLICK_ALG_ID)
        except Exception as exc:
            # Don't fail silently — surface why the dialog didn't open.
            from qgis.core import QgsMessageLog, Qgis
            QgsMessageLog.logMessage(
                f"Could not open One-Click dialog for '{self.ONECLICK_ALG_ID}': {exc}",
                "HLDPlanning", level=Qgis.Critical,
            )
            if self.iface is not None:
                self.iface.messageBar().pushWarning(
                    "HLD Planning",
                    f"Could not open the One-Click workflow ({self.ONECLICK_ALG_ID}). See the log.",
                )
