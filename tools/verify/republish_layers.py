"""Re-ingest a project's platform layers from the engine's CURRENT run.

`sync_project_layers()` is idempotent on purpose: it **skips any layer already
in the DB**, so a project whose published rows came from an older run can never
refresh itself. That is exactly why project `0dc85304…` kept serving a network
with floating couplers while the engine (and the files on disk) held a clean
one.

This re-fetches the **same layer names the project already has** and overwrites
those rows in place: nothing is deleted first, so any layer the engine cannot
serve keeps the data it had, and the layer set on the page never changes.

Usage:
    cd fiber-backend && set -a && . ../.env && set +a && \
        PYTHONPATH=. python ../tmp/republish_layers.py <project_id> [--apply]
"""
import json
import os
import sys

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from ftth_hld.models import FtthLayer, FtthProject  # noqa: E402
from ftth_hld.pipeline import get_layer_geojson, persist_layer  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    project_id = sys.argv[1]
    apply_changes = "--apply" in sys.argv

    project = FtthProject.objects.filter(project_id=project_id).first()
    if project is None:
        print("!! no FtthProject row for %s" % project_id)
        return 1
    print("project %s  name=%r  status=%s"
          % (project_id, project.name, project.status))

    rows = list(FtthLayer.objects.filter(ftth_project=project)
                .values_list("name", "feature_count"))
    if not rows:
        print("!! project has no persisted layers")
        return 1
    print("layers persisted: %d\n" % len(rows))
    print("%-22s %8s %8s  %s" % ("layer", "before", "engine", "action"))

    changed = []
    for name, before in sorted(rows):
        raw = get_layer_geojson(project_id, name)
        if not raw:
            print("%-22s %8s %8s  keep (engine served nothing)" % (name, before, "-"))
            continue
        try:
            data = json.loads(raw)
        except Exception as exc:
            print("%-22s %8s %8s  keep (bad json: %s)" % (name, before, "-", exc))
            continue
        engine_n = len(data.get("features") or [])
        if not engine_n:
            print("%-22s %8s %8d  keep (empty)" % (name, before, engine_n))
            continue
        if apply_changes:
            n = persist_layer(project_id, name, data)
            action = "OVERWRITTEN -> %d" % n
        else:
            action = "would overwrite (dry run)"
        if engine_n != before:
            changed.append((name, before, engine_n))
        print("%-22s %8s %8d  %s" % (name, before, engine_n, action))

    print("")
    if changed:
        print("layers whose feature count moves:")
        for name, before, after in changed:
            print("  %-22s %6s -> %6d" % (name, before, after))
    else:
        print("no layer changes feature count (geometry may still differ)")

    after = dict(FtthLayer.objects.filter(ftth_project=project)
                 .values_list("name", "feature_count"))
    print("\nDB now: %s" % json.dumps(after, sort_keys=True))
    if not apply_changes:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
