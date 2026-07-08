# -*- coding: utf-8 -*-
"""
Splitter sizing for a PDP / polygon serving area.

A PDP serves `hh` homes. Optical splitters come in fixed sizes (1:8 … 1:64).
`plan_splitters` picks the cheapest *set* of splitters (a mix of sizes, not just
N of one size) whose output ports cover all homes with sensible headroom, and
enumerates every splitter used so it can be written to the attribute table.
"""
import math

# Catalogue of splitter output ratios (1:8 … 1:64) and the utilisation window.
SPLITTER_SIZES = [8, 16, 32, 64]
SPLIT_UTIL_MIN = 60.0     # below this a splitter is wastefully empty
SPLIT_UTIL_MAX = 90.0     # above this there is no spare capacity


def plan_splitters(hh, sizes=SPLITTER_SIZES, util_min=SPLIT_UTIL_MIN, util_max=SPLIT_UTIL_MAX):
    """
    Decompose `hh` homes into the fewest splitters from `sizes` whose total
    output ports cover all homes with headroom (utilisation <= util_max), then
    among those pick the tightest fit (fewest wasted ports).

    Returns a dict:
      counts  {size: n}      splitters actually used, e.g. {64: 2, 16: 1}
      total   int            number of splitters
      ports   int            total output ports
      util    float          homes / ports * 100 (one decimal)
      ok      int            1 if util_min <= util <= util_max else 0
      primary int            largest size used (0 if none)
      label   str            "2x1:64 + 1x1:16"
    """
    empty = {"counts": {}, "total": 0, "ports": 0, "util": 0.0, "ok": 0, "primary": 0, "label": "-"}
    try:
        hh = int(hh)
    except (TypeError, ValueError):
        return dict(empty)
    if hh <= 0:
        return dict(empty)

    sizes = sorted(int(s) for s in sizes if int(s) > 0) or [64]
    # Ports must cover the homes and leave headroom (utilisation <= util_max).
    lo = max(hh, int(math.ceil(hh / (util_max / 100.0))), sizes[0])
    # Upper bound where utilisation is still >= util_min (not too empty), plus margin.
    hi = max(lo + sizes[-1], int(hh / (util_min / 100.0)) + sizes[-1])

    INF = 10 ** 9
    cnt = [INF] * (hi + 1)
    pick = [-1] * (hi + 1)
    cnt[0] = 0
    for p in range(1, hi + 1):
        for s in sizes:
            if p >= s and cnt[p - s] + 1 < cnt[p]:
                cnt[p] = cnt[p - s] + 1
                pick[p] = s

    # Rank reachable port totals >= lo: prefer utilisation inside [min, max],
    # then fewest splitters, then tightest fit (fewest ports).
    best = None
    for P in range(lo, hi + 1):
        if cnt[P] >= INF:
            continue
        util = 100.0 * hh / P
        in_band = 0 if (util_min <= util <= util_max) else 1   # 0 sorts first
        key = (in_band, cnt[P], P)
        if best is None or key < best[0]:
            best = (key, P)

    if best is None:
        # Unreachable target (shouldn't happen with an 8-port unit): stack the largest size.
        big = sizes[-1]
        n = int(math.ceil(hh / float(big)))
        counts = {big: n}
    else:
        counts = {}
        p = best[1]
        while p > 0 and pick[p] != -1:
            s = pick[p]
            counts[s] = counts.get(s, 0) + 1
            p -= s

    ports = sum(s * n for s, n in counts.items())
    total = sum(counts.values())
    util = round(100.0 * hh / ports, 1) if ports else 0.0
    ok = 1 if (util_min <= util <= util_max) else 0
    primary = max(counts) if counts else 0
    label = " + ".join(f"{counts[s]}x1:{s}" for s in sorted(counts, reverse=True)) or "-"
    return {"counts": counts, "total": total, "ports": ports,
            "util": util, "ok": ok, "primary": primary, "label": label}


def recommend_splitter(hh):
    """Backward-compatible summary: (primary_size_label, total_splitters, util, ok)."""
    p = plan_splitters(hh)
    size = f"1:{p['primary']}" if p["primary"] else "-"
    return size, p["total"], p["util"], p["ok"]
