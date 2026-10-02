"""Generate only the separate AI review artifact for an existing HLD project."""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
HLD_ROOT = BACKEND_DIR.parent.parent
if str(HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(HLD_ROOT))

from HLDPlanning.utils.attr_enrich import verify_surface_geometry


PROJECT_ID = "19c55183e8c7471cb16fd08e58d9009a"
OUTPUT_DIR = BACKEND_DIR / "outputs" / PROJECT_ID


def main() -> None:
    os.environ["SURFACE_AI_REVIEW"] = "1"
    # Provider, model and API key come from the environment (see .env): the
    # local Ollama service was removed when the review moved to Gemini.
    os.environ.setdefault("SURFACE_AI_PROVIDER", "gemini")
    # Same default as the module and Compose: IGN where it has imagery, Esri
    # World Imagery where it does not (IGN reports that case as a blank patch,
    # which the model otherwise reads as "the image is completely blank").
    os.environ["SURFACE_AI_IMAGE_PROVIDER"] = (
        "HLDPlanning.design.surface_ai_review:worldwide_surface_imagery"
    )
    # Default to a single candidate span; override with SURFACE_AI_MAX_SPANS
    # when running a wider review on a host with faster inference.
    os.environ.setdefault("SURFACE_AI_MAX_SPANS", "1")

    roads = OUTPUT_DIR / "inputs" / "roads.geojson"
    report = verify_surface_geometry(str(OUTPUT_DIR), roads_source=str(roads))
    print({
        "project_id": PROJECT_ID,
        "checked": report.get("checked"),
        "uncertain": report.get("uncertain"),
        "geometry_flags": len(report.get("flags", [])),
        "ai_review": report.get("ai_review"),
        "report_path": str(OUTPUT_DIR / "surface_ai_review.json"),
    })


if __name__ == "__main__":
    main()
