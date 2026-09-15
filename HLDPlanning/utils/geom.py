# -*- coding: utf-8 -*-
"""
utils.geom
Common geometry helpers for FTTH layers (Trench, Duct, Cable, etc.)
"""

import math
from collections import defaultdict

from qgis.core import QgsGeometry, QgsPointXY

def round_key_xy(x: float, y: float, tol: float = 0.5):
    """Rounds coordinates to a tolerance grid (used for node keys)."""
    rx = round(x / tol) * tol
    ry = round(y / tol) * tol
    return (round(rx, 6), round(ry, 6))


def geom_substring(geom: QgsGeometry, start: float, end: float) -> QgsGeometry:
    """Returns a portion of a line geometry between distances start and end (meters)."""
    if not geom or geom.isEmpty():
        return QgsGeometry()

    if hasattr(geom, "lineSubstring"):
        try:
            return geom.lineSubstring(start, end)
        except Exception:
            pass
    if hasattr(geom, "curveSubstring"):
        try:
            return geom.curveSubstring(start, end)
        except Exception:
            pass

    pl = geom.asPolyline()
    if not pl:
        m = geom.asMultiPolyline()
        if m and m[0]:
            pl = m[0]
        else:
            return QgsGeometry()

    cum = [0.0]
    for i in range(1, len(pl)):
        dx = pl[i].x() - pl[i - 1].x()
        dy = pl[i].y() - pl[i - 1].y()
        cum.append(cum[-1] + math.hypot(dx, dy))

    total = cum[-1]
    if end <= 0 or start >= total or start >= end:
        return QgsGeometry()

    def point_at(dist):
        if dist <= 0:
            return QgsPointXY(pl[0].x(), pl[0].y())
        if dist >= total:
            return QgsPointXY(pl[-1].x(), pl[-1].y())
        for i in range(1, len(pl)):
            if dist <= cum[i]:
                seg_len = (cum[i] - cum[i - 1]) or 1.0
                t = (dist - cum[i - 1]) / seg_len
                x = pl[i - 1].x() + (pl[i].x() - pl[i - 1].x()) * t
                y = pl[i - 1].y() + (pl[i].y() - pl[i - 1].y()) * t
                return QgsPointXY(x, y)
        return QgsPointXY(pl[-1].x(), pl[-1].y())

    start_pt = point_at(max(0.0, start))
    end_pt = point_at(min(total, end))
    pts = [start_pt]
    for i in range(1, len(pl)):
        if cum[i] > start and cum[i] < end:
            pts.append(QgsPointXY(pl[i].x(), pl[i].y()))
    pts.append(end_pt)
    return QgsGeometry.fromPolylineXY(pts)


def edges_to_geom(edge_geom: dict, seg_list: list):
    """Merge segment geometries to a MultiLineString."""
    parts = []
    for sid in seg_list:
        g = edge_geom.get(sid)
        if g:
            parts.append(g.asPolyline())
    return QgsGeometry.fromMultiPolylineXY(parts) if parts else QgsGeometry()


def merge_contiguous_runs(geom, grid=0.01):
    """Collapse a fragment MultiLineString into continuous line runs.

    ``edges_to_geom`` emits one geometry part per graph edge, so a routed
    cable or trench arrives as thousands of tiny stubs (a ~4 km feeder tree
    used to be a single feature made of ~2,415 disjoint 1.7 m pieces; clubbed
    trenches ran to ~170 parts each).  They render and select as thousands of
    disconnected lines instead of continuous corridors.

    ``QgsGeometry.mergeLines`` joins every part that shares an endpoint,
    leaving a MultiLineString whose parts are the longest possible continuous
    runs.  A branching route cannot become a single LineString, so the result
    stays multi-part on purpose — but each part is now a real run.

    ``grid`` first snaps vertices to a lattice (metres).  Graph edges are
    built by interpolating along parent lines, so two edges meeting at one
    shared node can differ by sub-micrometre float noise — enough for the
    merge to see them as disjoint and leave the route fragmented (measured:
    1,631 parts collapsed to 58 at 1 mm, to 6 at 1 cm).  1 cm is a thirtieth
    of a 300 mm trench width, so nothing moves at planning scale.  Pass
    ``grid=0`` to merge on the raw vertices.

    Returns the input unchanged when the merge is unavailable or yields
    nothing, so callers never lose geometry.
    """
    if geom is None or geom.isEmpty():
        return geom
    src = geom
    if grid:
        try:
            snapped = geom.snappedToGrid(grid, grid)
            if snapped is not None and not snapped.isEmpty():
                src = snapped
        except Exception:
            src = geom
    try:
        merged = src.mergeLines()
    except Exception:
        return geom
    if merged is None or merged.isEmpty():
        return geom
    return merged


def chain_geometry_pieces(pieces, tol=0.05):
    """Assemble disjoint line pieces into maximal continuous runs.

    Why this exists: a consolidated route is produced by unioning many raw
    trench geometries, and ``unaryUnion`` nodes a line network at EVERY
    crossing.  That leaves one route as dozens of 1-10 m pieces meeting at
    shared endpoints.  ``mergeLines`` only joins along a simple chain and
    gives up wherever three pieces meet, so it leaves the route as confetti
    (~170 parts for a 645 m trench, measured).

    This walks the pieces' shared endpoints instead and concatenates them
    end to end, emitting each junction vertex once — exactly what a linemerge
    would do if it handled branching.  A branching network cannot be a single
    polyline in any case, so the result is one run per maximal continuous
    path: a route crossing another stays continuous through the crossing
    instead of being chopped there.

    Returns a QgsGeometry (LineString / MultiLineString), or None when nothing
    usable was supplied.
    """
    polys = []
    for g in pieces or ():
        if g is None or g.isEmpty():
            continue
        try:
            if g.isMultipart():
                for pl in g.asMultiPolyline():
                    if len(pl) >= 2:
                        polys.append(list(pl))
            else:
                pl = g.asPolyline()
                if len(pl) >= 2:
                    polys.append(list(pl))
        except Exception:
            continue
    if not polys:
        return None

    def nk(p):
        return round_key_xy(p.x(), p.y(), tol)

    n = len(polys)
    used = [False] * n
    ends = defaultdict(list)
    for i, pl in enumerate(polys):
        ends[nk(pl[0])].append(i)
        ends[nk(pl[-1])].append(i)

    def walk(start):
        run = list(polys[start])
        used[start] = True
        while True:
            tail_k = nk(run[-1])
            nxt = None
            for j in ends.get(tail_k, ()):
                if used[j]:
                    continue
                pl = polys[j]
                if nk(pl[0]) == tail_k:
                    nxt = list(pl)
                elif nk(pl[-1]) == tail_k:
                    nxt = list(reversed(pl))
                if nxt is not None:
                    used[j] = True
                    break
            if nxt is None:
                break
            # The shared junction vertex is already the last point of `run`.
            run.extend(nxt[1:] if nk(nxt[0]) == nk(run[-1]) else nxt)
        return run

    runs = []
    # Start from true extremities first so runs follow the street outwards.
    for idxs in ends.values():
        if len(idxs) == 1 and not used[idxs[0]]:
            runs.append(walk(idxs[0]))
    # Then any leftovers (closed loops / isolated cycles).
    for i in range(n):
        if not used[i]:
            runs.append(walk(i))

    runs = [r for r in runs if len(r) >= 2]
    if not runs:
        return None
    if len(runs) == 1:
        return QgsGeometry.fromPolylineXY(runs[0])
    return QgsGeometry.fromMultiPolylineXY(runs)


def path_len(edge_len: dict, seg_list: list) -> float:
    """Sum length of all segments in a path."""
    return sum(edge_len.get(s, 0.0) for s in seg_list)


def is_prefix(a: list, b: list) -> bool:
    """Return True if path a is prefix of path b."""
    return len(a) <= len(b) and a == b[: len(a)]


def lcp_len(seq_list: list) -> int:
    """Find longest common prefix length among list of paths."""
    if not seq_list:
        return 0
    m = min(len(s) for s in seq_list)
    k = 0
    for i in range(m):
        seg0 = seq_list[0][i]
        if any(s[i] != seg0 for s in seq_list[1:]):
            break
        k += 1
    return k
