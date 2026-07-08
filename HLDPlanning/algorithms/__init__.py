# -*- coding: utf-8 -*-
"""
Central algorithm registry for HLDPlanning.

This module exposes ALL_ALGORITHMS — a flat list of QgsProcessingAlgorithm
classes discovered or manually imported here.

Currently includes:
 - BuildObjectLayer (object-layer geocoding and Excel import)

Add new algorithm classes to this list as the plugin expands.
"""

from typing import Dict, List, Type
from qgis.core import QgsProcessingAlgorithm

ALL_ALGORITHMS: List[Type[QgsProcessingAlgorithm]] = []
IMPORT_ERRORS: Dict[str, str] = {}


def _register(module_name: str, class_name: str) -> None:
    try:
        module = __import__(f"{__name__}.{module_name}", fromlist=[class_name])
        ALL_ALGORITHMS.append(getattr(module, class_name))
    except Exception as exc:
        IMPORT_ERRORS[f"{module_name}.{class_name}"] = str(exc)


_register("object_layer", "BuildObjectLayer")
_register("polygon_layer", "PolygonLayerAlgorithm")
_register("oneclick", "EndToEndPipelineAlgorithm")
_register("network_layer", "NetworkLayerAlgorithm")
_register("trench_layer", "TrenchLayerAlgorithm")
_register("duct_layer", "DuctLayer")
_register("cable_layer", "AlgCableBuilderAll")

__all__ = ["ALL_ALGORITHMS", "IMPORT_ERRORS"]
