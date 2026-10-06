# HLD engine tests

The engine is split across **two Python interpreters**, so no single `pytest`
invocation covers it.

| Suite | Path | Interpreter | Why |
|---|---|---|---|
| Plugin algorithms | `tests/` | QGIS Python 3.12 | imports `qgis.core`, a compiled module |
| Backend / FastAPI | `web/backend/tests/` | Anaconda 3.11 | tests do `from main import ...` |

## Run everything

```bash
./HLD_Planning_01/tools/run_all_tests.cmd     # from the repo root, or double-click
```

## Run one suite

```bash
# QGIS plugin algorithms
./HLD_Planning_01/tools/qgis_python.cmd \
    HLD_Planning_01/tools/run_qgis_tests.py

# Backend / FastAPI — must run from web/backend (the tests import `main`)
# Git Bash: remove any QGIS Python 3.12 environment inherited by Anaconda 3.11
cd HLD_Planning_01/web/backend && env -u PYTHONPATH -u PYTHONHOME python -m pytest tests -q
# Windows cmd.exe: clear those variables before invoking Python
cd HLD_Planning_01\web\backend
set "PYTHONPATH=" && set "PYTHONHOME=" && python -m pytest tests -q

# Focused LLD Mode A suite (same environment isolation)
cd HLD_Planning_01/web/backend && env -u PYTHONPATH -u PYTHONHOME python -m pytest tests/test_lld_mode_a.py -q
cd HLD_Planning_01\web\backend
set "PYTHONPATH=" && set "PYTHONHOME=" && python -m pytest tests\test_lld_mode_a.py -q

# One file / one test
./HLD_Planning_01/tools/qgis_python.cmd \
    HLD_Planning_01/tools/run_qgis_tests.py HLD_Planning_01/tests/test_trench_layer_helpers.py -k weld
```

## Why the harness looks like this

- **`tools/qgis_python.cmd`** exists because Git Bash cannot invoke QGIS's
  launcher directly — its path contains a space, so `cmd.exe` splits
  `"C:\Program Files\..."` and tries to run `"C:\Program"`. The wrapper sits at
  a space-free path and quotes the launcher correctly.
- **`tools/run_qgis_tests.py`** exists because the QGIS Python install has no
  `pytest`. Instead of installing into the QGIS tree, it appends an existing
  (pure-Python) pytest to the **end** of `sys.path`, so QGIS's own compiled
  packages (`qgis`, `osgeo`, `numpy`) still take precedence, and disables
  third-party plugin autoloading. Point it elsewhere with
  `QGIS_TEST_PYTEST_SITE` and `QGIS_BAT`.
- **The suites stay separate** because the backend tests cannot run under QGIS
  Python: Anaconda's `fastapi` pairs with a different `pydantic_core` than the
  one QGIS ships, so `import main` fails there.

## Notes

- Tests in `tests/` use the `qgis_app` fixture from `conftest.py`, which starts
  a headless `QgsApplication` once per session.
- **One rule set can straddle the two suites, and is split by what each test
  needs.** The trench-basis rules live in
  `web/backend/tests/test_trench_basis.py` (the `trench_design` half — GDAL and
  networkx only, so it runs under Anaconda with the rest of the backend suite)
  and `tests/test_trench_basis_plugin.py` (the half that reads `trench_layer`
  and `network_layer`, which import `qgis.core`). The two files pin one rule set
  and are read together; a file that imports a plugin module from the backend
  suite breaks the whole backend run at collection.
- Clear both `PYTHONPATH` and `PYTHONHOME` before running the backend suite by
  hand: QGIS's Python 3.12 packages/stdlib are incompatible with the backend's
  Anaconda Python 3.11. The test harness and server launchers clear both; the
  engine injects QGIS paths only into the `qgis_process` subprocess.
