"""CBT-area subdivision: split one PDP area into civil-structure (CBT) sub-areas.

A PDP area holds up to 64 homes; a CBT serves about 16. This module groups the
premises of one PDP area into CBT groups. It has no QGIS imports so it can be
tested directly.

Method: project the premises onto their principal axis (the street direction
when the area is one long street, the wider direction otherwise), sort along
it, and fill groups in order up to the cap. Consecutive premises along the
axis are neighbours, so each group is a contiguous strip of the area.

A single premises whose homes exceed the cap becomes its own group: the cap is
a target, because a building cannot be split across two CBTs.
"""

import math


def _principal_axis(points):
    """Unit vector of the largest spread of (x, y) points (PCA, 2-D)."""
    n = len(points)
    if n < 2:
        return (1.0, 0.0)
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    syy = sum((p[1] - my) ** 2 for p in points)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    # Angle of the major eigenvector of [[sxx, sxy], [sxy, syy]].
    theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
    return (math.cos(theta), math.sin(theta))


def split_into_cbt_groups(members, cap=16):
    """Group premises into CBT sub-areas of at most `cap` homes each.

    members: iterable of (key, x, y, hh). `hh` is the homes on that premises.
    Returns a list of groups; each group is a list of keys. Groups are ordered
    along the principal axis and every key appears in exactly one group.
    """
    cap = int(cap)
    if cap < 1:
        raise ValueError("cap must be >= 1")
    rows = [(k, float(x), float(y), max(0, int(hh))) for k, x, y, hh in members]
    if not rows:
        return []
    total = sum(r[3] for r in rows)
    if total <= cap:
        return [[r[0] for r in rows]]

    ux, uy = _principal_axis([(r[1], r[2]) for r in rows])
    # Sort by projection; key breaks ties so the result is deterministic.
    rows.sort(key=lambda r: (r[1] * ux + r[2] * uy, str(r[0])))

    groups = []
    current, load = [], 0
    for key, _x, _y, hh in rows:
        if current and load + hh > cap:
            groups.append(current)
            current, load = [], 0
        current.append(key)
        load += hh
    if current:
        groups.append(current)
    return groups
