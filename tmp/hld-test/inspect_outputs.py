import json
import os
import sys
from osgeo import ogr

EXPECTED_LAYERS = [
    'Distribution_Cable.gpkg', 'Distribution_Ducts.gpkg', 'Feeder_Cable.gpkg',
    'Feeder_Ducts.gpkg', 'Final_Trenches.gpkg', 'MFG.gpkg', 'Network.gpkg',
    'Objects.gpkg', 'PDPs.gpkg', 'Polygons.gpkg',
]
SAMPLE_ATTR_KEYS = [
    'ADDR_ID', 'Address', 'HH', 'POLYGON_ID', 'PDP_ID', 'MFG_ID', 'mfg_id',
    'pdp_id', 'pDp_POL_ID', 'duct_uid', 'duct_idx', 'hh_count',
    'edge_cnt', 'length_m', 'color', 'part', 'SRC_ID', 'STAGE',
]
WKT_TRUNC = 130


def truncate_text(text: str, cap: int = WKT_TRUNC) -> str:
    return text if len(text) <= cap else text[: cap - 1] + '\u2026'


def resolve_pid() -> str:
    pid = os.environ.get('LAST_PID') or (sys.argv[1] if len(sys.argv) > 1 else '')
    pid = pid.strip()
    if not pid:
        print(f'usage: {sys.argv[0]} <project_id>  (or set LAST_PID env)', file=sys.stderr)
        sys.exit(2)
    return pid


def run() -> int:
    pid = resolve_pid()
    base = f'/app/web/backend/outputs/{pid}'
    print(f'# project_id     : {pid}')
    print(f'# task_dir       : {base}')
    if not os.path.isdir(base):
        print(f'NO TASK DIR: {base}')
        return 1

    # Optional: surface polling status from /tmp/hld/final.json if available.
    final = None
    try:
        with open('/tmp/hld/final.json') as f:
            final = json.load(f)
    except (OSError, json.JSONDecodeError):
        final = None
    if isinstance(final, dict):
        print(f'# pipeline status: {final.get("status")} (timed_out={final.get("timed_out", False)})')
        if final.get('timed_out'):
            print('# warning: polling never reached a terminal state; outputs may be partial')

    files = sorted(f for f in os.listdir(base) if f.endswith('.gpkg'))
    print(f'\nfound {len(files)} gpkg files:')
    for f in files:
        print(f'  - {f}')
    print('missing:', [e for e in EXPECTED_LAYERS if e not in files])
    print('extras :', [f for f in files if f not in EXPECTED_LAYERS])

    for name in EXPECTED_LAYERS:
        path = os.path.join(base, name)
        print(f'\n[ {name} ]')
        if not os.path.exists(path):
            print('  MISSING')
            continue
        try:
            ds = ogr.Open(path)
        except Exception as e:
            print(f'  ogr.Open failed: {e}')
            continue
        if ds is None:
            print('  ogr.Open returned None')
            continue
        lyr = ds.GetLayer(0)
        if lyr is None:
            print('  layer[0] is None')
            ds = None
            continue
        defn = lyr.GetLayerDefn()
        cnt = lyr.GetFeatureCount()
        geom_name = ogr.GeometryTypeToName(defn.GetGeomType())
        print(f'  features : {cnt}')
        print(f'  geom_type: {geom_name}')
        print(f'  fields   : {defn.GetFieldCount()}')
        for i in range(defn.GetFieldCount()):
            fd = defn.GetFieldDefn(i)
            print(f'    {fd.GetName():24s} {fd.GetTypeName():14s}')
        f = lyr.GetNextFeature()
        if f and f.GetGeometryRef() and not f.GetGeometryRef().IsEmpty():
            wkt = f.GetGeometryRef().ExportToWkt()
            print(f'  sample WKT : {truncate_text(wkt)}')
            sample_keys = []
            for k in SAMPLE_ATTR_KEYS:
                try:
                    v_ = f.GetField(k)
                except (KeyError, ValueError, IndexError):
                    v_ = None
                if v_ is None or v_ == '':
                    continue
                sample_keys.append(f'{k}={v_!r}')
            if sample_keys:
                print(f'  sample attrs: {", ".join(sample_keys[:8])}')
        ds = None
    return 0


if __name__ == '__main__':
    sys.exit(run())
