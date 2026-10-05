"""Blast-radius pre-check before re-running an HLD project on the designer.

Re-running rewrites the project's published HLD layers, so anything frozen
downstream (Approved Survey Version, LLD runs) and anything *linked* to the old
HLD geometry (the survey copy project's Feature rows / SurveyFeature rows) has
to be counted before anything is touched.

Usage:
    cd fiber-backend && set -a && . ../.env && set +a && PYTHONPATH=. \
        python ../tmp/precheck_rerun.py <ftth_project_id>
"""
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.db.models import Count  # noqa: E402

from ftth_hld.models import FtthLayer, FtthProject  # noqa: E402
from ftth_lld.models import ApprovedSurveyVersion, LldLayer, LldRun  # noqa: E402
from projects.models import Feature, Project  # noqa: E402
from projects.models.stage_event import StageEvent  # noqa: E402
from survey.models import ApprovalRecord, SurveyFeature  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pid = sys.argv[1]

    fp = FtthProject.objects.filter(project_id=pid).first()
    if fp is None:
        print("!! no FtthProject for %s" % pid)
        return 1

    print("=" * 72)
    print("FtthProject %s" % pid)
    print("=" * 72)
    print("  name          : %r" % fp.name)
    print("  status        : %s   stage=%s (%s/%s) progress=%s%%"
          % (fp.status, fp.stage_name, fp.stage_index, fp.stage_count, fp.progress))
    print("  created       : %s" % fp.created_at)
    print("  completed_at  : %s" % fp.completed_at)
    print("  assigned_to   : %s" % (fp.assigned_engineer_id or "-"))
    print("  excel         : %r" % fp.excel_filename)
    print("  roads         : %r" % fp.roads_filename)
    print("  error         : %r" % (fp.error_message or "")[:160])

    # ---- published layers ------------------------------------------------
    layers = dict(FtthLayer.objects.filter(ftth_project=fp)
                  .values_list("name", "feature_count"))
    print("\n-- published layers (%d) --" % len(layers))
    for name in sorted(layers):
        print("   %-24s %8s" % (name, layers[name]))

    trunk = layers.get("trenches")
    era = ("designer/span-network" if (trunk or 0) > 50
           else "LEGACY (one merged feature per class)")
    print("\n  trench-era read: trenches=%s -> %s" % (trunk, era))

    # ---- frozen downstream -----------------------------------------------
    asvs = list(ApprovedSurveyVersion.objects.filter(ftth_project=fp)
                .values_list("version", "hld_version", "created_at")
                .order_by("created_at"))
    runs = list(LldRun.objects.filter(ftth_project=fp)
                .values_list("lld_version", "status", "run_date",
                             "approved_survey_version__version")
                .order_by("run_date"))
    print("\n-- frozen downstream --")
    print("  ApprovedSurveyVersion : %d %s" % (len(asvs), asvs if asvs else ""))
    print("     (version, hld_version, frozen_at)")
    print("  LLD runs              : %d" % len(runs))
    for r in runs:
        print("     %-10s %-10s %s  (ASV %s)" % (r[0], r[1], r[2], r[3]))
    print("  LLD output layers     : %d"
          % LldLayer.objects.filter(lld_run__ftth_project=fp).count())

    # ---- survey copy + survey edits --------------------------------------
    copies = list(Project.objects.filter(source_ftth_project_id=pid))
    print("\n-- survey linkage --")
    print("  survey copy projects  : %d" % len(copies))
    for c in copies:
        sf = SurveyFeature.objects.filter(project=c)
        by_status = dict(Counter(sf.values_list("survey_status", flat=True)))
        approved = SurveyFeature.objects.filter(
            project=c, survey_status=SurveyFeature.SurveyStatus.APPROVED).count()
        print("     %s  %r  status=%s  completion=%s%%"
              % (c.id, c.name, c.status, c.standard_completion))
        print("       SurveyFeature rows : %d  by status %s"
              % (sf.count(), json.dumps(by_status, sort_keys=True)))
        print("       ApprovalRecord rows: %d"
              % ApprovalRecord.objects.filter(survey_feature__in=sf).count())
        print("       approved features  : %d" % approved)
        linked = SurveyFeature.objects.filter(
            project=c, original_hld_feature__isnull=False).count()
        orphan = SurveyFeature.objects.filter(
            project=c, survey_status__in=["approved", "modified", "new", "removed"],
            original_hld_feature__isnull=True).count()
        print("       linked -> HLD Feature: %d   (unlinked-but-active: %d)"
              % (linked, orphan))
        print("       stage events       : %d"
              % StageEvent.objects.filter(project=c).count())

    # ---- the risk: HLD Feature rows the survey copy points at -------------
    print("\n-- HLD Feature rows reachable from the survey copy --")
    for c in copies:
        print("  projects.Feature rows on copy %s : %d"
              % (c.id, Feature.objects.filter(project_id=c.id).count()))

    risk = []
    if asvs:
        risk.append("%d Approved Survey Version(s) were frozen from the CURRENT HLD"
                    % len(asvs))
    if runs:
        risk.append("%d LLD run(s) reference the CURRENT HLD"
                    % len(runs))
    for c in copies:
        if c.status in ("submitted", "under_review", "reviewed", "accepted", "redo", "active"):
            risk.append("survey copy %s is %s (engineer work in flight)"
                        % (c.id, c.status))
    print("\n" + "=" * 72)
    if risk:
        print("BLAST RADIUS — %d item(s):" % len(risk))
        for r in risk:
            print("  ! %s" % r)
    else:
        print("BLAST RADIUS — CLEAN: nothing frozen or in-flight depends on the "
              "current HLD")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
