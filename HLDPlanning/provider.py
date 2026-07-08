# -*- coding: utf-8 -*-
from typing import Iterable, List, Type
import traceback

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsProcessingProvider,
    QgsMessageLog,
    Qgis,
    QgsProcessingAlgorithm,
)

# Central registry (algorithms/__init__.py)
from .algorithms import ALL_ALGORITHMS, IMPORT_ERRORS


class HLDPlanningProvider(QgsProcessingProvider):
    """
    Processing provider for HLDPlanning.
    Registers algorithms from the central algorithms package.
    """

    PROVIDER_ID = "hldplanning"
    PROVIDER_NAME = "HLDPlanning"

    def __init__(self, icon: QIcon = QIcon()):
        super().__init__()
        self._icon = icon

    def id(self) -> str:
        return self.PROVIDER_ID

    def name(self) -> str:
        return QCoreApplication.translate("HLDPlanningProvider", self.PROVIDER_NAME)

    def icon(self) -> QIcon:
        return self._icon

    def longName(self) -> str:
        return self.name()

    def loadAlgorithms(self) -> None:
        """
        Instantiate and register algorithms from ALL_ALGORITHMS.
        Adds duplicate-ID protection and detailed error reporting.
        """
        registries: List[Iterable[Type[QgsProcessingAlgorithm]]] = [ALL_ALGORITHMS]

        for label, err in IMPORT_ERRORS.items():
            QgsMessageLog.logMessage(
                f"Skipped algorithm import {label}: {err}",
                self.PROVIDER_NAME, level=Qgis.Critical
            )

        seen_ids = set()
        added = 0

        for reg in registries:
            for cls in reg:
                try:
                    alg = cls()
                    alg_id = alg.id()

                    if alg_id in seen_ids:
                        QgsMessageLog.logMessage(
                            f"Skipped duplicate algorithm id '{alg_id}' from {cls.__name__}.",
                            self.PROVIDER_NAME, level=Qgis.Warning
                        )
                        continue

                    self.addAlgorithm(alg)
                    seen_ids.add(alg_id)
                    added += 1

                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"Failed to add algorithm {getattr(cls, '__name__', str(cls))}: {e}\n{traceback.format_exc()}",
                        self.PROVIDER_NAME, level=Qgis.Critical
                    )

        QgsMessageLog.logMessage(
            f"Loaded {added} algorithms into {self.PROVIDER_NAME}.",
            self.PROVIDER_NAME, level=Qgis.Info
        )
