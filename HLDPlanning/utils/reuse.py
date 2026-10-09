# -*- coding: utf-8 -*-
"""Shared BROWN FIELD *reuse* classification for line features.

One rule, every consumer (trench designer, consolidated trench layer, duct
layer): a run counts as REUSED when it FOLLOWS an existing duct/trench asset
for at least ``REUSE_FOLLOW_MIN`` of its length AND that asset still has spare
capacity — at which point that capacity is consumed.

Proximity alone is not reuse. A trench brushing past an existing duct, or
running parallel to a full one, is not riding it: treating those as reuse once
classified ~96 % of a Berlin run as reuse and wrote the whole trench BOQ off as
nothing to build. This module is the single implementation of the stricter
rule so the designer, the consolidation pass and the duct layer cannot drift
apart.

Typical use:

    index = BrownfieldLineIndex.build()          # None when brownfield is off
    for row in rows:
        result = index.classify_run([row["_geom"]])
        stamp_row(row, result, proposed_status="New")
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from qgis.core import QgsFeature, QgsGeometry, QgsSpatialIndex

# Asset types that a new line can physically ride.
LINE_ASSET_TYPES: Tuple[str, ...] = ("duct", "trench", "fibre")

# Corridor tolerance: how far off the asset a run may sit and still be the
# same route (survey/GPS error, drawing offset).
REUSE_TOL_M: float = 2.0
# Fraction of a run's length that must ride the asset before it counts.
REUSE_FOLLOW_MIN: float = 0.5
# A run this close to its full length counts as wholly reused.
WHOLLY_REUSED_SLACK_M: float = 0.5

REUSE_LEN_FIELD = "REUSE_LEN_M"
REUSE_SOURCE_FIELD = "REUSE_SOURCE"
INFRA_FIELD = "INFRA_STATUS"
VERIFY_FIELD = "VERIFY_STATUS"

STATUS_REUSED = "Reused"
STATUS_MIXED = "Mixed"


class ReuseResult:
    """What a run (one geometry or several parts) rides."""

    __slots__ = ("reuse_len", "group_len", "sources", "verify_status", "status", "matched")

    def __init__(self, reuse_len: float, group_len: float, sources: List[str],
                 verify_status: Optional[str]) -> None:
        self.reuse_len = float(reuse_len)
        self.group_len = float(group_len)
        self.sources = list(sources)
        self.verify_status = verify_status
        self.matched = bool(sources)
        if not self.matched:
            self.status = None
        elif group_len > 0 and self.reuse_len >= group_len - WHOLLY_REUSED_SLACK_M:
            self.status = STATUS_REUSED
        else:
            self.status = STATUS_MIXED

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "ReuseResult(status=%s, reuse_len=%.2f, group_len=%.2f, sources=%s)" % (
            self.status, self.reuse_len, self.group_len, self.sources)


class BrownfieldLineIndex:
    """Spatial index over the registry's line assets, with capacity accounting.

    Built once per stage. ``classify_run`` consumes capacity on the assets a
    run actually rides, so a duct full by the end of the design cannot be
    reused again by the next feature.
    """

    def __init__(self, registry, line_types: Sequence[str] = LINE_ASSET_TYPES) -> None:
        self.registry = registry
        self.geoms: Dict[int, Tuple[str, dict, QgsGeometry]] = {}
        self.index = QgsSpatialIndex()
        fid = 0
        for aid in registry.asset_ids():
            a = registry.get_asset(aid)
            if not a or a.get("asset_type") not in line_types:
                continue
            g = a.get("geom")
            if not g or g.isEmpty():
                continue
            self.geoms[fid] = (aid, a, g)
            feat = QgsFeature(fid)
            feat.setGeometry(g)
            self.index.addFeature(feat)
            fid += 1

    # ── construction ─────────────────────────────────────────────────────

    @classmethod
    def build(cls, line_types: Sequence[str] = LINE_ASSET_TYPES
              ) -> Optional["BrownfieldLineIndex"]:
        """Index the active registry, or None when there is nothing to reuse.

        Uses the toggle-aware loader, so ``USE_BROWNFIELD=false`` disables
        reuse everywhere instead of leaking a registry from another run.
        """
        try:
            from .brownfield import BrownfieldRegistry
            reg = BrownfieldRegistry.load_from_project()
        except Exception:
            return None
        if reg is None or not reg.has_assets():
            return None
        idx = cls(reg, line_types)
        return idx if idx.geoms else None

    def __len__(self) -> int:
        return len(self.geoms)

    # ── classification ───────────────────────────────────────────────────

    @staticmethod
    def _parts(geom: Optional[QgsGeometry]) -> List[QgsGeometry]:
        """Split a (possibly multi-part) geometry into its runs.

        A branching trench is legitimately multi-part; each part is its own run
        and is classified on its own, exactly as the consolidated trench layer
        does — otherwise one long part can carry the whole feature's ratio.
        """
        if geom is None or geom.isEmpty():
            return []
        if geom.isMultipart():
            try:
                parts = [QgsGeometry.fromPolylineXY(line)
                         for line in geom.asMultiPolyline() if len(line) > 1]
                if parts:
                    return parts
            except Exception:
                pass
        return [geom]

    def _commit(self, asset_id: str) -> None:
        """Commit the asset's capacity once, whatever the registry supports."""
        try:
            self.registry.commit_capacity(asset_id)
        except AttributeError:
            try:
                self.registry.consume_capacity(asset_id)
            except Exception:
                pass
        except Exception:
            pass

    def _best_follow(self, part: QgsGeometry, part_len: float
                     ) -> Optional[Tuple[float, str, Optional[str]]]:
        """Best (followed_len, asset_id, verify_status) for ONE geometry part."""
        best: Optional[Tuple[float, str, Optional[str]]] = None
        try:
            bb = part.buffer(REUSE_TOL_M, 8).boundingBox()
            candidates = self.index.intersects(bb)
        except Exception:
            return None
        for bf_fid in candidates:
            entry = self.geoms.get(bf_fid)
            if entry is None:
                continue
            aid, asset, bg = entry
            try:
                if not self.registry.has_capacity(aid):
                    # A full duct cannot be reused.
                    continue
            except Exception:
                pass
            try:
                if part.distance(bg) > REUSE_TOL_M:
                    continue
                follow = float(part.intersection(bg.buffer(REUSE_TOL_M, 8)).length())
            except Exception:
                continue
            if best is None or follow > best[0]:
                best = (follow, aid, asset.get("verify_status"))
        if best is None:
            return None
        follow, aid, verify = best
        if part_len <= 0 or follow / part_len < REUSE_FOLLOW_MIN:
            return None
        return (follow, aid, verify)

    def classify_run(self, parts: Sequence[Optional[QgsGeometry]]
                     ) -> ReuseResult:
        """Classify a run made of one or more parts; commits capacity once."""
        flat: List[QgsGeometry] = []
        for geom in parts:
            flat.extend(self._parts(geom))

        group_len = 0.0
        for part in flat:
            try:
                group_len += float(part.length())
            except Exception:
                pass

        reuse_len = 0.0
        sources: List[str] = []
        verify_status: Optional[str] = None
        for part in flat:
            try:
                part_len = float(part.length())
            except Exception:
                continue
            if part_len <= 0:
                continue
            hit = self._best_follow(part, part_len)
            if hit is None:
                continue
            follow, aid, verify = hit
            reuse_len += follow
            if aid not in sources:
                sources.append(aid)
            if verify_status is None:
                verify_status = verify
            self._commit(aid)

        return ReuseResult(reuse_len, group_len, sources, verify_status)

    def classify_geometry(self, geom: Optional[QgsGeometry]) -> ReuseResult:
        """Convenience wrapper for a single-geometry feature."""
        return self.classify_run([geom])


# ── stamping helpers ─────────────────────────────────────────────────────


def stamp_row(row: Dict[str, Any], result: ReuseResult,
              proposed_status: str = "Proposed") -> None:
    """Stamp a designer/consolidation row dict with its reuse."""
    row[REUSE_LEN_FIELD] = round(result.reuse_len, 2)
    if not result.matched:
        return
    row[REUSE_SOURCE_FIELD] = ",".join(result.sources)
    if result.verify_status:
        row[VERIFY_FIELD] = result.verify_status
    row[INFRA_FIELD] = result.status or proposed_status


def stamp_feature(feat: QgsFeature, field_index: Dict[str, int],
                  result: ReuseResult, proposed_status: str = "Proposed") -> None:
    """Stamp a QgsFeature using a {field name: index} map (missing keys skipped)."""
    idx = field_index.get(REUSE_LEN_FIELD)
    if idx is not None and idx >= 0:
        feat[idx] = round(result.reuse_len, 2)
    if not result.matched:
        return
    idx = field_index.get(REUSE_SOURCE_FIELD)
    if idx is not None and idx >= 0:
        feat[idx] = ",".join(result.sources)
    idx = field_index.get(VERIFY_FIELD)
    if idx is not None and idx >= 0 and result.verify_status:
        feat[idx] = result.verify_status
    idx = field_index.get(INFRA_FIELD)
    if idx is not None and idx >= 0:
        feat[idx] = result.status or proposed_status


def field_index(fields) -> Dict[str, int]:
    """{upper-case field name: index} for a QgsFields/QgsFeature fields object."""
    out: Dict[str, int] = {}
    for i, f in enumerate(fields):
        out[f.name().upper()] = i
    return out
