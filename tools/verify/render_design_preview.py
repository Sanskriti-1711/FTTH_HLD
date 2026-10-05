"""Render a finished HLD run to a PNG so the design can be looked at.

Draws the layers the platform serves, in their map colours, straight from the
run's GeoPackages (all EPSG:25833, so no reprojection is needed), plus a zoomed
panel on the densest polygon so the trench/duct/cable/chamber relationships are
visible at street scale.

Usage:
    python tmp/render_design_preview.py <run_dir> <out.png> [--zoom-poly POLY_ID]

Run with the QGIS interpreter (tmp/qgis_env.sh) — it needs osgeo + matplotlib.
"""
import os
import sys
from collections import Counter, defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from osgeo import ogr

# trenches by construction class (mirrors the platform's TRENCH_TYPE buckets)
TRENCH_COLOR = {"Open Cut": "#2563EB", "HDD": "#DC2626",
                "Garden": "#16A34A", "Aerial": "#F59E0B"}
CHAMBER_COLOR = {"Bore": "#B91C1C", "Handhole": "#0369A1"}


def prop(props, *keys):
    """Case-insensitive property lookup.

    The designer publishes ``trench_type`` on ``Final_Trenches`` but the platform
    bucket field is ``TRENCH_TYPE``; reading only the uppercase name silently
    matched nothing and drew every span as the default class.
    """
    for k in keys:
        if k in props and props[k] not in (None, ""):
            return props[k]
    low = {str(k).lower(): v for k, v in props.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, ""):
            return v
    return None


def layer(path):
    if not os.path.exists(path):
        return None, []
    ds = ogr.Open(path)
    if ds is None:
        return None, []
    lyr = ds.GetLayer(0)
    defn = lyr.GetLayerDefn()
    names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    rows = []
    for f in lyr:
        g = f.geometry()
        if g is None:
            continue
        # Clone: the dataset is released when this function returns, and a
        # borrowed geometry pointer is dead the moment it is (segfault or
        # "argument 1 of type OGRGeometryShadow" downstream).
        rows.append((g.Clone(), {n: f[n] for n in names}))
    ds = None
    return names, rows


def parts(geom):
    """Every coordinate ring/line of a geometry, as [(x, y), ...]."""
    gt = geom.GetGeometryType()
    if gt in (ogr.wkbLineString, ogr.wkbLinearRing):
        yield [(geom.GetX(i), geom.GetY(i)) for i in range(geom.GetPointCount())]
    elif gt in (ogr.wkbMultiLineString, ogr.wkbPolygon, ogr.wkbMultiPolygon,
                ogr.wkbGeometryCollection):
        for i in range(geom.GetGeometryCount()):
            yield from parts(geom.GetGeometryRef(i))
    # points contribute nothing to a line plot


def first_point(geom):
    """A representative point of any geometry type, or None.

    ``GetX(0)`` raises "Incompatible geometry for operation" on a polygon or a
    multi-geometry, so the bbox test must not call it blindly.
    """
    if geom is None:
        return None
    gt = geom.GetGeometryType()
    if gt == ogr.wkbPoint:
        return (geom.GetX(), geom.GetY())
    if geom.GetPointCount() > 0:
        return (geom.GetX(0), geom.GetY(0))
    for i in range(geom.GetGeometryCount()):
        p = first_point(geom.GetGeometryRef(i))
        if p is not None:
            return p
    return None


def points(geom):
    if geom is None:
        return []
    gt = geom.GetGeometryType()
    if gt == ogr.wkbPoint:
        return [(geom.GetX(), geom.GetY())]
    if gt in (ogr.wkbMultiPoint, ogr.wkbGeometryCollection):
        out = []
        for i in range(geom.GetGeometryCount()):
            out.extend(points(geom.GetGeometryRef(i)))
        return out
    return []


def draw(ax, run, bbox=None):
    def clipped(geom):
        if bbox is None:
            return True
        p = first_point(geom)
        if p is None:
            return False
        return bbox[0] <= p[0] <= bbox[2] and bbox[1] <= p[1] <= bbox[3]

    # premises first (context), then trenches, ducts, cables, structures
    _, objs = layer(os.path.join(run, "Objects.gpkg"))
    ox = [p for g, _ in objs for p in points(g) if clipped(g)]
    if ox:
        ax.scatter([p[0] for p in ox], [p[1] for p in ox], s=6, c="#D1D5DB",
                   zorder=1, label="premises (%d)" % len(ox))

    _, tr = layer(os.path.join(run, "Final_Trenches.gpkg"))
    tcount = Counter()
    for g, p in tr:
        if not clipped(g):
            continue
        t = prop(p, "TRENCH_TYPE", "trench_type", "CONSTRUCT", "method") or "Open Cut"
        tcount[t] += 1
        for part in parts(g):
            if len(part) < 2:
                continue
            ax.plot([q[0] for q in part], [q[1] for q in part],
                    color=TRENCH_COLOR.get(t, "#2563EB"), lw=1.9,
                    solid_capstyle="round", zorder=3)

    for name, colour, lw, lab, z in (
        ("Feeder_Ducts", "#7C3AED", 1.1, "feeder duct", 5),
        ("Distribution_Ducts", "#0891B2", 0.85, "distribution duct", 5),
        ("Drop_Ducts", "#94A3B8", 0.5, "drop duct", 4),
        ("Feeder_Cable", "#111827", 0.55, "feeder cable", 6),
        ("Distribution_Cable", "#4B5563", 0.45, "distribution cable", 6),
    ):
        _, rows = layer(os.path.join(run, name + ".gpkg"))
        n = 0
        for g, _p in rows:
            if not clipped(g):
                continue
            for part in parts(g):
                if len(part) < 2:
                    continue
                n += 1
                ax.plot([q[0] for q in part], [q[1] for q in part],
                        color=colour, lw=lw, alpha=0.9, zorder=z)
        if n:
            ax.plot([], [], color=colour, lw=max(lw, 1.0),
                    label="%s (%d)" % (lab, len(rows)))

    _, nodes = layer(os.path.join(run, "Trench_Nodes.gpkg"))
    nx = [p for g, _ in nodes for p in points(g) if clipped(g)]
    if nx:
        ax.scatter([p[0] for p in nx], [p[1] for p in nx], s=7,
                   c="#6B7280", marker="o", alpha=0.55, zorder=7,
                   label="trench node (%d)" % len(nodes))

    _, ch = layer(os.path.join(run, "Chambers.gpkg"))
    by_sub = defaultdict(list)
    for g, p in ch:
        if not clipped(g):
            continue
        for pt in points(g):
            by_sub[prop(p, "SUBTYPE", "subtype", "CHAMBER_TYPE") or "?"].append(pt)
    for sub, pts_ in sorted(by_sub.items()):
        ax.scatter([p[0] for p in pts_], [p[1] for p in pts_],
                   s=34, marker="s", c=CHAMBER_COLOR.get(sub, "#111827"),
                   edgecolors="white", linewidths=0.5, zorder=8,
                   label="chamber %s (%d)" % (sub, len(pts_)))

    _, pdps = layer(os.path.join(run, "PDPs.gpkg"))
    px = [p for g, _ in pdps for p in points(g) if clipped(g)]
    if px:
        ax.scatter([p[0] for p in px], [p[1] for p in px], s=90, marker="^",
                   c="#F59E0B", edgecolors="#7C2D12", linewidths=0.7, zorder=9,
                   label="PDP (%d)" % len(px))

    _, mfg = layer(os.path.join(run, "MFG.gpkg"))
    mx = [p for g, _ in mfg for p in points(g) if clipped(g)]
    if mx:
        ax.scatter([p[0] for p in mx], [p[1] for p in mx], s=230, marker="H",
                   c="#7C3AED", edgecolors="white", linewidths=1.0, zorder=10,
                   label="MFG (%d)" % len(mx))
    return tcount


def main():
    run = sys.argv[1].rstrip("/\\") + "/"
    out = sys.argv[2]
    zoom_poly = None
    if "--zoom-poly" in sys.argv:
        zoom_poly = sys.argv[sys.argv.index("--zoom-poly") + 1]

    _, tr = layer(os.path.join(run, "Final_Trenches.gpkg"))
    xs, ys = [], []
    for g, _p in tr:
        for part in parts(g):
            for q in part:
                xs.append(q[0])
                ys.append(q[1])
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)

    # zoom target: the densest PDP cluster unless a polygon was named
    if zoom_poly:
        _, pdps = layer(os.path.join(run, "PDPs.gpkg"))
        px = [(x, y) for g, p in pdps for (x, y) in points(g)
              if p.get("POLYGON_ID") == zoom_poly]
    else:
        _, pdps = layer(os.path.join(run, "PDPs.gpkg"))
        px = [p for g, _p in pdps for p in points(g)]
    if px:
        best, bestn = None, -1
        for cx, cy in px:
            n = sum(1 for qx, qy in px
                    if (qx - cx) ** 2 + (qy - cy) ** 2 < 400 ** 2)
            if n > bestn:
                best, bestn = (cx, cy), n
        zx, zy = best
    else:
        zx, zy = (x0 + x1) / 2, (y0 + y1) / 2

    fig, axes = plt.subplots(1, 2, figsize=(21, 10.5),
                             gridspec_kw={"width_ratios": [1.35, 1]})
    draw(axes[0], run)
    axes[0].set_title("Full extent — %s" % os.path.basename(run.rstrip("/\\")),
                      fontsize=13, loc="left")
    axes[0].set_aspect("equal")
    axes[0].grid(alpha=0.15, lw=0.5)
    axes[0].ticklabel_format(style="plain", useOffset=False)
    axes[0].tick_params(labelsize=7)

    span = 260.0
    draw(axes[1], run, bbox=(zx - span, zy - span, zx + span, zy + span))
    axes[1].set_xlim(zx - span, zx + span)
    axes[1].set_ylim(zy - span, zy + span)
    axes[1].set_aspect("equal")
    axes[1].set_title("Zoom 520 m — trench / duct / cable / chamber detail",
                      fontsize=13, loc="left")
    axes[1].grid(alpha=0.2, lw=0.5)
    axes[1].tick_params(labelsize=7)

    h, lab = axes[1].get_legend_handles_labels()
    seen, hh, ll = set(), [], []
    for a, b in zip(h, lab):
        if b in seen:
            continue
        seen.add(b)
        hh.append(a)
        ll.append(b)
    hh.append(Line2D([], [], color="none"))
    ll.append("trench colour = TRENCH_TYPE")
    for t, c in TRENCH_COLOR.items():
        hh.append(Line2D([], [], color=c, lw=2.4))
        ll.append("trench: %s" % t)
    axes[1].legend(hh, ll, loc="upper left", fontsize=8.5, framealpha=0.92)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    fig.savefig(out, dpi=145, facecolor="white")
    print("wrote %s  (%.1f MB)" % (out, os.path.getsize(out) / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
