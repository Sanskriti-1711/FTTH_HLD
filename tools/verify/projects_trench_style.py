"""Which projects serve DESIGNER trenches and which still serve the legacy set?

The designer publishes one span per chamber-to-chamber part (hundreds of
features). The old trench stage published ONE merged multi-line per construction
class (typically 3-5). That difference is visible straight from the served
payload, so every project can be classified without opening a map.

Usage: python tmp/projects_trench_style.py
"""
import json
import os
import urllib.request

BASE = "http://localhost:8000"
TOKEN = open("tmp/token_sub.txt").read().strip()
CACHE = "tmp/apicache"


def get(path):
    req = urllib.request.Request(BASE + path)
    req.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def layer(pid, name):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    os.makedirs(os.path.join(CACHE, pid), exist_ok=True)
    fp = os.path.join(CACHE, pid, safe + ".json")
    if os.path.exists(fp) and os.path.getsize(fp) > 0:
        try:
            return json.load(open(fp, encoding="utf-8"))
        except Exception:
            pass
    try:
        gj = get("/api/ftth/hld/results/%s/layers/%s/" % (pid, name))
    except Exception as exc:
        gj = {"__error__": str(exc)}
    json.dump(gj, open(fp, "w", encoding="utf-8"))
    return gj


def main():
    projects = get("/api/ftth/hld/projects/")
    print("%-34s %-26s %10s %10s  %s" %
          ("project", "name", "trenches", "ducts", "trench publishing style"))
    for p in projects:
        pid = p.get("project_id")
        name = (p.get("name") or "")[:24]
        try:
            st = get("/api/ftth/hld/results/%s/" % pid)
        except Exception as exc:
            print("%-34s %-26s  status error: %s" % (pid, name, exc))
            continue
        names = [(l.get("name") or "") for l in (st.get("layers") or [])]
        if "trenches" not in names:
            print("%-34s %-26s %10s %10s  (no trench layer served)" %
                  (pid, name, "-", "-"))
            continue
        tj = layer(pid, "trenches")
        tn = len(tj.get("features") or []) if isinstance(tj, dict) else 0
        dn = "-"
        if "ducts" in names:
            dj = layer(pid, "ducts")
            dn = len(dj.get("features") or []) if isinstance(dj, dict) else "-"
        style = ("designer (per-span network)" if tn >= 50
                 else "LEGACY (merged per class) <== old pipeline trenches")
        print("%-34s %-26s %10s %10s  %s" % (pid, name, tn, dn, style))


if __name__ == "__main__":
    main()
