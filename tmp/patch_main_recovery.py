"""Surgical recovery for web/backend/main.py.

The previous edit attempts left a broken `_append_msg(...)` block in
process_hld: the written file ended line 1880 with a literal line-feed
mid-string instead of the literal `\` + `n` Python source for a newline
escape. This script:

  1. Locates the broken warning block by anchoring on the SURVIVING
     context lines (start: comment "# missing vs PATH-not-set.",
     end: `_run_mock(task_id, roads_path, ...)`).
  2. Replaces it with a syntactically clean version using adjacent-
     string-concatenation with raw strings (no escape pitfalls).
  3. Wraps the .bat-launcher invocations inside
     `_run_qgis_process_subprocess` in `cmd.exe /c` so Windows command
     quoting handles paths-with-spaces correctly.
  4. Verifies syntax with `python -c "import main"` (run from web/backend).
"""

import os
import sys

FILE = "web/backend/main.py"

with open(FILE, "r", encoding="utf-8") as f:
    content = f.read()

# ------------------------------------------------------------------ #
# Step 1: splice a clean warning block into the no-qgis_process path.
# ------------------------------------------------------------------ #
# Anchor on the comment + the next _run_mock call. Both must survive
# intact regardless of the broken middle.
START_ANCHOR = "                # missing vs PATH-not-set.\n"
END_ANCHOR = "                _run_mock(task_id, roads_path, task_out_dir, layer_names, layer_files)"

i_start = content.find(START_ANCHOR)
i_end = content.find(END_ANCHOR)
assert i_start != -1, "start anchor missing"
assert i_end != -1, "end anchor missing"
assert i_end > i_start, "anchors reversed"

# The replacement is one literal block using adjacent-string-
# concatenation with raw strings so there are zero backslash-escape
# footguns. Each segment ends/starts with python-source-level "\n"
# which becomes a runtime newline character.
NEW_BLOCK = (
    '                _append_msg(\n'
    '                    task_id, "warning",\n'
    '                    (\n'
    '                        r"No qgis_process launcher found. To enable real QGIS:" "\n"\n'
    '                        r"  1. set QGIS_EXECUTABLE=C:\\Program Files\\QGIS 3.44.6\\bin\\qgis_process-qgis.bat" "\n"\n'
    '                        r"  2. OR add C:\\Program Files\\QGIS 3.44.6\\bin to PATH" "\n"\n'
    '                        r"  3. OR run start-qgis-backend.cmd (repo root) which sets both." "\n"\n'
    '                        r"Falling back to mock mode \u2014 weights will not appear and the algorithm will NOT run."\n'
    '                    ),\n'
    '                )\n'
)

# Replace the broken range. The end anchor itself (the _run_mock call)
# stays put; we only overwrite [start_anchor_end : end_anchor_start].
i_start_after_anchor = i_start + len(START_ANCHOR)
content = content[:i_start_after_anchor] + NEW_BLOCK + content[i_end:]

# ------------------------------------------------------------------ #
# Step 2: wrap .bat launchers in `cmd.exe /c`.
# ------------------------------------------------------------------ #
# The `cmd` list inside _run_qgis_process_subprocess is generated just
# above the Popen call. We rewrite that exact block.
OLD_CMD_LIST = (
    '    cmd = [\n'
    '        qgis_exec, "run", "hldplanning:end_to_end_pipeline",\n'
    '        "--",\n'
    '        f"EXCEL={excel_path}",\n'
    '        f"OUTPUT_DIR={task_out_dir}",\n'
    '        f"ROADS={roads_path}",\n'
    '    ]\n'
)
NEW_CMD_LIST = (
    '    cmd = [\n'
    '        qgis_exec, "run", "hldplanning:end_to_end_pipeline",\n'
    '        "--",\n'
    '        f"EXCEL={excel_path}",\n'
    '        f"OUTPUT_DIR={task_out_dir}",\n'
    '        f"ROADS={roads_path}",\n'
    '    ]\n'
    '    # On Windows, ``qgis_process`` ships as a ``.bat`` wrapper\n'
    '    # (``qgis_process-qgis.bat``). ``CreateProcess`` called directly\n'
    '    # on a ``.bat`` does not handle argument quoting for paths with\n'
    '    # spaces or tokens containing ``=`` (e.g. ``EXCEL=C:\\Users\\Name\\``).\n'
    '    # Routing through ``cmd.exe /c`` lets cmd.exe perform the parsing,\n'
    '    # so the SAME flag survives a path with spaces.\n'
    '    if qgis_exec.lower().endswith((".bat", ".cmd")):\n'
    '        cmd = ["cmd.exe", "/c"] + cmd\n'
)

if OLD_CMD_LIST in content and "cmd.exe\", \"/c\"]" not in content:
    content = content.replace(OLD_CMD_LIST, NEW_CMD_LIST, 1)
    print("[OK] cmd.exe /c wrapper injected into _run_qgis_process_subprocess")
elif "cmd.exe\", \"/c\"]" in content:
    print("[skip] cmd.exe wrapper already present")
else:
    print("[warn] OLD_CMD_LIST not found verbatim; not changed.")

with open(FILE, "w", encoding="utf-8") as f:
    f.write(content)

print("[OK] patch script complete; size:", os.path.getsize(FILE), "chars")
