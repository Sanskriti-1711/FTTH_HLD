"""Engine-served layer counts vs the platform's persisted rows.

The engine re-exports a layer's GeoJSON lazily when its GeoPackage is newer, so
a republish that runs while a file is being rewritten can persist a partial
layer. This says which layers disagree, so a republish can be repeated until
they match.

Usage:
    cd fiber-backend && set -a && . ../.env && set +a && \
        PYTHONPATH=. python ../tmp/compare_served.py <project_id>
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from ftth_hld.models import FtthLayer, FtthProject  # noqa: E402

ENGINE = os.environ.get("FTTH_ENGINE_URL", "http://127.0.0.1:8080")


def engine_count(pid, layer):
    url = "%s/ftth/hld/results/%s/layers/%s" % (ENGINE, pid, layer)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        return "err(%s)" % type(exc).__name__
    if isinstance(d, dict):
        return len(d.get("features") or [])
    return "?"


def main():
    pid = sys.argv[1]
    project = FtthProject.objects.filter(project_id=pid).first()
    if project is None:
        print("no project %s" % pid)
        return 1
    print("project status in DB: %s" % project.status)
    bad = []
    print("%-20s %8s %8s  %s" % ("layer", "engine", "DB", "verdict"))
    for row in FtthLayer.objects.filter(ftth_project=project).order_by("name"):
        eng = engine_count(pid, row.name)
        same = (eng == row.feature_count)
        if not same:
            bad.append((row.name, eng, row.feature_count))
        print("%-20s %8s %8s  %s" % (row.name, eng, row.feature_count,
                                     "ok" if same else "MISMATCH"))
    print()
    if bad:
        print("layers to re-publish (engine != DB): %s"
              % ", ".join(b[0] for b in bad))
    else:
        print("all layers match the engine")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
