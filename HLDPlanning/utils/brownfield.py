# -*- coding: utf-8 -*-
"""
Brownfield Registry — mutable registry of existing (brownfield) infrastructure assets
with capacity tracking, spatial queries, and graph-edge generation for the routing engine.

Supports: ducts, chambers, poles, fibre, cabinets, trenches.

Usage:
    registry = BrownfieldRegistry(crs, feedback)

    # Load asset layers
    registry.load_ducts(duct_layer, capacity_field="capacity")
    registry.load_chambers(chamber_layer)
    registry.load_trenches(trench_layer)

    # Get edges for routing graph (low-weight = preferred reuse)
    for edge in registry.iter_graph_edges():
        G.add_edge(edge.u, edge.v, weight=edge.weight, asset_id=edge.asset_id, infra_type=edge.infra_type)

    # Check capacity
    if registry.has_capacity(asset_id):
        registry.consume_capacity(asset_id)

    # Classify after routing
    status = registry.classify_asset(asset_id)  # -> "Existing" | "Reused" | "Proposed"
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple, Iterator, Any, Set
from dataclasses import dataclass, field
import uuid

from qgis.core import (
    QgsVectorLayer, QgsGeometry, QgsFeature, QgsSpatialIndex, QgsPointXY,
    QgsWkbTypes, QgsCoordinateReferenceSystem, QgsProcessingFeedback,
)


# ── Process-scoped registry storage ─────────────────────────────────────────
# The brownfield registry is handed from the Load Brownfield stage to the
# downstream stages (trench, network) of the one-click pipeline.  It lives at
# MODULE level on purpose: QgsProject Python attributes are unreliable because
# each QgsProject.instance() call can return a fresh Python wrapper around the
# same C++ singleton — instance attributes are lost when the old wrapper is
# garbage-collected.  All pipeline stages run in the same process, so module
# state is the correct scope (each qgis_process invocation is a fresh process).
_PROJECT_REGISTRY: Optional['BrownfieldRegistry'] = None
_PROJECT_REUSE_ENABLED: bool = True


# ── Asset types ──────────────────────────────────────────────────────────────

class AssetType:
    DUCT = "duct"
    CHAMBER = "chamber"
    POLE = "pole"
    FIBRE = "fibre"
    CABINET = "cabinet"
    TRENCH = "trench"
    PDP = "pdp"          # existing PDP (treated as special chamber)
    MFG = "mfg"          # existing MFG (treated as special cabinet)

    ALL_LINE_TYPES = {DUCT, FIBRE, TRENCH}
    ALL_POINT_TYPES = {CHAMBER, POLE, CABINET, PDP, MFG}

    @classmethod
    def is_line(cls, t: str) -> bool:
        return t in cls.ALL_LINE_TYPES

    @classmethod
    def is_point(cls, t: str) -> bool:
        return t in cls.ALL_POINT_TYPES


# ── Verification status ─────────────────────────────────────────────────────

class VerifyStatus:
    VERIFIED = "Verified"
    ASSUMED = "Assumed"
    SURVEY_REQUIRED = "Survey Required"

    ALL = {VERIFIED, ASSUMED, SURVEY_REQUIRED}

    @classmethod
    def normalize(cls, value: Optional[str]) -> str:
        if not value:
            return cls.ASSUMED
        v = str(value).strip()
        for s in cls.ALL:
            if s.lower() == v.lower():
                return s
        # Fuzzy matching
        low = v.lower()
        if "verif" in low:
            return cls.VERIFIED
        if "survey" in low or "field" in low:
            return cls.SURVEY_REQUIRED
        return cls.ASSUMED


# ── Infrastructure status (output classification) ───────────────────────────

class InfraStatus:
    EXISTING = "Existing"     # in the field, not used by this design
    REUSED = "Reused"         # existing asset incorporated into the design
    PROPOSED = "Proposed"     # newly planned infrastructure
    REMOVED = "Removed"       # existing asset planned for decommissioning

    ALL = {EXISTING, REUSED, PROPOSED, REMOVED}


# ── Graph edge dataclass ────────────────────────────────────────────────────

@dataclass
class BrownfieldEdge:
    """Represents an existing line asset as a graph edge for routing."""
    u: Tuple[float, float]       # start point (x, y) as hashable tuple
    v: Tuple[float, float]       # end point (x, y)
    weight: float                # edge weight (0 = mandatory survey, 0.1 = preferred brownfield)
    asset_id: str                # registry asset id
    infra_type: str              # duct / fibre / trench
    geom: QgsGeometry            # original geometry for output
    capacity_total: int          # total sub-ducts / fibre strands
    capacity_used: int = 0       # currently consumed
    verify_status: str = VerifyStatus.ASSUMED
    use_mode: str = ""           # 'survey' = mandatory (weight=0); '' = regular brownfield


# ── Main Registry ───────────────────────────────────────────────────────────

class BrownfieldRegistry:
    """
    Mutable registry of all brownfield assets.

    Tracks:
    - Asset geometries and metadata
    - Capacity (total / used) for ducts and fibres
    - Verification status
    - Which assets were reused during routing
    """

    def __init__(self, crs: QgsCoordinateReferenceSystem,
                 feedback: Optional[QgsProcessingFeedback] = None):
        self.crs = crs
        self.feedback = feedback
        self._assets: Dict[str, dict] = {}           # asset_id -> metadata
        self._spatial_index: Optional[QgsSpatialIndex] = None
        self._id_to_feat: Dict[int, QgsFeature] = {}  # spatial index fid -> feature
        self._fid_to_asset: Dict[int, str] = {}       # spatial index fid -> asset_id
        self._next_sid = 0                            # spatial index feature id counter
        self._used_assets: Set[str] = set()           # assets consumed during routing

        # Counters for auto-generated IDs
        self._counters: Dict[str, int] = {
            AssetType.DUCT: 0, AssetType.CHAMBER: 0, AssetType.POLE: 0,
            AssetType.FIBRE: 0, AssetType.CABINET: 0, AssetType.TRENCH: 0,
            AssetType.PDP: 0, AssetType.MFG: 0,
        }

    # ── Reporting ────────────────────────────────────────────────────────

    def _report(self, msg: str) -> None:
        if self.feedback:
            self.feedback.pushInfo(msg)

    # ── Asset ID generation ──────────────────────────────────────────────

    def _make_id(self, asset_type: str) -> str:
        self._counters[asset_type] += 1
        return f"BF_{asset_type.upper()}_{self._counters[asset_type]:05d}"

    # ── Spatial index ────────────────────────────────────────────────────

    def _ensure_index(self) -> QgsSpatialIndex:
        if self._spatial_index is None:
            self._spatial_index = QgsSpatialIndex()
        return self._spatial_index

    def _add_to_index(self, asset_id: str, geom: QgsGeometry) -> None:
        idx = self._ensure_index()
        feat = QgsFeature(self._next_sid)
        feat.setGeometry(geom)
        idx.addFeature(feat)
        self._id_to_feat[self._next_sid] = feat
        self._fid_to_asset[self._next_sid] = asset_id
        self._next_sid += 1

    # ── Generic loader ───────────────────────────────────────────────────

    def _load_layer(self, layer: QgsVectorLayer, asset_type: str,
                    capacity_field: Optional[str] = None,
                    capacity_default: int = 1,
                    capacity_used_field: Optional[str] = None,
                    verify_field: Optional[str] = None,
                    id_field: Optional[str] = None,
                    use_mode_field: Optional[str] = None) -> int:
        """
        Load features from a vector layer into the registry.

        Args:
            layer: Source vector layer
            asset_type: One of AssetType.*
            capacity_field: Optional field name for capacity (total sub-ducts etc.)
            capacity_default: Default capacity when no field or value invalid
            capacity_used_field: Optional field name for already-consumed capacity
                (survey spare-capacity / occupancy). Defaults to 0 when absent.
            verify_field: Optional field name for verification status
            id_field: Optional field name for a user-supplied asset ID

        Returns:
            Number of assets loaded
        """
        if layer is None or not layer.isValid() or layer.featureCount() == 0:
            self._report(f"Brownfield: no features in {asset_type} layer (skipped).")
            return 0

        # Ensure CRS matches
        if layer.crs() != self.crs:
            self._report(
                f"Brownfield: {asset_type} layer CRS ({layer.crs().authid()}) "
                f"differs from project CRS ({self.crs.authid()}). Reprojecting..."
            )
            from qgis import processing
            from .layer_io import as_layer
            try:
                layer = as_layer(processing.run(
                    "native:reprojectlayer",
                    {
                        "INPUT": layer,
                        "TARGET_CRS": self.crs,
                        "OUTPUT": "TEMPORARY_OUTPUT",
                    },
                    is_child_algorithm=True,
                )["OUTPUT"])
            except Exception as exc:
                self._report(f"Brownfield: reprojection failed for {asset_type}: {exc}")
                return 0

        # Resolve field indices
        fields = {f.name().lower(): (f.name(), i) for i, f in enumerate(layer.fields())}

        cap_idx = -1
        if capacity_field and capacity_field.lower() in fields:
            cap_idx = fields[capacity_field.lower()][1]

        cap_used_idx = -1
        if capacity_used_field and capacity_used_field.lower() in fields:
            cap_used_idx = fields[capacity_used_field.lower()][1]

        verify_idx = -1
        if verify_field and verify_field.lower() in fields:
            verify_idx = fields[verify_field.lower()][1]

        id_idx = -1
        if id_field and id_field.lower() in fields:
            id_idx = fields[id_field.lower()][1]

        # USE_MODE field: 'survey' = mandatory path; absent = regular brownfield.
        use_mode_idx = -1
        for candidate in (use_mode_field or "", "use_mode", "USE_MODE"):
            if candidate and candidate.lower() in fields:
                use_mode_idx = fields[candidate.lower()][1]
                break

        count = 0
        verify_counts: Dict[str, int] = {}
        for feat in layer.getFeatures():
            geom = feat.geometry()
            if not geom or geom.isEmpty():
                continue

            # Determine asset ID
            asset_id = None
            if id_idx >= 0:
                raw = feat[id_idx]
                if raw is not None and str(raw).strip():
                    asset_id = f"BF_{asset_type.upper()}_{str(raw).strip()}"

            if asset_id is None or asset_id in self._assets:
                asset_id = self._make_id(asset_type)

            # Capacity (total) + already-consumed capacity (spare/occupancy
            # recorded by the survey engineer). Without a used-field the asset
            # starts empty (capacity_used=0) exactly as before.
            capacity = capacity_default
            if cap_idx >= 0:
                try:
                    capacity = int(feat[cap_idx])
                except (ValueError, TypeError):
                    capacity = capacity_default
            capacity = max(1, capacity)

            capacity_used = 0
            if cap_used_idx >= 0:
                try:
                    capacity_used = int(feat[cap_used_idx])
                except (ValueError, TypeError):
                    capacity_used = 0
            capacity_used = max(0, min(capacity_used, capacity))

            # Verification status
            verify_raw = feat[verify_idx] if verify_idx >= 0 else None
            verify_status = VerifyStatus.normalize(verify_raw)

            verify_counts[verify_status] = verify_counts.get(verify_status, 0) + 1

            # USE_MODE: 'survey' = mandatory path (weight=0, forced);
            # anything else or absent = regular brownfield (weight=0.1, preferred).
            use_mode_raw = feat[use_mode_idx] if use_mode_idx >= 0 else None
            use_mode = str(use_mode_raw).strip().lower() if use_mode_raw else ""

            self._assets[asset_id] = {
                "asset_type": asset_type,
                "geom": QgsGeometry(geom),
                "capacity_total": capacity,
                "capacity_used": capacity_used,
                "verify_status": verify_status,
                "use_mode": use_mode,
                "reused": False,
            }
            self._add_to_index(asset_id, geom)
            count += 1

        # Build a concise verify-status breakdown (e.g. "10 Verified, 3 Assumed")
        if verify_counts:
            verify_part = ", ".join(
                f"{n} {s}" for s, n in verify_counts.items()
            )
        else:
            verify_part = "N/A"

        self._report(
            f"Brownfield: loaded {count} {asset_type} features "
            f"(capacity_default={capacity_default}, verify: {verify_part})."
        )
        return count

    # ── Public loaders ───────────────────────────────────────────────────

    def load_ducts(self, layer: QgsVectorLayer,
                   capacity_field: Optional[str] = None,
                   capacity_default: int = 2,
                   capacity_used_field: Optional[str] = None,
                   verify_field: Optional[str] = None,
                   id_field: Optional[str] = None) -> int:
        """Load existing duct lines."""
        return self._load_layer(layer, AssetType.DUCT,
                                capacity_field, capacity_default,
                                capacity_used_field, verify_field, id_field)

    def load_chambers(self, layer: QgsVectorLayer,
                      verify_field: Optional[str] = None,
                      id_field: Optional[str] = None) -> int:
        """Load existing chamber points."""
        return self._load_layer(layer, AssetType.CHAMBER,
                                capacity_default=1, verify_field=verify_field,
                                id_field=id_field)

    def load_poles(self, layer: QgsVectorLayer,
                   verify_field: Optional[str] = None,
                   id_field: Optional[str] = None) -> int:
        """Load existing pole points."""
        return self._load_layer(layer, AssetType.POLE,
                                capacity_default=1, verify_field=verify_field,
                                id_field=id_field)

    def load_fibre(self, layer: QgsVectorLayer,
                   capacity_field: Optional[str] = None,
                   capacity_default: int = 12,
                   capacity_used_field: Optional[str] = None,
                   verify_field: Optional[str] = None,
                   id_field: Optional[str] = None) -> int:
        """Load existing fibre lines."""
        return self._load_layer(layer, AssetType.FIBRE,
                                capacity_field, capacity_default,
                                capacity_used_field, verify_field, id_field)

    def load_cabinets(self, layer: QgsVectorLayer,
                      capacity_field: Optional[str] = None,
                      capacity_default: int = 32,
                      verify_field: Optional[str] = None,
                      id_field: Optional[str] = None) -> int:
        """Load existing cabinet points (including PDP cabinets)."""
        return self._load_layer(layer, AssetType.CABINET,
                                capacity_field, capacity_default,
                                None, verify_field, id_field)

    def load_trenches(self, layer: QgsVectorLayer,
                      verify_field: Optional[str] = None,
                      id_field: Optional[str] = None) -> int:
        """Load existing trench lines (trenches have no capacity limit per se)."""
        return self._load_layer(layer, AssetType.TRENCH,
                                capacity_default=999,  # effectively unlimited
                                verify_field=verify_field,
                                id_field=id_field)

    def load_existing_pdps(self, layer: QgsVectorLayer,
                           verify_field: Optional[str] = None,
                           id_field: Optional[str] = None) -> int:
        """Load existing PDPs as special chamber-type assets."""
        return self._load_layer(layer, AssetType.PDP,
                                capacity_default=1, verify_field=verify_field,
                                id_field=id_field)

    def load_existing_mfgs(self, layer: QgsVectorLayer,
                           verify_field: Optional[str] = None,
                           id_field: Optional[str] = None) -> int:
        """Load existing MFGs as special cabinet-type assets."""
        return self._load_layer(layer, AssetType.MFG,
                                capacity_default=1, verify_field=verify_field,
                                id_field=id_field)

    # ── Queries ──────────────────────────────────────────────────────────

    @property
    def asset_count(self) -> int:
        return len(self._assets)

    @property
    def line_asset_count(self) -> int:
        return sum(1 for a in self._assets.values()
                   if AssetType.is_line(a["asset_type"]))

    @property
    def point_asset_count(self) -> int:
        return sum(1 for a in self._assets.values()
                   if AssetType.is_point(a["asset_type"]))

    def has_assets(self) -> bool:
        return len(self._assets) > 0

    def get_asset(self, asset_id: str) -> Optional[dict]:
        return self._assets.get(asset_id)

    def asset_ids(self) -> List[str]:
        return list(self._assets.keys())

    def assets_by_type(self, asset_type: str) -> List[str]:
        return [aid for aid, a in self._assets.items()
                if a["asset_type"] == asset_type]

    # ── Spatial queries ──────────────────────────────────────────────────

    def find_nearest_point_asset(self, pt: QgsPointXY, max_dist: float,
                                 asset_types: Optional[Set[str]] = None,
                                 require_capacity: bool = False) -> Optional[Tuple[str, float, QgsGeometry]]:
        """
        Find the nearest point-type asset (chamber/pole/cabinet/PDP) to a point.

        Returns:
            (asset_id, distance, geometry) or None
        """
        if not self._spatial_index:
            return None

        if asset_types is None:
            asset_types = AssetType.ALL_POINT_TYPES

        search_rect = QgsGeometry.fromPointXY(pt).buffer(max_dist, 8).boundingBox()
        nearby_fids = self._spatial_index.intersects(search_rect)

        best_id, best_dist, best_geom = None, float("inf"), None
        for fid in nearby_fids:
            aid = self._fid_to_asset.get(fid)
            if not aid:
                continue
            a = self._assets.get(aid)
            if not a:
                continue
            if not AssetType.is_point(a["asset_type"]):
                continue
            if a["asset_type"] not in asset_types:
                continue
            if require_capacity and not self.has_capacity(aid):
                continue
            dist = a["geom"].distance(QgsGeometry.fromPointXY(pt))
            if dist < best_dist and dist <= max_dist:
                best_id, best_dist, best_geom = aid, dist, a["geom"]

        if best_id:
            return (best_id, best_dist, best_geom)
        return None

    def find_nearby_chambers(self, pt: QgsPointXY, max_dist: float) -> List[Tuple[str, float]]:
        """Find existing chambers near a point, sorted by distance."""
        result = self.find_nearest_point_asset(
            pt, max_dist,
            asset_types={AssetType.CHAMBER, AssetType.PDP}
        )
        if result:
            return [(result[0], result[1])]
        return []

    # ── Capacity management ──────────────────────────────────────────────

    def has_capacity(self, asset_id: str) -> bool:
        """Check if an asset has spare capacity."""
        a = self._assets.get(asset_id)
        if not a:
            return False
        return a["capacity_used"] < a["capacity_total"]

    def remaining_capacity(self, asset_id: str) -> int:
        """Get remaining capacity for an asset."""
        a = self._assets.get(asset_id)
        if not a:
            return 0
        return max(0, a["capacity_total"] - a["capacity_used"])

    def consume_capacity(self, asset_id: str, amount: int = 1) -> bool:
        """
        Try to consume capacity from an asset.
        Returns True if successful, False if insufficient capacity.
        """
        a = self._assets.get(asset_id)
        if not a:
            return False
        if a["capacity_used"] + amount > a["capacity_total"]:
            return False
        a["capacity_used"] += amount
        a["reused"] = True
        self._used_assets.add(asset_id)
        return True

    # ── Graph edge generation ────────────────────────────────────────────

    def iter_graph_edges(self, step_m: float = 5.0,
                         base_weight: float = 0.1) -> Iterator[BrownfieldEdge]:
        """
        Yield BrownfieldEdge objects for all line-type assets,
        densified by step_m for graph insertion.

        Two-tier weight model:
        - **use_mode='survey'** → weight=0 (mandatory: the engineer's approved
          path is the field truth; the routing algorithm MUST follow it)
        - **regular brownfield** → weight=base_weight (preferred: existing
          infrastructure is available but the algorithm may find a better path)

        Args:
            step_m: Densification distance in meters
            base_weight: Base edge weight for regular brownfield (0.1, preferred;
                         new construction = 1.0)
        """
        for asset_id, a in self._assets.items():
            if not AssetType.is_line(a["asset_type"]):
                continue
            if a["capacity_used"] >= a["capacity_total"]:
                continue  # skip fully consumed assets

            geom = a["geom"]
            if not geom or geom.isEmpty():
                continue

            densified = geom.densifyByDistance(step_m)
            try:
                lines = densified.asMultiPolyline() if densified.isMultipart() else [densified.asPolyline()]
            except Exception:
                continue

            # ── Two-tier weight: survey = mandatory (0), brownfield = preferred ──
            is_survey = a.get("use_mode") == "survey"
            if is_survey:
                weight = 0.0  # forced: Dijkstra always picks this
            else:
                weight = base_weight
                # Scale weight by capacity utilisation: fuller assets are slightly
                # less attractive (but still preferred over new construction)
                if a["capacity_total"] > 0:
                    util = a["capacity_used"] / a["capacity_total"]
                    weight = base_weight + util * 0.3  # range: 0.1 - 0.4 (still << 1.0)

            for ln in lines:
                for i in range(len(ln) - 1):
                    p1, p2 = QgsPointXY(ln[i]), QgsPointXY(ln[i + 1])
                    u = (p1.x(), p1.y())
                    v = (p2.x(), p2.y())
                    if u == v:
                        continue
                    yield BrownfieldEdge(
                        u=u, v=v, weight=weight, asset_id=asset_id,
                        infra_type=a["asset_type"], geom=geom,
                        capacity_total=a["capacity_total"],
                        capacity_used=a["capacity_used"],
                        verify_status=a["verify_status"],
                        use_mode=a.get("use_mode", ""),
                    )

    @property
    def survey_asset_count(self) -> int:
        """Count of line assets marked as mandatory survey paths."""
        return sum(1 for a in self._assets.values()
                   if AssetType.is_line(a["asset_type"]) and a.get("use_mode") == "survey")

    @property
    def brownfield_asset_count(self) -> int:
        """Count of line assets that are regular (preferred) brownfield."""
        return sum(1 for a in self._assets.values()
                   if AssetType.is_line(a["asset_type"]) and a.get("use_mode") != "survey")

    # ── Classification ───────────────────────────────────────────────────

    def is_reused(self, asset_id: str) -> bool:
        """Check if an asset was consumed during routing."""
        return asset_id in self._used_assets

    def classify_asset(self, asset_id: str) -> str:
        """
        Classify an asset's infrastructure status:
        - 'Reused' if capacity was consumed during routing
        - 'Existing' if loaded but not used
        - 'Proposed' is used for new (non-brownfield) features
        - 'Removed' for assets flagged for decommissioning
        """
        a = self._assets.get(asset_id)
        if not a:
            return InfraStatus.PROPOSED
        if a.get("decommission", False):
            return InfraStatus.REMOVED
        if self.is_reused(asset_id):
            return InfraStatus.REUSED
        return InfraStatus.EXISTING

    def classify_edge(self, asset_id: Optional[str]) -> Tuple[str, str]:
        """
        Classify a graph edge. Returns (infra_status, verify_status).

        If asset_id is None, the edge is new construction (Proposed / Verified).
        """
        if asset_id is None:
            return (InfraStatus.PROPOSED, VerifyStatus.VERIFIED)

        a = self._assets.get(asset_id)
        if not a:
            return (InfraStatus.PROPOSED, VerifyStatus.VERIFIED)

        return (self.classify_asset(asset_id), a["verify_status"])

    # ── Downstream stage helpers ───────────────────────────────────────

    @staticmethod
    def store_registry(registry: Optional['BrownfieldRegistry'],
                       enabled: bool = True) -> bool:
        """Persist the registry and its downstream-reuse flag (process scope).

        Returns True on success.  When 'enabled' is False the registry stays
        stored (so the Existing Infrastructure map layers are still produced)
        but every downstream consumer treats it as unused.
        """
        global _PROJECT_REGISTRY, _PROJECT_REUSE_ENABLED
        _PROJECT_REGISTRY = registry
        _PROJECT_REUSE_ENABLED = bool(enabled)
        return True

    @staticmethod
    def set_reuse_enabled(enabled: bool) -> None:
        """Flip the downstream-reuse gate without clearing the registry."""
        global _PROJECT_REUSE_ENABLED
        _PROJECT_REUSE_ENABLED = bool(enabled)

    @staticmethod
    def load_from_project() -> Optional['BrownfieldRegistry']:
        """Retrieve the registry if stored and reuse is enabled (process scope).

        Downstream stages use this as their single gate: a registry stored with
        reuse disabled (USE_BROWNFIELD toggle off) returns None, so routing,
        PDP snapping and classification all ignore it.
        """
        if not _PROJECT_REUSE_ENABLED:
            return None
        reg = _PROJECT_REGISTRY
        if reg is not None and reg.has_assets():
            return reg
        return None

    def inject_edges_into_graph(self, G, dens: float, eps: float,
                                 _qkey, base_weight: float = 0.1,
                                 connect_tol: float = 5.0) -> int:
        """
        Inject brownfield duct/trench edges into a NetworkX routing graph.

        Used by trench_layer.py during graph construction.
        'connect_tol' (> 0) bridges brownfield corridor endpoints to the
        nearest pre-existing graph node within that many metres, so corridors
        offset from the road network (survey GPS error, drawing offsets) are
        still reused during routing.

        Two-tier model:
        - survey (USE_MODE=survey): weight=0, mandatory — algorithm MUST follow
        - brownfield (default): weight=0.1, preferred — algorithm may choose

        Returns number of edges added.
        """
        from ..utils.graph_ops import add_brownfield_edges_to_graph as _add_bf_edges
        return _add_bf_edges(G, self, dens, eps, _qkey, base_weight=base_weight,
                             connect_tol=connect_tol)

    def try_snap_pdp_to_chamber(self, pdp_geom, max_dist: float = 10.0
                                ) -> Tuple[Any, str]:
        """
        Try to snap a PDP geometry to the nearest existing chamber/PDP.

        Used by network_layer.py during PDP placement.
        Returns (geometry, src_id) — if no nearby chamber found,
        returns the original geometry and empty src_id.
        """
        if pdp_geom is None:
            return pdp_geom, ""
        try:
            pdp_pt = pdp_geom.asPoint()
        except Exception:
            return pdp_geom, ""
        nearby = self.find_nearby_chambers(pdp_pt, max_dist)
        if nearby:
            aid, dist = nearby[0]
            a = self.get_asset(aid)
            if a:
                from qgis.core import QgsGeometry
                return QgsGeometry(a["geom"]), aid
        return pdp_geom, ""

    # ── Summary ──────────────────────────────────────────────────────────

    def summary(self) -> str:
        lines = [
            f"BrownfieldRegistry: {self.asset_count} assets "
            f"({self.line_asset_count} lines, {self.point_asset_count} points)",
            f"  survey (mandatory, weight=0): {self.survey_asset_count} lines",
            f"  brownfield (preferred, weight=0.1): {self.brownfield_asset_count} lines",
        ]
        for asset_type in [AssetType.DUCT, AssetType.TRENCH, AssetType.FIBRE,
                           AssetType.CHAMBER, AssetType.POLE, AssetType.CABINET,
                           AssetType.PDP, AssetType.MFG]:
            ids = self.assets_by_type(asset_type)
            if ids:
                reused = sum(1 for aid in ids if self.is_reused(aid))
                lines.append(f"  {asset_type}: {len(ids)} loaded, {reused} reused")

        return "\n".join(lines)
