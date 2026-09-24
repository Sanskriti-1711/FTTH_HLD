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
cd HLD_Planning_01/web/backend && python -m pytest tests -q

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
- `unset PYTHONPATH` before running the backend suite by hand: a QGIS
  `site-packages` on `PYTHONPATH` shadows the Anaconda interpreter and makes
  Django report "Pillow is not installed". `run_all_tests.cmd` clears it.
