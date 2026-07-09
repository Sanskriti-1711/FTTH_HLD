#!/usr/bin/env python3
"""Parse HLD OneClick log files and produce a per-run timing summary table.

Usage:
    python parse_timings.py <log_file_path>

Parses the structured [timing] lines emitted by the End-to-End HLD Pipeline
and prints a formatted table for each pipeline run, plus a cross-run comparison
when the log contains multiple runs.
"""

import re
import sys
import os

# ── Recognised stage names (in execution order) ──────────────────────────
STAGE_ORDER = [
    "Object Layer",
    "Polygon Layer",
    "Network Layer",
    "Trench Layer",
    "Cable Layer",
    "Duct Layer",
]

# ── Regex patterns ───────────────────────────────────────────────────────
PIPELINE_START = "Algorithm 'One Click – End-to-End HLD Pipeline' starting..."
PIPELINE_END   = "Algorithm 'One Click – End-to-End HLD Pipeline' finished"
PIPELINE_FAIL  = "Execution FAILED after"

TIMESTAMP_RE = re.compile(r"^Algorithm started at: (.+)$")

# Stage  :  "  [timing] Polygon Layer: 850 features, 12.350s"
# Stage  :  "  [timing] Cable Layer: Feeder: 12, Dist: 45 features, 8.150s"
# Stage  :  "  [timing] Object Layer: 15.200s"
STAGE_RE = re.compile(r"^  \[timing\] (.+?): (.+?)([\d.]+)s$")

# Total  :  "[timing] Total pipeline: 120.450s"
TOTAL_RE = re.compile(r"^\[timing\] Total pipeline: ([\d.]+)s$")

# GPKG   :  "  [timing] Polygons.gpkg: skipped (no output dir) in 0.000s"
# GPKG   :  "  [timing] Polygons.gpkg: written to /path/file in 0.234s"
GPKG_RE  = re.compile(
    r"^  \[timing\] (?P<name>.+?\.gpkg): (?P<msg>.+) in (?P<secs>[\d.]+)s$"
)


# ── Parser ───────────────────────────────────────────────────────────────

def parse_log(filepath):
    """Parse *filepath* and return a list of pipeline-run dicts.

    Each run dict has keys:
        timestamp   – ISO datetime string or None
        stages      – list of dicts {name, feature_info, seconds}
        gpkg_writes – list of dicts {name, message, seconds}
        total       – float seconds or None
        failed      – bool
    """
    runs = []
    current = None

    with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n\r")

            # --- pipeline boundaries --------------------------------------
            if PIPELINE_START in line:
                current = {
                    "timestamp": None,
                    "stages": [],
                    "gpkg_writes": [],
                    "total": None,
                    "failed": False,
                }
                runs.append(current)
                continue

            if current is None:
                continue

            if PIPELINE_END in line:
                continue

            if PIPELINE_FAIL in line:
                current["failed"] = True
                continue

            # --- timestamp ------------------------------------------------
            m = TIMESTAMP_RE.match(line)
            if m:
                current["timestamp"] = m.group(1)
                continue

            # --- total pipeline -------------------------------------------
            m = TOTAL_RE.match(line)
            if m:
                current["total"] = float(m.group(1))
                continue

            # --- GPKG write -----------------------------------------------
            m = GPKG_RE.match(line)
            if m:
                current["gpkg_writes"].append({
                    "name":    m.group("name"),
                    "message": m.group("msg").strip(),
                    "seconds": float(m.group("secs")),
                })
                continue

            # --- stage timing ---------------------------------------------
            m = STAGE_RE.match(line)
            if m:
                name   = m.group(1)
                fc_raw = m.group(2).strip().rstrip(",").strip()
                secs   = float(m.group(3))

                if name in STAGE_ORDER:
                    current["stages"].append({
                        "name":         name,
                        "feature_info": fc_raw if fc_raw else None,
                        "seconds":      secs,
                    })
                continue

    return runs


# ── Formatting helpers ───────────────────────────────────────────────────

def _trunc(s, width):
    """Truncate *s* to *width*, appending '…' when shortened."""
    if len(s) <= width:
        return s
    return s[:width - 1] + "…"


def _print_sep(chars=None):
    if chars:
        print(f"  {chars}")
    else:
        print()


def print_run_table(run, idx):
    """Pretty-print a single pipeline run."""
    ts     = run["timestamp"] or "unknown"
    status = "FAILED" if run["failed"] else "OK"

    print()
    print("=" * 80)
    print(f"  Run #{idx}  |  Started: {ts}  |  Status: {status}")
    print("=" * 80)

    # Header
    print(f"  {'Stage':<25} {'Features':<38} {'Time':>10}")
    _print_sep("-" * 25 + " " + "-" * 38 + " " + "-" * 10)

    stage_total = 0.0
    for s in run["stages"]:
        fc = s["feature_info"] or ""
        print(f"  {s['name']:<25} {fc:<38} {s['seconds']:>9.3f}s")
        stage_total += s["seconds"]

    # GPKG writes (if any with out_dir)
    gpkg_shown = [g for g in run["gpkg_writes"] if "skipped" not in g["message"]]
    if gpkg_shown:
        _print_sep("-" * 25 + " " + "-" * 38 + " " + "-" * 10)
        gpkg_total = 0.0
        for g in gpkg_shown:
            short = _trunc(g["message"], 38)
            print(f"  {g['name']:<25} {short:<38} {g['seconds']:>9.3f}s")
            gpkg_total += g["seconds"]
        print(f"  {'(GPKG subtotal)':<25} {'':<38} {gpkg_total:>9.3f}s")

    _print_sep("-" * 25 + " " + "-" * 38 + " " + "-" * 10)
    total = run["total"] if run["total"] is not None else stage_total
    print(f"  {'PIPELINE TOTAL':<25} {'':<38} {total:>9.3f}s")


def print_summary(runs):
    """Print a cross-run comparison table when > 1 runs exist."""
    if len(runs) < 2:
        return

    n = len(runs)
    col_w = 13  # column width per run

    print()
    print("=" * 80)
    print("  CROSS-RUN COMPARISON")
    print("=" * 80)

    # Header
    header = f"  {'Stage':<25}"
    for i in range(n):
        header += f" {'Run #' + str(i + 1):>{col_w}}"
    print(header)
    _print_sep("-" * 25 + " " + "-" * col_w * n)

    # Per-stage rows
    for stage_name in STAGE_ORDER:
        row = f"  {stage_name:<25}"
        for run in runs:
            match = [s for s in run["stages"] if s["name"] == stage_name]
            if match:
                row += f" {match[0]['seconds']:>{col_w - 1}.3f}s"
            else:
                row += f" {'—':>{col_w}}"
        print(row)

    # Total row
    _print_sep("-" * 25 + " " + "-" * col_w * n)
    row = f"  {'TOTAL':<25}"
    for run in runs:
        t = run["total"] if run["total"] is not None else sum(
            s["seconds"] for s in run["stages"]
        )
        row += f" {t:>{col_w - 1}.3f}s"
    print(row)
    print()


# ── Entry point ──────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python parse_timings.py <log_file_path>")
        print()
        print("  Parses OneClick HLD pipeline log files and prints per-run")
        print("  timing summary tables with feature counts and totals.")
        sys.exit(1)

    filepath = sys.argv[1]
    if not os.path.isfile(filepath):
        print(f"Error: file not found – {filepath}")
        sys.exit(1)

    runs = parse_log(filepath)

    if not runs:
        print("No pipeline runs found in the log file.")
        sys.exit(0)

    for i, run in enumerate(runs, 1):
        print_run_table(run, i)

    print_summary(runs)


if __name__ == "__main__":
    main()
