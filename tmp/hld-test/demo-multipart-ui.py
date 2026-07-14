"""
Drive the live HLD Planner React app through the multipart shp+shx+dbf
upload path \u2014 via headless Chrome + raw CDP, because the inline
`browser-use` tool's `upload_file` only accepts a single filePath at a time.

Why this script vs. browser-use:
  The chrome-devtools `upload_file` tool routes through Page.setFileInputFiles
  internally but exposes only a single-string `filePath` parameter. For our
  Roads dropzone we need FOUR files (shp + shx + dbf + prj) attached to ONE
  `<input type=file multiple>` in ONE CDP call. That requires driving CDP
  ourselves.

What this script does, end-to-end:
  1. Launches headless Chrome on `--remote-debugging-port=9333`.
  2. Connects to the first page target via websocket-client.
  3. Navigates to http://localhost:5173/.
  4. Polls until React mounts (the Roads `<input[type=file][multiple]>` exists).
  5. Locates the two file inputs via DOM.getDocument + DOM.querySelector.
     - Roads input:   `.sidebar-section:nth-of-type(2) input[type="file"][multiple]`
     - Address input: `input[type="file"]:not([multiple])`
  6. Sends `DOM.setFileInputFiles` with FOUR absolute paths for the Roads
     input (single CDP call \u2014 the multipart bundle upload contract).
  7. Polls `.roads-status.ok-bundle` until the green chip appears (or
     surfaces the structured error chip if /upload-roads returned 400).
  8. Saves screenshot `01-after-roads-upload.png`.
  9. Sends `DOM.setFileInputFiles` with the single xlsx path for the Address
     input.
 10. Polls `.dz-file` for the Excel filename label.
 11. Saves screenshot `02-both-files-selected.png`.
 12. Clicks the primary "Generate HLD" button via Runtime.evaluate (only if
     not disabled).
 13. Polls `.status-panel` until the badge reads `Done` / `Failed` / `Stopped`
     or 2 minutes elapsed.
 14. Saves screenshot `03-after-generate.png`.
 15. Prints the final state JSON: title, badge, task_id, runner tag, downloads,
     layer names.
 16. Closes Chrome, removes the user-data-dir.

Pre-flight (required before running this script):
  - `tmp/hld-test/make-fixtures.py` has been run \u2014 it synthesizes the four
    shapefile sidecars via ogr2ogr + copies a real xlsx into
    `tmp/hld-test/fixtures/`.
  - `web/backend` uvicorn is running at :8000 (we skip the check; the React
    health probe will surface a failure if it isn't).
  - `web/frontend` Vite dev server is running at :5173.

Output (verbose so we can verify each step):
  - stdout: per-step chip text, status polls (every ~1.0s during Generate)
  - `tmp/hld-test/screenshots/`: 3 PNGs
  - `tmp/hld-test/logs/chrome.log`: chrome --headless stderr
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import websocket  # `pip install websocket-client` (already in this env)

# --- Windows console encoding ----------------------------------------------
# The default Windows ANSI codec (cp1252) cannot encode `\u2713` (`✓`) or
# `\u00b7` (`·`) which appear in the green-chip text and pipeline log.
# Reconfiguring stdout to UTF-8 prevents UnicodeEncodeError on print().
# Safe on all platforms (no-op on POSIX UTF-8 TTYs).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------- Paths / config ----------

PROJECT = Path(__file__).resolve().parents[2]
FIX_DIR = PROJECT / "tmp" / "hld-test" / "fixtures"
SHOTS_DIR = PROJECT / "tmp" / "hld-test" / "screenshots"
LOG_DIR = PROJECT / "tmp" / "hld-test" / "logs"
for d in (FIX_DIR, SHOTS_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEBUG_PORT = 9333
URL = "http://localhost:5173/"

# 4-file bundle for the Roads dropzone (shp + shx + dbf + prj).
# Use forward-slash form: CDP on Windows accepts both, but Playwright-style
# forward slashes avoid quoting issues in the JSON wire payload.
ROADS_FILES = [
    (FIX_DIR / "test_roads.shp").as_posix(),
    (FIX_DIR / "test_roads.shx").as_posix(),
    (FIX_DIR / "test_roads.dbf").as_posix(),
    (FIX_DIR / "test_roads.prj").as_posix(),
]
ADDR_FILES = [(FIX_DIR / "test_addresses.xlsx").as_posix()]


# ---------- Minimal CDP client ----------

class CDP:
    """Round-trip a single CDP command and ignore non-matching events.

    Designed for our 4-step set-and-poll flow (Page.navigate, setFileInputFiles,
    Runtime.evaluate, Page.captureScreenshot) where we don't care about
    browser-driven events. We just drain the socket until we see a message
    whose `id` matches the request we just sent.
    """

    def __init__(self, ws_url: str, recv_timeout: float = 30.0):
        self.ws = websocket.create_connection(ws_url, timeout=recv_timeout)
        self.id = 0
        print(f"[cdp] connected to {ws_url}", flush=True)

    def send(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self.id += 1
        payload = {"id": self.id, "method": method, "params": params or {}}
        self.ws.send(json.dumps(payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.2, deadline - time.time()))
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketConnectionClosedException:
                raise RuntimeError(f"CDP connection closed before reply to {method}")
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("id") == self.id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} error: {msg['error']}")
                return msg.get("result", {})
            # else: a server-pushed event; ignore
        raise TimeoutError(f"CDP {method} timeout after {timeout}s")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# ---------- Chrome lifecycle ----------

def launch_headless_chrome(port: int, user_data_dir: str, log_path: Path) -> subprocess.Popen:
    args = [
        CHROME_EXE,
        "--headless=new",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        # Chrome 111+ refuses DevTools WS handshakes from origins not in this
        # allowlist. Local Python websocket-client is not a browser origin, so
        # without this flag the WS handshake fails with HTTP 403 Forbidden.
        # Single-user dev box only; tighten on shared hosts.
        "--remote-allow-origins=*",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-features=Translate,InfiniteSessionRestore,ImprovedFlashControls",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1440,900",
        "--hide-scrollbars",
        "about:blank",
    ]
    print(f"[chrome] launching: {' '.join(args[:4])} ... (full args hidden)", flush=True)
    log_fh = open(log_path, "wb")
    # CREATE_NO_WINDOW: 0x08000000 \u2014 suppress console flash on Windows when this
    # script is launched from a terminal that has a console.
    proc = subprocess.Popen(
        args,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        creationflags=0x08000000,
    )
    return proc


def wait_for_debug_port(port: int, retries: int = 80) -> bool:
    for i in range(retries):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
            print(f"[chrome] debug port ready after {i * 0.25:.2f}s", flush=True)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def attach_to_page(port: int) -> str:
    raw = urllib.request.urlopen(f"http://127.0.0.1:{port}/json").read()
    pages = json.loads(raw)
    for p in pages:
        if p.get("type") == "page":
            print(f"[cdp] attaching to page: {p.get('title')!r} url={p.get('url')!r}", flush=True)
            return p["webSocketDebuggerUrl"]
    raise RuntimeError("no page target on Chrome debug port")


# ---------- Helpers around Runtime.evaluate ----------

def eval_js(cdp: CDP, expression: str, await_promise: bool = False) -> Any:
    """Run an expression and return the unwrapped JS value as a Python primitive.

    The CDP envelope for Runtime.evaluate is shaped as
        { "id": <id>, "result": <RemoteObject> }
    where <RemoteObject> is one of
        { "type": "string",   "value": "..." }
        { "type": "boolean",  "value": true | false }
        { "type": "number",   "value": 0 | 3.14 | ... }
        { "type": "object",   "value": <serialized> }
        { "type": "undefined" }                       (no "value" key)
    When the page-side throws, the envelope carries `exceptionDetails` AND a
    result with `subtype == "error"`.
    """
    r = cdp.send(
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        },
    )
    res = r.get("result", {})
    if "exceptionDetails" in res:
        raise RuntimeError(f"Runtime.evaluate exception: {res.get('exceptionDetails')}")
    if res.get("subtype") == "error":
        raise RuntimeError(f"Runtime.evaluate error: {res.get('description', res)}")
    # No further nesting — `res` IS the RemoteObject, with "value" directly.
    return res.get("value")


def screenshot(cdp: CDP, path: Path) -> int:
    r = cdp.send("Page.captureScreenshot", {"format": "png"})
    data = base64.b64decode(r["data"])
    path.write_bytes(data)
    return path.stat().st_size


# ---------- The demo flow ----------

def main() -> int:
    if not Path(CHROME_EXE).exists():
        print(f"[error] Chrome not found at {CHROME_EXE}", flush=True)
        return 1
    for p in ROADS_FILES + ADDR_FILES:
        if not Path(p).exists():
            print(f"[error] missing fixture: {p}; run make-fixtures.py first", flush=True)
            return 2

    user_data = tempfile.mkdtemp(prefix="hld-demo-chrome-")
    chrome = None
    try:
        chrome = launch_headless_chrome(DEBUG_PORT, user_data, LOG_DIR / "chrome.log")
        if not wait_for_debug_port(DEBUG_PORT):
            print("[error] Chrome failed to expose debug port", flush=True)
            return 3

        cdp = CDP(attach_to_page(DEBUG_PORT))
        cdp.send("Page.enable")

        # 1. Navigate
        print(f"[ui] navigating to {URL}", flush=True)
        cdp.send("Page.navigate", {"url": URL})

        # 2. Wait for readyState = complete
        for _ in range(120):  # ~30s
            v = eval_js(cdp, "document.readyState")
            if v == "complete":
                break
            time.sleep(0.25)
        print(f"[ui] document.readyState: {eval_js(cdp, 'document.readyState')!r}", flush=True)

        # 3. Wait for React mount (the upload dropzones exist)
        for i in range(120):  # ~30s
            v = eval_js(
                cdp,
                "!!document.querySelector('input[type=\"file\"][multiple]') && !!document.querySelector('input[type=\"file\"]:not([multiple])')",
            )
            if v:
                print(f"[ui] both file inputs mounted after {i * 0.25:.2f}s", flush=True)
                break
            time.sleep(0.25)

        # 4. Locate file inputs via DOM (need nodeId for setFileInputFiles)
        doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
        root_node_id = doc["root"]["nodeId"]

        # Roads input: uniquely identified by the [multiple] attribute, since
        # only the Roads dropzone is multi-file (the Excel dropzone is single).
        # Avoids brittleness around sidebar-section ordering
        # (Project=1st, Address List=2nd, Road Network=3rd, Generate=4th).
        r_q = cdp.send(
            "DOM.querySelector",
            {
                "nodeId": root_node_id,
                "selector": "input[type=\"file\"][multiple]",
            },
        )
        roads_node = r_q.get("nodeId")
        print(f"[dom] roads file-input nodeId: {roads_node}", flush=True)
        if not roads_node:
            print("[error] cannot locate Roads multi-file input", flush=True)
            return 4

        a_q = cdp.send(
            "DOM.querySelector",
            {
                "nodeId": root_node_id,
                "selector": "input[type=\"file\"]:not([multiple])",
            },
        )
        addr_node = a_q.get("nodeId")
        print(f"[dom] address file-input nodeId: {addr_node}", flush=True)

        # 5. SET 4 FILES IN ONE CDP CALL \u2014 the multipart upload contract.
        # This is THE moment browser-use's `upload_file` couldn't deliver.
        print("[upload] DOM.setFileInputFiles with 4 Roads files (single CDP call):", flush=True)
        for p in ROADS_FILES:
            print(f"           - {p}", flush=True)
        cdp.send("DOM.setFileInputFiles", {"nodeId": roads_node, "files": ROADS_FILES})

        # 6. Poll until the green chip or a structured error chip appears.
        chip = None
        for i in range(60):  # ~30s
            v = eval_js(
                cdp,
                "(function(){"
                "const el = document.querySelector('.roads-status');"
                "if (!el) return JSON.stringify({state:'missing'});"
                "const cls = (el.className||'').toString();"
                "if (cls.includes('ok-bundle')) return JSON.stringify({state:'ok', text: el.innerText.trim()});"
                "if (cls.includes('roads-status') && (cls.includes('error') || cls.includes('warning')))"
                "  return JSON.stringify({state:'error', text: el.innerText.trim()});"
                "if (cls.includes('busy')) return JSON.stringify({state:'busy'});"
                "return JSON.stringify({state:'unknown', classes: cls});"
                "})()",
            )
            try:
                data = json.loads(v) if isinstance(v, str) else v
            except Exception:
                data = {"state": "parse_err", "raw": str(v)}
            if data.get("state") in ("ok", "error"):
                chip = data
                print(f"[chip] resolved after {(i + 1) * 0.5:.1f}s -> {chip}", flush=True)
                break
            time.sleep(0.5)
        if chip is None:
            print("[error] chip never resolved; aborting", flush=True)
            return 5

        # 7. Screenshot #1
        n = screenshot(cdp, SHOTS_DIR / "01-after-roads-upload.png")
        print(f"[shot] 01-after-roads-upload.png  ({n} bytes)", flush=True)

        if chip["state"] != "ok":
            print(f"[error] Roads validation chip is not OK: {chip}", flush=True)
            # Capture the full chip HTML so we know what structured field popped up.
            html = eval_js(cdp, "document.querySelector('.roads-status')?.outerHTML || ''")
            print(f"[error] chip HTML: {html[:600]!r}", flush=True)
            return 6

        # 8. SET the Excel file (single file via setFileInputFiles)
        print(f"[upload] DOM.setFileInputFiles with 1 Excel file: {ADDR_FILES[0]}", flush=True)
        cdp.send("DOM.setFileInputFiles", {"nodeId": addr_node, "files": ADDR_FILES})
        # Wait for filename label inside dropzone
        for i in range(30):
            v = eval_js(
                cdp,
                "(document.querySelector('.sidebar-section:nth-of-type(1) .dz-file')?.innerText || '').trim()",
            )
            if v and '.xlsx' in v.lower():
                print(f"[excel] dropzone label: {v!r}", flush=True)
                break
            time.sleep(0.25)

        # 9. Screenshot #2
        n = screenshot(cdp, SHOTS_DIR / "02-both-files-selected.png")
        print(f"[shot] 02-both-files-selected.png  ({n} bytes)", flush=True)

        # 10. Click Generate (only if button is enabled)
        print("[generate] clicking the 'Generate HLD' button", flush=True)
        click_state = eval_js(
            cdp,
            "(function(){"
            "const btn = document.querySelector('button.btn.btn-primary.btn-wide');"
            "if (!btn) return JSON.stringify({state:'no_button'});"
            "if (btn.disabled) return JSON.stringify({state:'disabled', text: btn.innerText.trim().slice(0,80)});"
            "btn.click();"
            "return JSON.stringify({state:'clicked', text: btn.innerText.trim().slice(0,80)});"
            "})()",
        )
        print(f"[generate] click state: {click_state}", flush=True)
        if isinstance(click_state, str):
            try:
                click_state = json.loads(click_state)
            except Exception:
                pass
        if not isinstance(click_state, dict) or click_state.get("state") != "clicked":
            print("[error] could not click Generate", flush=True)
            return 7

        # 11. Poll status panel until 'Done' / 'Failed' / 'Stopped' OR 3 min.
        print("[status] polling .status-panel until terminal state", flush=True)
        final_state = None
        for i in range(180):  # 3 min
            panel = eval_js(
                cdp,
                "(function(){"
                "const sp = document.querySelector('.status-panel');"
                "if (!sp) return JSON.stringify({state:'no_panel'});"
                "  return JSON.stringify({"
                "    state: 'panel',"
                "    title: sp.querySelector('.status-title')?.innerText.trim() || '',"
                "    badge: sp.querySelector('.status-badge')?.innerText.trim() || '',"
                "    stage: sp.querySelector('.status-detail.stage-line .stage-name')?.innerText.trim() || '',"
                "    pct:   sp.querySelector('.progress-bar')?.getAttribute('aria-valuenow') || '',"
                "  });"
                "})()",
            )
            try:
                panel = json.loads(panel) if isinstance(panel, str) else panel
            except Exception:
                panel = {"state": "parse_err"}
            badge = (panel or {}).get("badge", "")
            stage = (panel or {}).get("stage", "")
            pct = (panel or {}).get("pct", "")
            print(f"  [poll {i:>3}] badge={badge!r}  stage={stage!r}  pct={pct!r}", flush=True)
            if badge in ("Done", "Failed"):
                final_state = panel
                break
            time.sleep(1.0)

        # 12. Screenshot #3
        n = screenshot(cdp, SHOTS_DIR / "03-after-generate.png")
        print(f"[shot] 03-after-generate.png  ({n} bytes)", flush=True)

        # 13. Final state JSON
        final = eval_js(
            cdp,
            "(function(){"
            "const sp = document.querySelector('.status-panel');"
            "const title = sp?.querySelector('.status-title')?.innerText.trim() || '(no panel)';"
            "const badge = sp?.querySelector('.status-badge')?.innerText.trim() || '(none)';"
            "const task_id = sp?.querySelector('.task-id')?.innerText.trim() || '';"
            "const runner = sp?.querySelector('.runner-tag')?.innerText.trim() || '';"
            "const error = sp?.querySelector('.status-error')?.innerText.trim() || '';"
            "const downloads = Array.from(sp?.querySelectorAll('.download-link .dl-name') || []).map(e=>e.innerText.trim());"
            "const layers = Array.from(document.querySelectorAll('.layer-card .layer-name') || []).map(e=>e.innerText.trim());"
            "return JSON.stringify({title, badge, task_id, runner, error, downloads, layers});"
            "})()",
        )
        try:
            final_obj = json.loads(final) if isinstance(final, str) else final
        except Exception:
            final_obj = {"raw": str(final)}
        print("\n[final]\n" + json.dumps(final_obj, indent=2), flush=True)

        cdp.close()
    finally:
        if chrome is not None:
            try:
                chrome.terminate()
                chrome.wait(timeout=5)
            except Exception:
                try:
                    chrome.kill()
                except Exception:
                    pass
        shutil.rmtree(user_data, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
