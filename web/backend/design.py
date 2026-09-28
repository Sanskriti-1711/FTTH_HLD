"""Civil trench design runner for the engine API.

Phase A of ``docs/subprojects/ftth-engine/TRENCH_DESIGN.md``: the standalone
trench designer (``HLDPlanning/design/trench_design.py``) is exposed as an
on-demand design run for a project that already has HLD outputs.

Inputs come from the project itself — nothing is re-uploaded:

* plan anchors : ``<outputs>/<project>/`` MFG / PDPs / Objects / Polygons
* routing base : ``<outputs>/<project>/inputs/`` roads (gpkg or zipped shapefile)
* aerial zones : ``inputs/aerial_zones.*`` when present, otherwise derived from
  the optional OSM landuse reference layer (restricted green space only)

Outputs land in ``<outputs>/<project>/design/`` and are served as GeoJSON by
``GET /ftth/hld/results/{project_id}/design``.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND_DIR = Path(__file__).resolve().parent
HLD_ROOT = BACKEND_DIR.parents[1]                    # HLD_Planning_01
OUTPUT_DIR = BACKEND_DIR / "outputs"

if str(HLD_ROOT) not in sys.path:
    sys.path.insert(0, str(HLD_ROOT))
# The engine is normally started with the plugin root already importable; keep
# the explicit insert above so a direct ``python -c "import design"`` works too.

# Design layer name -> (output file stem, label)
DESIGN_LAYERS: Dict[str, Tuple[str, str]] = {
    "trenches": ("Final_Trenches", "Trench spans (Open Cut / HDD / Garden)"),
    "nodes": ("Trench_Nodes", "Structural nodes (pits, PDPs, bends, pulls)"),
    "crossings": ("Tangent_Crossings", "HDD road crossings"),
    "aerial_drops": ("Aerial_Drops", "Aerial drop legs"),
    "aerial_zones": ("Aerial_Zones", "Aerial zones (no excavation)"),
}

# OSM landuse classes where underground construction is restricted. Roadside
# classes (grass, scrub, garden, recreation_ground) are deliberately excluded —
# in OSM they are mostly verges, and including them blankets the whole AOI.
RESTRICTED_LANDUSE = {
    "park", "forest", "nature_reserve", "meadow", "wetland", "wood",
    "farmland", "farm", "orchard", "vineyard", "cemetery", "allotments",
}
MIN_ZONE_AREA_M2 = 2000.0

_tasks: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# paths / status
# ─────────────────────────────────────────────────────────────────────────────

def design_dir(project_id: str) -> Path:
    return OUTPUT_DIR / project_id / "design"


def _status_file(project_id: str) -> Path:
    return design_dir(project_id) / "status.json"


def read_status(project_id: str) -> Dict[str, Any]:
    with _lock:
        live = _tasks.get(project_id)
    if live is not None:
        return dict(live)
    path = _status_file(project_id)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    if (design_dir(project_id) / "trench_design_report.json").is_file():
        return {"status": "completed"}
    return {"status": "missing"}


def _set_status(project_id: str, **fields: Any) -> Dict[str, Any]:
    state = read_status(project_id)
    state.update(fields)
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _lock:
        _tasks[project_id] = dict(state)
    try:
        design_dir(project_id).mkdir(parents=True, exist_ok=True)
        _status_file(project_id).write_text(
            json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass
    return state


# ─────────────────────────────────────────────────────────────────────────────
# inputs
# ─────────────────────────────────────────────────────────────────────────────

def _first_existing(paths: List[Path]) -> Optional[Path]:
    for p in paths:
        if p and p.exists():
            return p
    return None


def _shapefile_in_zip(zip_path: Path,
                      prefer: Optional[str] = None) -> Optional[str]:
    """``/vsizip/`` path to a .shp inside an archive (optionally preferring
    a name fragment, e.g. the landuse layer of a multi-layer bundle)."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".shp")]
    except (OSError, zipfile.BadZipFile):
        return None
    if not names:
        return None
    if prefer:
        for name in names:
            if prefer in Path(name).name.lower():
                return f"/vsizip/{zip_path.as_posix()}/{name}"
    return f"/vsizip/{zip_path.as_posix()}/{names[0]}"


def design_inputs(project_id: str) -> Dict[str, Any]:
    """Resolve every designer input from the project folder."""
    out = OUTPUT_DIR / project_id
    if not out.is_dir():
        raise RuntimeError(f"project {project_id} has no outputs directory")
    inputs = out / "inputs"

    anchors: Dict[str, Path] = {}
    for key, stem in (("mfg", "MFG"), ("pdps", "PDPs"),
                      ("objects", "Objects"), ("polygons", "Polygons")):
        path = _first_existing([out / f"{stem}.geojson", out / f"{stem}.gpkg"])
        if path is None:
            raise RuntimeError(
                f"HLD output {stem} is missing — run the HLD pipeline first")
        anchors[key] = path

    roads = _first_existing([
        inputs / "roads.gpkg", inputs / "roads.geojson", inputs / "roads.shp",
    ])
    if roads is None:
        for pattern in ("berlin-roads-bundle.zip", "roads*.zip", "*.zip"):
            for candidate in sorted(inputs.glob(pattern)):
                vsi = _shapefile_in_zip(candidate)
                if vsi:
                    # NOTE: a /vsizip/ path must stay a *string* with forward
                    # slashes — Path() turns it into a Windows path GDAL rejects.
                    roads = vsi
                    break
            if roads is not None:
                break
    if roads is None:
        roads = _first_existing(sorted(inputs.glob("roads*")))
    if roads is None:
        raise RuntimeError(
            "project roads input is missing (inputs/roads.gpkg or roads .zip)")

    aerial = _first_existing(sorted(inputs.glob("aerial_zones.*"))
                             + sorted(inputs.glob("Aerial_Zones.*")))
    landuse = None
    osm_dir = inputs / "osm" / "landuse"
    if osm_dir.is_dir():
        for candidate in sorted(osm_dir.iterdir()):
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() == ".zip":
                vsi = _shapefile_in_zip(candidate, prefer="landuse")
                if vsi:
                    landuse = vsi
                    break
                continue
            landuse = str(candidate)
            break
    return {"output_dir": out, "roads": str(roads),
            "aerial": str(aerial) if aerial else None,
            "landuse": landuse,
            **{k: str(v) for k, v in anchors.items()}}


def _bbox_25833(paths: List[Path], buffer_m: float = 400.0
                ) -> Tuple[float, float, float, float]:
    from osgeo import ogr, osr

    dst = osr.SpatialReference()
    dst.ImportFromEPSG(25833)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    xs: List[float] = []
    ys: List[float] = []
    for path in paths:
        ds = ogr.Open(str(path))
        if ds is None:
            continue
        for li in range(ds.GetLayerCount()):
            lyr = ds.GetLayer(li)
            src = lyr.GetSpatialRef()
            if src is not None:
                src = src.Clone()
                try:
                    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                except Exception:
                    pass
            tr = osr.CoordinateTransformation(src, dst) if src is not None else None
            for feat in lyr:
                g = feat.GetGeometryRef()
                if g is None or g.IsEmpty():
                    continue
                g = g.Clone()
                if tr is not None:
                    g.Transform(tr)
                minx, maxx, miny, maxy = g.GetEnvelope()
                xs += [minx, maxx]
                ys += [miny, maxy]
        ds = None
    if not xs:
        raise RuntimeError("cannot compute the project AOI from its HLD outputs")
    return (min(xs) - buffer_m, max(xs) + buffer_m,
            min(ys) - buffer_m, max(ys) + buffer_m)


def derive_aerial_zones(landuse, anchor_paths, out_path: Path
                        ) -> Optional[Path]:
    """Restricted-area polygons from the OSM landuse layer, clipped to the AOI."""
    from osgeo import ogr, osr

    bbox = _bbox_25833(anchor_paths)
    ds = ogr.Open(str(landuse))
    if ds is None:
        return None
    lyr = ds.GetLayer(0)
    src = lyr.GetSpatialRef()
    if src is not None:
        src = src.Clone()
        try:
            src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except Exception:
            pass
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(25833)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst) if src is not None else None

    clip = ogr.Geometry(ogr.wkbPolygon)
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((bbox[0], bbox[2]), (bbox[1], bbox[2]),
                 (bbox[1], bbox[3]), (bbox[0], bbox[3]), (bbox[0], bbox[2])):
        ring.AddPoint_2D(x, y)
    clip.AddGeometry(ring)

    drv = ogr.GetDriverByName("GeoJSON")
    if out_path.exists():
        drv.DeleteDataSource(str(out_path))
    out = drv.CreateDataSource(str(out_path))
    ol = out.CreateLayer("Aerial_Zones", dst, ogr.wkbMultiPolygon)
    ol.CreateField(ogr.FieldDefn("ZONE_ID", ogr.OFTString))
    ol.CreateField(ogr.FieldDefn("FCLASS", ogr.OFTString))
    ol.CreateField(ogr.FieldDefn("AREA_HA", ogr.OFTReal))
    defn = ol.GetLayerDefn()

    kept = 0
    ol.StartTransaction()
    for feat in lyr:
        cls = (feat.GetField("fclass") or "").strip()
        if cls not in RESTRICTED_LANDUSE:
            continue
        g = feat.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        g = g.Clone()
        if tr is not None:
            g.Transform(tr)
        minx, maxx, miny, maxy = g.GetEnvelope()
        if maxx < bbox[0] or minx > bbox[1] or maxy < bbox[2] or miny > bbox[3]:
            continue
        g = g.Intersection(clip)
        if g is None or g.IsEmpty() or g.GetArea() < MIN_ZONE_AREA_M2:
            continue
        if g.GetGeometryName() == "POLYGON":
            multi = ogr.Geometry(ogr.wkbMultiPolygon)
            multi.AddGeometry(g)
            g = multi
        kept += 1
        ft = ogr.Feature(defn)
        ft.SetField("ZONE_ID", "AZ-%05d" % kept)
        ft.SetField("FCLASS", cls)
        ft.SetField("AREA_HA", round(g.GetArea() / 10000.0, 3))
        ft.SetGeometry(g)
        ol.CreateFeature(ft)
    ol.CommitTransaction()
    out = None
    if not kept:
        try:
            out_path.unlink()
        except OSError:
            pass
        return None
    return out_path


def _restricted_landuse_classes(landuse: Optional[str]) -> List[str]:
    """The restricted classes present in a landuse layer, for reporting only.

    Reads the same `fclass` field the derivation reads, so "the area has
    restricted land" and "the zones were derived from it" are decided by one
    rule.  Returns [] for a missing/unreadable layer, which is deliberately
    silent: no landuse means no claim either way.
    """
    if not landuse:
        return []
    from osgeo import ogr
    try:
        ds = ogr.Open(str(landuse))
        if ds is None:
            return []
        lyr = ds.GetLayer(0)
        idx = lyr.GetLayerDefn().GetFieldIndex("fclass")
        if idx < 0:
            return []
        found = set()
        for feat in lyr:
            cls = (feat.GetField(idx) or "").strip()
            if cls in RESTRICTED_LANDUSE:
                found.add(cls)
    except Exception:  # noqa: BLE001 - reporting must never fail a design
        return []
    return sorted(found)


# ─────────────────────────────────────────────────────────────────────────────
# run
# ─────────────────────────────────────────────────────────────────────────────

def run_design(project_id: str, force: bool = False) -> Dict[str, Any]:
    """Run the designer for a project (blocking). Returns the status dict."""
    from HLDPlanning.design import trench_design as td

    target = design_dir(project_id)
    if (target / "trench_design_report.json").is_file() and not force:
        # Already designed: keep the existing report, just make the state
        # coherent (a queued write must not linger next to a completed run).
        return _set_status(project_id, status="completed", stage="Complete",
                           error=None)

    _set_status(project_id, status="running", stage="Resolving inputs",
                error=None)
    try:
        src = design_inputs(project_id)
        target.mkdir(parents=True, exist_ok=True)

        # ── aerial zones: explicit layer wins, else derive from OSM landuse ──
        _set_status(project_id, status="running", stage="Resolving aerial zones")
        aerial_path = src["aerial"]
        zones_out = target / "Aerial_Zones.geojson"
        # Whether the landuse the area supplied actually CONTAINS restricted
        # ground.  This is the difference between "there is nothing here worth
        # protecting" and "the layer was unreadable / the derivation failed",
        # and only the first is a reason to have no zones.
        restricted = _restricted_landuse_classes(src["landuse"])
        zone_note = None
        if aerial_path is None and src["landuse"] is not None:
            try:
                derived = derive_aerial_zones(
                    src["landuse"],
                    [src["objects"], src["pdps"], src["mfg"], src["polygons"]],
                    zones_out)
                if derived is not None:
                    aerial_path = derived
            except Exception as exc:  # noqa: BLE001
                aerial_path = None  # zones are optional; design without them
                zone_note = f"the aerial-zone derivation failed ({type(exc).__name__})"
        elif aerial_path is not None:
            try:
                shutil.copyfile(str(aerial_path), str(zones_out))
                aerial_path = zones_out
            except OSError:
                pass

        _set_status(project_id, status="running", stage="Designing trenches")
        cfg: Dict[str, Any] = {
            "mfg": str(src["mfg"]), "pdps": str(src["pdps"]),
            "objects": str(src["objects"]), "polygons": str(src["polygons"]),
            "roads": str(src["roads"]), "out": str(target),
            "aerial": str(aerial_path) if aerial_path else None,
            "target_epsg": 25833,
        }
        report = td.design(cfg)
        # Say so when the design could not be protected, and only then. A drop
        # leg is classified aerial on `zone` only when a zone exists, so an area
        # whose landuse layer was missing or unreadable is indistinguishable
        # downstream from an area with no restricted land — the trenching is
        # legal-looking either way, and the run log is the only place the
        # difference is still visible.
        if restricted and not aerial_path:
            zone_note = zone_note or "the landuse layer could not be read"
        return _set_status(
            project_id, status="completed", stage="Complete", error=None,
            spans=report.get("spans"), runs=report.get("runs"),
            total_length_m=report.get("total_length_m"),
            aerial_legs=report.get("aerial_legs", 0),
            aerial_length_m=report.get("aerial_length_m", 0),
            aerial_zones=bool(aerial_path),
            restricted_landuse=restricted,
            aerial_zone_note=(
                f"No aerial zones were derived, because {zone_note}. Drop legs were "
                f"therefore classified on length and chain only, and any leg in "
                f"restricted ground ({', '.join(restricted)}) was trenched rather "
                f"than built overhead."
                if restricted and not aerial_path else None
            ),
            drills=report.get("drills"))
    except Exception as exc:  # surfaced to the client instead of a 500
        return _set_status(project_id, status="failed", stage="Failed",
                           error=f"{type(exc).__name__}: {exc}")


def start_design(project_id: str, force: bool = False) -> Dict[str, Any]:
    """Kick off a design run in a background thread, return the live status."""
    with _lock:
        live = _tasks.get(project_id)
    if live is not None and live.get("status") == "running":
        return dict(live)
    _set_status(project_id, status="queued", stage="Queued", error=None)
    thread = threading.Thread(
        target=run_design, args=(project_id, force), daemon=True)
    thread.start()
    return read_status(project_id)


# ─────────────────────────────────────────────────────────────────────────────
# read
# ─────────────────────────────────────────────────────────────────────────────

def payload(project_id: str, include_layers: bool = True) -> Dict[str, Any]:
    """Status + report + every design layer as GeoJSON."""
    status = read_status(project_id)
    target = design_dir(project_id)
    report: Optional[Dict[str, Any]] = None
    report_path = target / "trench_design_report.json"
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = None

    if report is not None and status.get("status") == "completed" \
            and status.get("stage") in (None, "", "Queued"):
        status = dict(status, stage="Complete")

    layers: Dict[str, Any] = {}
    counts: Dict[str, int] = {}
    if include_layers and report is not None:
        for name, (stem, label) in DESIGN_LAYERS.items():
            path = target / f"{stem}.geojson"
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            layers[name] = {"label": label, "geojson": data}
            counts[name] = len(data.get("features") or [])
    # The design is written in the project CRS (metres), which a browser map
    # cannot draw. Publish the CRS with the payload so the gateway can
    # reproject to WGS84 for the map without guessing.
    crs = "EPSG:25833"
    if isinstance(report, dict):
        try:
            crs = "EPSG:%d" % int((report.get("params") or {}).get("target_epsg", 25833))
        except (TypeError, ValueError):
            crs = "EPSG:25833"
    return {
        "project_id": project_id,
        "status": status.get("status", "missing"),
        "stage": status.get("stage"),
        "error": status.get("error"),
        "updated_at": status.get("updated_at"),
        "crs": crs,
        "report": report,
        "counts": counts,
        "layers": layers,
    }
