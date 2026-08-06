#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clone brownfield GeoJSON layers from actual HLD output GPKGs using GDAL.
"""
import json, math, random, os
from osgeo import ogr

random.seed(42)
OUT_DIR = "HLDPlanning/output"
# Delete old brownfield files first
for f in os.listdir('.'):
    if f.startswith('brownfield_') and f.endswith('.geojson'):
        os.remove(f)
        print(f"  Deleted old: {f}")

def gpkg_to_features(gpkg_path, layer_name):
    """Extract features from GPKG using GDAL."""
    ds = ogr.Open(gpkg_path)
    if not ds:
        return []
    lyr = ds.GetLayerByName(layer_name)
    if not lyr:
        lyr = ds.GetLayer(0)
    if not lyr:
        return []
    features = []
    lyr.ResetReading()
    for feat in lyr:
        geom = feat.GetGeometryRef()
        if not geom:
            continue
        geojson_str = geom.ExportToJson()
        gj = json.loads(geojson_str)
        props = {}
        for i in range(feat.GetFieldCount()):
            fdef = feat.GetFieldDefnRef(i)
            name = fdef.GetName()
            if name.startswith('fid'):
                continue
            val = feat.GetField(i)
            if val is not None:
                props[name] = val
        features.append({"type": "Feature", "geometry": gj, "properties": props})
    ds = None
    return features

def make_fc(feats, crs="EPSG:25833"):
    return {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": crs}}, "features": feats}

def gj_point(coords, props):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": list(coords)}, "properties": props}

def gj_line(coords, props):
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [list(c) for c in coords]}, "properties": props}


# ══════════════════════════════════════════════════════════════════════════════
# 1. EXISTING PDPs — clone subset from PDPs.gpkg
# ══════════════════════════════════════════════════════════════════════════════
print("=== 1. brownfield_existing_pdps.geojson ===")
pdps = gpkg_to_features(f"{OUT_DIR}/PDPs.gpkg", "PDPs")
subset_pdps = pdps[::2]
bf_pdps = []
for i, f in enumerate(subset_pdps):
    p = f["properties"]
    c = f["geometry"]["coordinates"]
    bf_pdps.append(gj_point(c, {
        "SRC_ID": p.get("PDP_ID", f"BF_PDP_{i:03d}"),
        "STAGE": "brownfield",
        "ASSET_TYPE": "pdp",
        "INFRA_STATUS": "Existing",
        "VERIFY_STATUS": random.choice(["Verified","Verified","Verified","Assumed"]),
        "CAPACITY_USED": random.randint(0, 24),
        "CAPACITY_TOTAL": random.choice([32, 32, 64]),
        "POLYGON_ID": p.get("POLYGON_ID", ""),
        "PDP_ID": p.get("PDP_ID", ""),
        "MFG_ID": p.get("MFG_ID", ""),
        "NODE_TYPE": "PDP",
        "REUSE_SOURCE": "",
        "HH": p.get("HH", 0),
        "label": p.get("label", ""),
    }))
json.dump(make_fc(bf_pdps), open("brownfield_existing_pdps.geojson", "w"), indent=2)
print(f"  -> {len(bf_pdps)} PDPs (from PDPs.gpkg)")


# ══════════════════════════════════════════════════════════════════════════════
# 2. EXISTING MFG
# ══════════════════════════════════════════════════════════════════════════════
print("=== 2. brownfield_existing_mfg.geojson ===")
mfgs = gpkg_to_features(f"{OUT_DIR}/MFG.gpkg", "MFG")
bf_mfgs = []
for i, f in enumerate(mfgs):
    p = f["properties"]
    c = f["geometry"]["coordinates"]
    bf_mfgs.append(gj_point(c, {
        "SRC_ID": p.get("MFG_ID", f"BF_MFG_{i:03d}"),
        "STAGE": "brownfield",
        "ASSET_TYPE": "mfg",
        "INFRA_STATUS": "Existing",
        "VERIFY_STATUS": "Verified",
        "CAPACITY_USED": 0, "CAPACITY_TOTAL": 1,
        "POLYGON_ID": p.get("POLYGON_ID", ""),
        "PDP_ID": "", "MFG_ID": p.get("MFG_ID", ""),
        "NODE_TYPE": "MFG",
        "REUSE_SOURCE": "",
    }))
json.dump(make_fc(bf_mfgs), open("brownfield_existing_mfg.geojson", "w"), indent=2)
print(f"  -> {len(bf_mfgs)} MFG (from MFG.gpkg)")


# ══════════════════════════════════════════════════════════════════════════════
# 3. FEEDER TRENCH — every 3rd from Feeder_Trench.gpkg
# ══════════════════════════════════════════════════════════════════════════════
print("=== 3. brownfield_feeder_trench.geojson ===")
feeder = gpkg_to_features(f"{OUT_DIR}/Feeder_Trench.gpkg", "Feeder_Trench")
subset_feeder = feeder[::3]
bf_feeder = []
for i, f in enumerate(subset_feeder):
    p = f["properties"]
    g = f["geometry"]
    coords = g.get("coordinates", [])
    # Flatten MultiLineString -> LineString
    if g["type"] == "MultiLineString":
        flat = []
        for ln in coords:
            flat.extend(ln)
        coords = flat
    if len(coords) >= 2:
        bf_feeder.append(gj_line(coords, {
            "SRC_ID": f"BF_FDR_{i:03d}",
            "STAGE": "brownfield",
            "ASSET_TYPE": "trench",
            "INFRA_STATUS": "Existing",
            "VERIFY_STATUS": random.choice(["Verified","Verified","Verified","Assumed","Survey Required"]),
            "CAPACITY_USED": 0, "CAPACITY_TOTAL": 1,
            "POLYGON_ID": p.get("POLYGON_ID", ""),
            "PDP_ID": p.get("PDP_ID", ""),
            "REUSE_SOURCE": "",
            "trench_type": "feeder",
        }))
json.dump(make_fc(bf_feeder), open("brownfield_feeder_trench.geojson", "w"), indent=2)
print(f"  -> {len(bf_feeder)} feeder trenches (from Feeder_Trench.gpkg)")


# ══════════════════════════════════════════════════════════════════════════════
# 4. DISTRIBUTION TRENCH — every 6th from Distribution_Trench.gpkg
# ══════════════════════════════════════════════════════════════════════════════
print("=== 4. brownfield_distribution_trench.geojson ===")
dist = gpkg_to_features(f"{OUT_DIR}/Distribution_Trench.gpkg", "Distribution_Trench")
subset_dist = dist[::6]
bf_dist = []
for i, f in enumerate(subset_dist):
    p = f["properties"]
    g = f["geometry"]
    coords = g.get("coordinates", [])
    if g["type"] == "MultiLineString":
        flat = []
        for ln in coords:
            flat.extend(ln)
        coords = flat
    if len(coords) >= 2:
        bf_dist.append(gj_line(coords, {
            "SRC_ID": f"BF_DST_{i:03d}",
            "STAGE": "brownfield",
            "ASSET_TYPE": "trench",
            "INFRA_STATUS": "Existing",
            "VERIFY_STATUS": random.choice(["Verified","Verified","Verified","Assumed"]),
            "CAPACITY_USED": 0, "CAPACITY_TOTAL": 1,
            "POLYGON_ID": p.get("POLYGON_ID", ""),
            "PDP_ID": p.get("PDP_ID", ""),
            "MFG_ID": p.get("MFG_ID", ""),
            "REUSE_SOURCE": "",
            "trench_type": "distribution",
            "length_m": p.get("length_m", 0),
        }))
json.dump(make_fc(bf_dist), open("brownfield_distribution_trench.geojson", "w"), indent=2)
print(f"  -> {len(bf_dist)} distribution trenches (from Distribution_Trench.gpkg)")


# ══════════════════════════════════════════════════════════════════════════════
# 5. EXISTING DUCTS — same paths as feeder trenches, with duct metadata
# ══════════════════════════════════════════════════════════════════════════════
print("=== 5. brownfield_existing_ducts.geojson ===")
bf_ducts = []
for i, f in enumerate(subset_feeder):
    p = f["properties"]
    g = f["geometry"]
    coords = g.get("coordinates", [])
    if g["type"] == "MultiLineString":
        flat = []
        for ln in coords:
            flat.extend(ln)
        coords = flat
    if len(coords) >= 2:
        cap_total = 4 if i < 4 else random.choice([2, 3])
        cap_used = random.randint(0, cap_total)
        bf_ducts.append(gj_line(coords, {
            "SRC_ID": f"BF_DUCT_{i:03d}",
            "STAGE": "brownfield",
            "ASSET_TYPE": "duct",
            "INFRA_STATUS": "Existing",
            "VERIFY_STATUS": random.choice(["Verified","Verified","Assumed","Survey Required"]),
            "CAPACITY_USED": cap_used,
            "CAPACITY_TOTAL": cap_total,
            "POLYGON_ID": p.get("POLYGON_ID", ""),
            "PDP_ID": p.get("PDP_ID", ""),
            "REUSE_SOURCE": "",
            "MATERIAL": random.choice(["HDPE","HDPE","PVC"]),
            "DIAMETER_MM": random.choice([40, 50, 63]),
        }))
json.dump(make_fc(bf_ducts), open("brownfield_existing_ducts.geojson", "w"), indent=2)
print(f"  -> {len(bf_ducts)} ducts (from feeder trench geometry)")


# ══════════════════════════════════════════════════════════════════════════════
# 6. EXISTING CHAMBERS — near PDP locations
# ══════════════════════════════════════════════════════════════════════════════
print("=== 6. brownfield_existing_chambers.geojson ===")
bf_chambers = []
for i, f in enumerate(subset_pdps):
    p = f["properties"]
    c = f["geometry"]["coordinates"]
    ch = [c[0] + random.uniform(-5, 5), c[1] + random.uniform(-5, 5)]
    bf_chambers.append(gj_point(ch, {
        "SRC_ID": f"BF_CH_{i:03d}",
        "STAGE": "brownfield",
        "ASSET_TYPE": "chamber",
        "INFRA_STATUS": "Existing",
        "VERIFY_STATUS": random.choice(["Verified","Verified","Verified","Survey Required"]),
        "CAPACITY_USED": 0, "CAPACITY_TOTAL": 1,
        "POLYGON_ID": p.get("POLYGON_ID", ""),
        "PDP_ID": "", "REUSE_SOURCE": "",
        "NODE_TYPE": "chamber",
        "CHAMBER_TYPE": random.choice(["MH","MH","HH"]),
    }))
json.dump(make_fc(bf_chambers), open("brownfield_existing_chambers.geojson", "w"), indent=2)
print(f"  -> {len(bf_chambers)} chambers (near PDP locations)")


# ══════════════════════════════════════════════════════════════════════════════
# 7. EXISTING FIBRE — along feeder trench paths with spare duct capacity
# ══════════════════════════════════════════════════════════════════════════════
print("=== 7. brownfield_existing_fibre.geojson ===")
bf_fibre = []
for i, f in enumerate(subset_feeder):
    p = f["properties"]
    g = f["geometry"]
    coords = g.get("coordinates", [])
    if g["type"] == "MultiLineString":
        flat = []
        for ln in coords:
            flat.extend(ln)
        coords = flat
    if len(coords) < 2:
        continue
    duct = bf_ducts[i] if i < len(bf_ducts) else None
    if duct and duct["properties"]["CAPACITY_USED"] >= duct["properties"]["CAPACITY_TOTAL"]:
        continue
    strands_total = random.choice([48, 96, 144])
    strands_used = random.randint(0, strands_total // 2)
    bf_fibre.append(gj_line(coords, {
        "SRC_ID": f"BF_FIBRE_{i:03d}",
        "STAGE": "brownfield",
        "ASSET_TYPE": "fibre",
        "INFRA_STATUS": "Existing",
        "VERIFY_STATUS": random.choice(["Verified","Verified","Assumed"]),
        "CAPACITY_USED": strands_used,
        "CAPACITY_TOTAL": strands_total,
        "POLYGON_ID": p.get("POLYGON_ID", ""),
        "PDP_ID": p.get("PDP_ID", ""),
        "REUSE_SOURCE": "",
    }))
json.dump(make_fc(bf_fibre), open("brownfield_existing_fibre.geojson", "w"), indent=2)
print(f"  -> {len(bf_fibre)} fibre segments")


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("  BROWNFIELD — Cloned from HLD Output (GDAL)")
print("=" * 65)
files = [
    ("brownfield_existing_pdps.geojson", bf_pdps),
    ("brownfield_existing_mfg.geojson", bf_mfgs),
    ("brownfield_feeder_trench.geojson", bf_feeder),
    ("brownfield_distribution_trench.geojson", bf_dist),
    ("brownfield_existing_ducts.geojson", bf_ducts),
    ("brownfield_existing_chambers.geojson", bf_chambers),
    ("brownfield_existing_fibre.geojson", bf_fibre),
]
for fn, feats in files:
    print(f"  {fn:48s} {len(feats):4d}")
