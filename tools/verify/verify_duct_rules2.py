"""Verify the duct/cable rules on a finished run (docs/stages/HLD.md).

  1. Distribution cable = ONE trunk per spine span (CABLE_TYPE=Distribution,
     FIBER_COUNT = max(48, households + 2)) + ONE drop per premise on its
     garden leg (CABLE_TYPE=Drop, 12F garden sizing).
  2. Route ducts are built from the cables: feeder 4-Way (solid behaviour),
     distribution 2-Way, and the drop legs live ONLY in Drop_Ducts.
  3. Distribution duct length must track the shared spine, not spine+garden.

Usage: python tmp/verify_duct_rules2.py <output_dir>
"""
import os
import sys

from osgeo import ogr

LAYERS = {
    "trenches": "Final_Trenches.gpkg",
    "dist_duct": "Distribution_Ducts.gpkg",
    "dist_duct_runs": "Distribution_Ducts_Runs.gpkg",
    "feeder_duct": "Feeder_Ducts.gpkg",
    "drop_duct": "Drop_Ducts.gpkg",
    "dist_cable": "Distribution_Cable.gpkg",
    "feeder_cable": "Feeder_Cable.gpkg",
    "chambers": "Chambers.gpkg",
}

FIELDS = {
    "trenches": ["trench_type", "TRENCH_TIER", "length_m"],
    "dist_duct": ["DUCT_TYPE", "length_m", "N_DUCTS", "WAYS_TOTAL", "CLUBS",
                  "BUNDLE_LEN_M", "capacity_total", "ways_used", "cables_carried"],
    "dist_duct_runs": ["DUCT_TYPE", "length_m", "capacity_total", "ways_used"],
    "feeder_duct": ["DUCT_TYPE", "length_m", "N_DUCTS", "WAYS_TOTAL", "CLUBS"],
    "drop_duct": ["DUCT_TYPE", "LENGTH_M", "WAYS"],
    "dist_cable": ["CABLE_TYPE", "CONNECTION_TYPE", "FIBER_COUNT", "HH_COUNT",
                   "length_m", "AVAILABLE_FIBERS"],
    "feeder_cable": ["CABLE_TYPE", "FIBER_COUNT", "length_m", "TRUNK_NO"],
    "chambers": ["SUBTYPE", "CHAMBER_TYPE"],
}


def main(out_dir):
    print("output:", out_dir)
    summary = {}
    for key, fname in LAYERS.items():
        path = os.path.join(out_dir, fname)
        if not os.path.exists(path):
            print("MISSING", key, fname)
            continue
        ds = ogr.Open(path)
        lyr = ds.GetLayer(0)
        names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName()
                 for i in range(lyr.GetLayerDefn().GetFieldCount())]
        want = [f for f in FIELDS[key] if f in names]
        rows = []
        total_len = 0.0
        for feat in lyr:
            r = {f: feat.GetField(f) for f in want}
            g = feat.GetGeometryRef()
            if g is not None:
                r["_len"] = g.Length()
            rows.append(r)
            if "length_m" in r and r["length_m"]:
                total_len += float(r["length_m"])
        summary[key] = (rows, total_len, want)
        print("\n== %s : %d feature(s) | sum(length_m)=%.0f m"
              % (key, len(rows), total_len))
        for f in want:
            vals = {}
            for r in rows:
                v = r.get(f)
                if v is None or v == "":
                    continue
                vals[str(v)] = vals.get(str(v), 0) + 1
            if 1 <= len(vals) <= 12:
                print("   %-14s %s" % (f, dict(sorted(vals.items(),
                                                       key=lambda kv: -kv[1])[:12])))
            elif vals:
                print("   %-14s (%d distinct values)" % (f, len(vals)))

    # --- rule checks ---------------------------------------------------
    print("\n================ RULE CHECKS ================")
    trows, tlen = summary["trenches"][0], summary["trenches"][1]
    feeder = sum(float(r["length_m"] or 0) for r in trows
                 if str(r.get("TRENCH_TIER") or "") == "Feeder")
    spine = sum(float(r["length_m"] or 0) for r in trows
                if str(r.get("TRENCH_TIER") or "") == "Distribution")
    garden = sum(float(r["length_m"] or 0) for r in trows
                 if str(r.get("TRENCH_TIER") or "") == "Garden")
    print("feeder trench                    : %8.0f m" % feeder)
    print("distribution spine (cable/duct)  : %8.0f m" % spine)
    print("garden  (drop legs)              : %8.0f m" % garden)

    crows = summary["dist_cable"][0]
    trunks = [r for r in crows if str(r.get("CABLE_TYPE") or "") == "Distribution"]
    drops = [r for r in crows if str(r.get("CABLE_TYPE") or "") == "Drop"]
    trunk_m = sum(float(r.get("length_m") or 0) for r in trunks)
    drop_m = sum(float(r.get("length_m") or 0) for r in drops)
    print("dist cable: %d trunk(s) %.0f m | %d drop(s) %.0f m"
          % (len(trunks), trunk_m, len(drops), drop_m))
    tfib = sorted({int(r["FIBER_COUNT"]) for r in trunks if r.get("FIBER_COUNT")})
    dfib = sorted({int(r["FIBER_COUNT"]) for r in drops if r.get("FIBER_COUNT")})
    print("   trunk FIBER_COUNT values      :", tfib or "n/a")
    print("   drop  FIBER_COUNT values      :", dfib or "n/a")
    # Documented rule: Distribution = max(48, households + 2); a garden-leg
    # drop takes the 12F garden floor and is sized by the dwellings it serves.
    drop_ok = True
    for r in drops:
        fc = int(r["FIBER_COUNT"])
        hh = int(r.get("HH_COUNT") or 0)
        if fc < 12 or (hh and fc < hh + 2):
            drop_ok = False
    print("   rule: trunk = max(48, hh+2)   :", "PASS" if tfib and min(tfib) >= 48 else "CHECK")
    print("   rule: drop = max(12, hh+2)    :", "PASS" if drop_ok else "CHECK",
          "(floor 12 seen: %s)" % (12 in dfib if dfib else False))
    trunk_ratio = trunk_m / spine if spine else 0
    print("   trunk metres / dist spine     : %.2fx (want ~1.0)" % trunk_ratio)
    print("   drop metres / garden legs     : %.2fx (drop includes the PDP->footway arm)"
          % ((drop_m / garden) if garden else 0))

    drows, dlen = summary["dist_duct"][0], summary["dist_duct"][1]
    print("dist duct published              : %.0f m  (%.2fx dist spine, want ~1.0)"
          % (dlen, (dlen / spine) if spine else 0))
    if "'N_DUCTS'" in str(summary["dist_duct"][2]) or "N_DUCTS" in summary["dist_duct"][2]:
        for r in drows:
            print("   corridor: N_DUCTS=%s WAYS_TOTAL=%s CLUBS=%s BUNDLE_LEN_M=%s"
                  % (r.get("N_DUCTS"), r.get("WAYS_TOTAL"), r.get("CLUBS"),
                     r.get("BUNDLE_LEN_M")))
    runs = summary["dist_duct_runs"][0]
    over = 0
    tot_ways = used = 0
    for r in runs:
        try:
            tot_ways += int(r.get("capacity_total") or 0)
            used += int(r.get("ways_used") or 0)
            if int(r.get("ways_used") or 0) > int(r.get("capacity_total") or 0):
                over += 1
        except Exception:
            pass
    print("dist duct route runs              : %d | ways %d/%d used | over-packed %d"
          % (len(runs), used, tot_ways, over))

    rrows = summary["drop_duct"][0]
    print("drop ducts (1-Way per premise)    : %d" % len(rrows))
    print("   rule: one per premise          :",
          "PASS" if abs(len(rrows) - len(drops)) <= 2 else "CHECK (cables=%d)" % len(drops))

    frows = summary["feeder_duct"][0]
    for r in frows:
        print("feeder duct: %s N_DUCTS=%s WAYS_TOTAL=%s CLUBS=%s"
              % (r.get("DUCT_TYPE"), r.get("N_DUCTS"), r.get("WAYS_TOTAL"), r.get("CLUBS")))
    print("   rule: 4-Way HDPE HDPE profile  :",
          "PASS" if frows and "4-Way" in str(frows[0].get("DUCT_TYPE")) else "CHECK")

    chrows = summary["chambers"][0]
    print("chambers                          : %d by subtype %s"
          % (len(chrows), summary["chambers"][1] and "" or ""))
    sub = {}
    for r in chrows:
        sub[str(r.get("SUBTYPE"))] = sub.get(str(r.get("SUBTYPE")), 0) + 1
    print("   subtypes                       :", sub)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
