# -*- coding: utf-8 -*-
"""
Splitter sizing for a PDP / polygon serving area.

A PDP serves `hh` homes. Optical splitters come in fixed sizes (1:8 … 1:64).
`plan_splitters` picks a standardised set of splitters (2-3 combos wherever
possible) whose output ports cover all homes with sensible headroom, and
enumerates every splitter used so it can be written to the attribute table.

Standardisation policy
----------------------
1. Primary: minimise the number of splitters (fewest physical devices).
2. Secondary: minimise the number of *distinct* sizes in the plan. A plan of
   2 × 1:64 is preferred over 1 × 1:64 + 1 × 1:32 + 1 × 1:16 + 1 × 1:8,
   even though both cover the homes, because the former uses only one size.
3. Tertiary: minimise *unused ports* (total ports − homes) so an extra
   small demand adds the smallest adequate splitter — e.g. growing a
   2 × 1:64 area by 10 HP adds a 1:32, not another wasteful 1:64.
4. Quaternary: prefer the fewest *distinct* sizes in the plan (2-3 combos).
5. Last: prefer utilisation closest to the middle of the 60-90 % band so
   the plan is neither wasteful nor oversubscribed.

Catalogue of splitter output ratios (1:8 … 1:64) and the utilisation window.
"""
import math

SPLITTER_SIZES = [8, 16, 32, 64]
SPLIT_UTIL_MIN = 60.0     # below this a splitter is wastefully empty
SPLIT_UTIL_MAX = 90.0     # above this there is no spare capacity


def _decompose(p, sizes, pick):
    """Reconstruct the splitter multiset for a target port count `p`.

    Traces back through the DP ``pick[]`` table.  ``pick[x]`` is the size of the
    *last* splitter added on the optimal (fewest-count) path to exactly ``x``
    ports, or ``-1`` if ``x`` is unreachable.  Starting from ``p``, we repeatedly
    peel off ``pick[remaining]`` until we hit 0.

    When ``p`` is reachable the trace is deterministic and always consumes exactly
    ``p`` ports.  ``pick[]`` is built by the DP in :func:`plan_splitters` so the
    reconstruction reproduces one optimal-count plan.
    """
    counts = {}
    remaining = p
    while remaining > 0 and pick[remaining] != -1:
        s = pick[remaining]
        if s not in sizes or s > remaining:
            # Should not happen for a reachable ``p`` built by the DP — bail out
            # before looping forever.
            break
        counts[s] = counts.get(s, 0) + 1
        remaining -= s
    if remaining > 0:
        # DP said ``p`` is reachable but the trace didn't consume it entirely.
        # Fill the gap with the smallest size so the returned counts still sum to
        # ``p`` and the caller doesn't see a half-baked plan.
        for s in sorted(sizes):
            while remaining >= s:
                counts[s] = counts.get(s, 0) + 1
                remaining -= s
    return counts


def plan_splitters(hh, sizes=SPLITTER_SIZES, util_min=SPLIT_UTIL_MIN, util_max=SPLIT_UTIL_MAX):
    """
    Decompose `hh` homes into a standardised set of splitters from `sizes`
    whose total output ports cover all homes with headroom (utilisation <=
    util_max),    preferring:

      1. fewest splitters,
      2. fewest unused ports (reduce stranded capacity),
      3. fewest distinct sizes (2-3 combos wherever possible),
      4. utilisation closest to the middle of the 60-90 % band.

    Returns a dict:
      counts  {size: n}      splitters actually used, e.g. {64: 2}
      total   int            number of splitters
      ports   int            total output ports
      util    float          homes / ports * 100 (one decimal)
      ok      int            1 if util_min <= util <= util_max else 0
      primary int            largest size used (0 if none)
      label   str            "2x1:64"
    """
    empty = {"counts": {}, "total": 0, "ports": 0, "util": 0.0, "ok": 0,
             "primary": 0, "label": "-"}
    try:
        hh = int(hh)
    except (TypeError, ValueError):
        return dict(empty)
    if hh <= 0:
        return dict(empty)

    sizes = sorted(int(s) for s in sizes if int(s) > 0) or [64]

    # Smallest port total that covers the homes with utilisation <= util_max.
    lo = max(hh, int(math.ceil(hh / (util_max / 100.0))), sizes[0])
    # Largest port total that still has utilisation >= util_min, plus one extra
    # catalogue step so we don't miss a cleaner 2-splits plan just above lo.
    hi = max(lo + sizes[-1], int(hh / (util_min / 100.0)) + sizes[-1])

    INF = 10 ** 9
    cnt = [INF] * (hi + 1)     # cnt[p] = fewest splitters to reach exactly p ports
    pick = [-1] * (hi + 1)     # pick[p] = size of the last splitter in that plan
    cnt[0] = 0
    for p in range(1, hi + 1):
        for s in sizes:
            if p >= s and cnt[p - s] + 1 < cnt[p]:
                cnt[p] = cnt[p - s] + 1
                pick[p] = s

    best = None
    for P in range(lo, hi + 1):
        if cnt[P] >= INF:
            continue
        util = 100.0 * hh / P
        in_band = 0 if (util_min <= util <= util_max) else 1

        # Reconstruct the multiset for this port target using largest-first
        # decomposition so the "distinct sizes" count reflects the plan we'd
        # actually install.
        counts = _decompose(P, sizes, pick)
        n_sizes = len(counts)
        largest = max(counts) if counts else max(sizes)

        # Unused ports (over-provision). Minimising this makes a small
        # residual demand add the smallest adequate splitter (1:32 instead
        # of another 1:64) — less stranded capacity per the HLD review.
        waste = P - hh

        # Closer-to-middle of the utilisation band (0 = exactly mid-band).
        mid = (util_min + util_max) / 2.0
        band_dist = abs(util - mid)

        # Ranking key — lexicographic, lower is better:
        #   1. in_band
        #   2. splitter count (fewest devices)
        #   3. unused ports (reduce stranded capacity)
        #   4. number of distinct sizes (standardisation)
        #   5. distance from mid-band utilisation
        #   6. total ports (tiebreaker: tighter fit)
        key = (in_band, cnt[P], waste, n_sizes, band_dist, P)
        if best is None or key < best[0]:
            best = (key, P, dict(counts))

    if best is None:
        # Unreachable target (shouldn't happen with an 8-port unit): stack the
        # largest size.
        big = sizes[-1]
        n = int(math.ceil(hh / float(big)))
        counts = {big: n}
    else:
        counts = best[2]

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
