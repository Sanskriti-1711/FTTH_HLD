"""Do the drop ducts and couplers still reach the splitter chambers?

After a splitter takes over the structure inside its keep-out (the node, and
therefore the chamber, moves onto the cabinet), this checks the chain
premise -> drop duct -> coupler -> distribution duct -> splitter chamber for
every splitter, and separately for the chambers that were MOVED onto a
splitter, since those are the ones whose old position downstream geometry
might still reference.

Usage: python tmp/pdp_chamber_links.py <run_dir>
"""
import os
import sys

from osgeo import ogr

TOL = 1.0


def load(d, name):
    p = os.path.join(d, name)
    if not os.path.exists(p):
        return []
    ds = ogr.Open(p)
    lyr = ds.GetLayer(0)
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        it = f.items() or {}
        if g is not None:
            it["geom"] = g.Clone()
        out.append(it)
    ds = None
    return out


def line_ends(g):
    """Both endpoints of every part of a (multi)line."""
    nm = g.GetGeometryName().upper()
    parts = ([g.GetGeometryRef(i).Clone() for i in range(g.GetGeometryCount())]
             if nm.startswith("MULTI") else [g])
    out = []
    for s in parts:
        if s.GetPointCount():
            out.append(s.GetPoint(0))
            out.append(s.GetPoint(s.GetPointCount() - 1))
    return out


def min_dist_to_ends(pt, feats):
    """Distance to the nearest ENDPOINT (a joint is an end of one of them)."""
    best = float("inf")
    for f in feats:
        for e in line_ends(f["geom"]):
            v = ((pt[0] - e[0]) ** 2 + (pt[1] - e[1]) ** 2) ** 0.5
            if v < best:
                best = v
    return best


def min_dist_to_line(pt, feats):
    """Distance to the nearest duct GEOMETRY.

    A coupler taps the distribution duct MID-SPAN (a T-tap) — measuring to
    endpoints only would score every mid-span tap as unlinked.
    """
    p = ogr.Geometry(ogr.wkbPoint)
    p.AddPoint_2D(pt[0], pt[1])
    return min((p.Distance(f["geom"]) for f in feats), default=float("inf"))


def main(d):
    pdps = load(d, "PDPs.gpkg")
    ch = load(d, "Chambers.gpkg")
    dd = load(d, "Drop_Ducts.gpkg")
    xd = load(d, "Distribution_Ducts.gpkg")
    fd = load(d, "Feeder_Ducts.gpkg")
    cp = load(d, "Coupleurs.gpkg")
    objs = load(d, "Objects.gpkg")
    print(f"PDPs {len(pdps)}  chambers {len(ch)}  drop ducts {len(dd)}  "
          f"distribution ducts {len(xd)}  feeder ducts {len(fd)}  couplers {len(cp)}")
    by_struct = {str(c.get("STRUCT_ID")): c for c in ch}

    # ---- A. every splitter has its chamber and a duct reaching it ----------
    print("\n=== A. distribution duct reaches the splitter chamber ===")
    ok = noroom = noduct = 0
    worst = 0.0
    worst_src = "-"
    taken_over = []
    for p in pdps:
        pid = str(p.get("PDP_ID"))
        g = p["geom"]
        c = min(ch, key=lambda c: g.Distance(c["geom"]))
        dmin = g.Distance(c["geom"])
        sid = str(c.get("STRUCT_ID"))
        if dmin > TOL:
            noroom += 1
            print(f"  {pid}: chamber {dmin:.2f} m away ({sid})")
            continue
        # a duct of either tier naming this chamber
        attached = [x for x in (xd + fd) if sid in (str(x.get("START_CHAMBER")), str(x.get("END_CHAMBER")))]
        if attached:
            ok += 1
            for a in attached:
                v = a["geom"].Distance(c["geom"])
                if v > worst:
                    worst, worst_src = v, f"{a.get('DUCT_ID')} -> {sid}"
        else:
            noduct += 1
        if str(c.get("REASON")) != "Splitter/F2D (PDP)":
            taken_over.append((pid, sid, str(c.get("REASON"))))
    print(f"  chamber at the cabinet: {len(pdps) - noroom}/{len(pdps)}")
    print(f"  a duct naming that chamber: {ok}/{len(pdps)}   (no duct naming it: {noduct}, "
          f"worst duct {worst:.2f} m from the chamber: {worst_src})")
    print(f"  chambers that TOOK OVER a structure (moved onto the splitter): {len(taken_over)}")
    for pid, sid, reason in taken_over:
        print(f"     {pid} -> {sid} ({reason})")

    # ---- B. couplers link a distribution duct to a drop duct --------------
    print("\n=== B. couplers ===")
    linked = half = broken = 0
    for c in cp:
        pt = (c["geom"].GetX(), c["geom"].GetY())
        dx = min_dist_to_line(pt, xd)          # taps the distribution duct mid-span
        dp = min_dist_to_ends(pt, dd)          # joins the drop duct at its end
        if dx <= TOL and dp <= TOL:
            linked += 1
        elif dx <= TOL or dp <= TOL:
            half += 1
        else:
            broken += 1
    print(f"  {len(cp)} couplers: linked (distribution <=1m AND drop end <=1m) {linked}, "
          f"one-sided {half}, touching neither {broken}")
    dx_off = dp_off = 0
    for c in cp:
        pt = (c["geom"].GetX(), c["geom"].GetY())
        if min_dist_to_line(pt, xd) > TOL:
            dx_off += 1
        if min_dist_to_ends(pt, dd) > TOL:
            dp_off += 1
    print(f"     distribution side off: {dx_off}, drop side off: {dp_off}")

    # named-pair test: does the coupler actually touch the two ducts it names?
    xd_by_id = {str(x.get("DUCT_ID")): x for x in xd}
    dd_by_uid = {str(x.get("DUCT_UID")): x for x in dd}
    n_ok = n_miss_x = n_miss_d = n_no_ref = 0
    for c in cp:
        xr = xd_by_id.get(str(c.get("DIST_DUCT_ID") or ""))
        dr = dd_by_uid.get(str(c.get("DROP_DUCT_UID") or ""))
        if xr is None and dr is None:
            n_no_ref += 1
            continue
        pt = (c["geom"].GetX(), c["geom"].GetY())
        px = min_dist_to_line(pt, [xr]) if xr is not None else float("inf")
        pd = min_dist_to_ends(pt, [dr]) if dr is not None else float("inf")
        if px <= TOL and pd <= TOL:
            n_ok += 1
        else:
            if px > TOL:
                n_miss_x += 1
            if pd > TOL:
                n_miss_d += 1
    print(f"  coupler vs the ducts it NAMES: joined {n_ok}, named distribution not reached "
          f"{n_miss_x}, named drop duct not reached {n_miss_d}, no resolvable reference {n_no_ref}")

    # ---- C. drop ducts: chamber end + distribution end + premise end ------
    print("\n=== C. drop ducts ===")
    named = res_ok = res_bad = 0
    close_dist = close_prem = 0
    for du in dd:
        sc, ec = str(du.get("START_CHAMBER") or ""), str(du.get("END_CHAMBER") or "")
        ends = line_ends(du["geom"])
        if sc or ec:
            named += 1
            pts = [by_struct[k] for k in (sc, ec) if k in by_struct]
            if pts:
                res_ok += 1
            else:
                res_bad += 1
        if min((pt[0] - o["geom"].GetX()) ** 2 + (pt[1] - o["geom"].GetY()) ** 2
               for pt in ends for o in objs) ** 0.5 <= TOL:
            close_prem += 1
        if min_dist_to_ends(ends[0], xd) <= TOL or min_dist_to_ends(ends[-1], xd) <= TOL:
            close_dist += 1
    print(f"  naming a chamber: {named}/{len(dd)}  (both ids resolve to a real STRUCT_ID: {res_ok}, "
          f"unresolved: {res_bad})")
    print(f"  an end within 1 m of the premise it serves: {close_prem}/{len(dd)}")
    print(f"  an end within 1 m of a distribution duct:   {close_dist}/{len(dd)}")

    # ---- D. ducts per moved chamber --------------------------------------
    if taken_over:
        print("\n=== D. the moved chambers ===")
        moved_ids = {sid for _, sid, _ in taken_over}
        for sid in sorted(moved_ids):
            c = by_struct[sid]
            pt = (c["geom"].GetX(), c["geom"].GetY())
            xd_n = [x for x in xd if sid in (str(x.get("START_CHAMBER")), str(x.get("END_CHAMBER")))]
            dd_n = [x for x in dd if sid in (str(x.get("START_CHAMBER")), str(x.get("END_CHAMBER")))]
            cp_n = sum(1 for k in cp if str(k.get("DIST_DUCT_ID") or "") in
                       {str(x.get("DUCT_ID")) for x in xd_n})
            print(f"  {sid}: CONN_DUCTS={c.get('CONN_DUCTS')}, distribution ducts naming it "
                  f"{len(xd_n)}, drop ducts naming it {len(dd_n)}, couplers on those ducts {cp_n}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
