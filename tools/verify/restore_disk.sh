#!/usr/bin/env bash
# Put the 12:47-era baseline files back into the run's output directory so the
# downloads and the disk fallback serve the same design the map does.
set -u
OUT=HLD_Planning_01/web/backend/outputs/f0426f446acd4b02ada8595e1bb3e3a9
BK=tmp/ref_drifted_latest
mkdir -p "$BK"
cp -f "$OUT"/*.gpkg "$BK"/ 2>/dev/null
cp -f "$OUT"/*.geojson "$BK"/ 2>/dev/null
echo "backed up $(ls "$BK" | wc -l) file(s) -> $BK"

# stem:<snapshot dir>  (see tmp/verify_restore3.py for the provenance proof)
SRC="
Final_Trenches:tmp/ref_before_final2
Feeder_Ducts:tmp/ref_before_final2
Distribution_Ducts:tmp/ref_before_final2
Distribution_Ducts_Runs:tmp/ref_before_final2
Drop_Ducts:tmp/ref_before_region
Feeder_Cable:tmp/ref_before_rerun
Distribution_Cable:tmp/ref_before_final
Coupleurs:tmp/ref_before_region
Feeder_Ducts_Runs:tmp/ref_before_rerun
"
for entry in $SRC; do
  stem="${entry%%:*}"; dir="${entry##*:}"
  src="$dir/$stem.gpkg"
  if [ ! -f "$src" ]; then echo "MISSING $src"; continue; fi
  cp -f "$src" "$OUT/$stem.gpkg"
  ogr2ogr -f GeoJSON -t_srs EPSG:4326 -lco RFC7946=NO "$OUT/$stem.geojson" "$src" || echo "CONVERT FAILED $stem"
  n=$(python -c "import json;print(len(json.load(open(r'$OUT/$stem.geojson',encoding='utf-8'))['features']))")
  echo "  $stem <- $dir   $n feature(s)"
done
