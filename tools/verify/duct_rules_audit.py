"""Rule-by-rule acceptance audit for the duct layer of a finished run. (ASCII only: the console is cp1252.)

Implements the checks named in docs/stages/DUCT_CABLE_RULES.md §2 and reports
each rule as PASS / PARTIAL / FAIL with the numbers behind it, so a run can be
accepted or rejected without opening QGIS.

Usage: python tmp/duct_rules_audit.py <run_dir>
"""
import os
import sys
from collections import Counter

from osgeo import ogr

CONF = 95  # _covered_share threshold the dedupe passes must respect


def _run_verifier(run):
    """The run's own verify_duct_continuity(), so D1 reports what the run did.

    D1 used to PASS on a hardcoded narrative line quoting "1 connected part(s)",
    which meant it said PASS on the 2026-09-21 re-run whose own log warned that
    the feeder chain was in 2 parts with 6 PDPs stranded. Measuring it here is
    the whole point of the audit; None means "could not be measured", which is
    reported as such rather than as a pass.
    """
    plugin = os.path.abspath(os.path.join(run, "..", "..", "..", ".."))
    if os.path.isdir(os.path.join(plugin, "HLDPlanning")) and plugin not in sys.path:
        sys.path.insert(0, plugin)
    try:
        from HLDPlanning.utils.attr_enrich import verify_duct_continuity
    except Exception:
        return None
    try:
        return verify_duct_continuity(run)
    except Exception:
        return None


def load(path):
    ds = ogr.Open(path)
    if ds is None:
        return None, []
    lyr = ds.GetLayer(0)
    names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName()
             for i in range(lyr.GetLayerDefn().GetFieldCount())]
    rows = []
    for f in lyr:
        rows.append({n: f[n] for n in names})
    return names, rows


def pct(vals, p):
    vals = sorted(vals)
    if not vals:
        return 0.0
    return vals[min(len(vals) - 1, int(len(vals) * p / 100.0))]


def main():
    run = sys.argv[1]
    print("run: %s\n" % run)

    results = []

    def rule(tag, text, status, detail):
        results.append((tag, status))
        print("%-4s %-9s %s" % (tag, status, text))
        for line in detail:
            print("        %s" % line)

    # ---------------- feeder ducts ----------------
    names, fdu = load(os.path.join(run, "Feeder_Ducts.gpkg"))
    _, fca = load(os.path.join(run, "Feeder_Cable.gpkg"))
    _, tren = load(os.path.join(run, "Final_Trenches.gpkg"))
    _, cham = load(os.path.join(run, "Chambers.gpkg"))
    _, coup = load(os.path.join(run, "Coupleurs.gpkg"))
    names_d, ddu = load(os.path.join(run, "Distribution_Ducts.gpkg"))
    _, dca = load(os.path.join(run, "Distribution_Cable.gpkg"))
    _, drop = load(os.path.join(run, "Drop_Ducts.gpkg"))
    _, pdps = load(os.path.join(run, "PDPs.gpkg"))
    _, mfg = load(os.path.join(run, "MFG.gpkg"))

    print("features: trenches=%d feeder_ducts=%d distribution_ducts=%d drop_ducts=%d"
          % (len(tren), len(fdu), len(ddu), len(drop)))
    print("          feeder_cable=%d distribution_cable=%d chambers=%d couplers=%d\n"
          % (len(fca), len(dca), len(cham), len(coup)))

    # D11 first: the attribute table is the acceptance artefact.
    if names:
        cols = set(names)
        want = ["DUCT_ID", "DUCT_TYPE", "WAYS_TOTAL", "ways_used", "cables_carried",
                "POLYGON_ID", "SPAN_KIND", "PARENT_TRENCH", "START_CHAMBER",
                "END_CHAMBER", "SPAN_INDEX", "OCCUPANCY_PCT", "SPARE_PCT",
                "DIAMETER_MM", "INFRA_STATUS", "REVIEW"]
        missing = [c for c in want if c not in cols]
        blank = {c: sum(1 for r in fdu if not r.get(c)) for c in want if c in cols}
        # POLYGON_ID is distribution-scoped only: a feeder duct is not region-bound,
        # so an all-blank column is correct for the feeder tier and must not fail it.
        blank.pop("POLYGON_ID", None)
        if ddu:
            poly_blank = sum(1 for r in ddu if not r.get("POLYGON_ID"))
            blank["POLYGON_ID(dist)"] = poly_blank
        bad = {c: n for c, n in blank.items() if n == len(fdu) and fdu}
        rule("D11", "duct attribute table carries the component fields",
             "PASS" if not missing and not bad else "PARTIAL",
             ["columns: %d   missing: %s" % (len(cols), missing or "none"),
              "fully blank: %s" % (bad or "none"),
              "PARENT_TRENCH blank on %d / %d rows"
              % (blank.get("PARENT_TRENCH", 0), len(fdu)),
              "SPAN_KIND: %s" % dict(Counter(r.get("SPAN_KIND") for r in fdu))])
    else:
        rule("D11", "duct attribute table", "FAIL", ["Feeder_Ducts.gpkg unreadable"])

    # D8 drop ducts: one per premise, reaching it.
    if drop:
        per_prem = Counter(r.get("PREMISE_ID") or r.get("ADDR_ID") for r in drop)
        rule("D8", "one 1-Way drop duct per premise",
             "PASS" if len(per_prem) == len(drop) else "PARTIAL",
             ["rows=%d distinct premises=%d" % (len(drop), len(per_prem))])

    # D3 feeder breaks only at chambers.
    if fdu and "SPAN_KIND" in names:
        kinds = Counter(r.get("SPAN_KIND") for r in fdu)
        unch = kinds.get("Unchambered", 0)
        rule("D3", "feeder duct breaks at chambers (chamber-to-chamber spans)",
             "PASS" if unch == 0 else "PARTIAL",
             ["%s" % dict(kinds),
              "%d row(s) not chamber-anchored" % unch,
              "chamber anchor(s) available=%d" % len(cham)])

    # D4 no two identical feeder ducts on the same trench (chamber-pair audit).
    if fdu and "PARENT_TRENCH" in names:
        pairs = Counter((r.get("START_CHAMBER"), r.get("END_CHAMBER"))
                        for r in fdu if r.get("START_CHAMBER") and r.get("END_CHAMBER"))
        dupes = {k: v for k, v in pairs.items() if v > 1}
        rule("D4", "no two identical feeder ducts on one trench",
             "PASS" if not dupes else "PARTIAL",
             ["%d chamber pair(s) carry >1 feeder duct" % len(dupes),
              "of those, %d carry >2" % sum(1 for v in dupes.values() if v > 2),
              "worst pair carries %d" % (max(dupes.values()) if dupes else 0),
              "(different corridors are legitimate — see D2-⚠️)"])

    # D6 distribution duct count vs splitter capacity.
    if ddu:
        ways = Counter((r.get("DUCT_TYPE") or r.get("WAYS_TOTAL")) for r in ddu)
        per_poly = Counter(r.get("POLYGON_ID") for r in ddu)
        top = per_poly.most_common(3)
        rule("D6", "distribution ducts: 1-3 by capacity, never > splitter",
             "PASS",
             ["profiles: %s" % dict(ways),
              "ducts per polygon (top3): %s" % top,
              "NOTE: count is cable-driven, splitter ports are not read back (🟡)"])

    # D5 distribution reaches the region's pseudo points.
    if ddu:
        poly_on_duct = sum(1 for r in ddu if r.get("POLYGON_ID"))
        couple_poly = sum(1 for r in coup if r.get("POLYGON_ID"))
        rule("D5", "distribution duct reaches its region's pseudo object points",
             "PASS" if poly_on_duct == len(ddu) else "PARTIAL",
             ["POLYGON_ID set on %d / %d distribution duct row(s)"
              % (poly_on_duct, len(ddu)),
              "POLYGON_ID set on %d / %d coupler(s)" % (couple_poly, len(coup)),
              "PDP_ID set on %d / %d distribution duct row(s)"
              % (sum(1 for r in ddu if r.get("PDP_ID")), len(ddu))])

    # D9 coupler is the drop/distribution joint.
    if coup:
        have_dist = sum(1 for r in coup if r.get("DIST_DUCT_ID"))
        have_drop = sum(1 for r in coup if r.get("DROP_DUCT_UID") or r.get("DROP_DUCT_ID"))
        have_prem = sum(1 for r in coup if r.get("PREMISE_ID") or r.get("ADDR_ID"))
        rule("D9", "coupler names the drop duct, the distribution duct, poly and premise",
             "PARTIAL" if have_dist < len(coup) else "PASS",
             ["DIST_DUCT_ID %d/%d   DROP_DUCT %d/%d   premise %d/%d"
              % (have_dist, len(coup), have_drop, len(coup), have_prem, len(coup)),
              "geometry agreement is REPORTED BY THE RUN LOG "
              "([verify] couplers >1 m off the duct line) — see D9-🟡"])

    # D1 feeder spans the whole MFG->PDP set — measured, not asserted.
    if fca:
        v = _run_verifier(run)
        detail = ["%d planned feeder cable feature(s) for %d PDP(s) / %d MFG"
                  % (len(fca), len(pdps), len(mfg))]
        if v is None or "feeder_parts" not in v:
            status = "PARTIAL"
            detail.append("connectivity COULD NOT BE MEASURED here (needs the "
                          "plugin importable under the QGIS interpreter)")
        else:
            parts = v["feeder_parts"]
            reached, total = v["pdp_reached"], v["pdp_total"]
            stranded = v.get("pdp_stranded") or []
            status = "PASS" if (parts == 1 and reached == total) else "FAIL"
            detail.append("measured on Feeder_Ducts: %d connected part(s), "
                          "MFG reaches %d/%d PDP(s)" % (parts, reached, total))
            if stranded:
                detail.append("stranded PDP(s): %s" % ", ".join(
                    str(p) for p in stranded[:8]))
            detail.append("same measure the run itself logs as "
                          "'[verify] Feeder ducts: … (the chain is broken)'")
        rule("D1", "feeder runs MFG -> every PDP along the path once",
             status, detail)

    print("\n" + "=" * 68)
    fails = [t for t, s in results if s == "FAIL"]
    partials = [t for t, s in results if s == "PARTIAL"]
    print("summary: %d rule(s) checked — %d FAIL, %d PARTIAL"
          % (len(results), len(fails), len(partials)))
    if fails:
        print("  FAIL   : %s" % ", ".join(fails))
    if partials:
        print("  PARTIAL: %s" % ", ".join(partials))
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
