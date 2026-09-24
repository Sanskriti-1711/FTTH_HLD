"""Run the QGIS-dependent engine tests under the QGIS Python interpreter.

Why this exists
---------------
Part of the engine is a QGIS plugin: `HLDPlanning/algorithms/*.py` imports
`qgis.core`, a compiled module built for CPython 3.12. It cannot be imported by
the Anaconda 3.11 interpreter the Django backend runs on, so those modules have
no test coverage under the normal suite.

The QGIS Python install has no `pytest`. Rather than installing anything, this
runner appends an existing pytest to the *end* of `sys.path`, so QGIS's own
compiled packages (qgis, osgeo, numpy) still take precedence, and disables
third-party plugin autoloading so the run stays hermetic.

The two suites, and which interpreter each needs
------------------------------------------------
  HLD_Planning_01/tests/                QGIS-dependent  -> QGIS Python (here)
  HLD_Planning_01/web/backend/tests/    FastAPI/backend -> Anaconda 3.11

The backend tests cannot run here: they import `main` (the FastAPI app), and
Anaconda's `fastapi` pairs with a different `pydantic_core` than the one QGIS
ships, so the import fails. Run them with the Anaconda interpreter instead.

Usage (from the repo root):
    ./HLD_Planning_01/tools/qgis_python.cmd \\
        HLD_Planning_01/tools/run_qgis_tests.py [pytest args]

Default target is `HLD_Planning_01/tests`.
"""

from __future__ import annotations

import os
import sys

# Where a compatible, pure-Python pytest may already live. Anaconda's pytest is
# pure Python, so it loads fine under CPython 3.12; override with
# QGIS_TEST_PYTEST_SITE if the layout differs.
PYTEST_SITE_CANDIDATES = [
    os.environ.get("QGIS_TEST_PYTEST_SITE", ""),
    os.path.expanduser(r"~\anaconda3\Lib\site-packages"),
    os.path.expanduser(r"~\miniconda3\Lib\site-packages"),
    r"C:\ProgramData\anaconda3\Lib\site-packages",
]

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.dirname(TOOLS_DIR)          # .../Fibre-FTTH/HLD_Planning_01
REPO_ROOT = os.path.dirname(ENGINE_DIR)          # .../Fibre-FTTH

DEFAULT_TARGET = os.path.join(ENGINE_DIR, "tests")


def _find_pytest_site() -> str | None:
    for candidate in PYTEST_SITE_CANDIDATES:
        if candidate and os.path.isdir(os.path.join(candidate, "pytest")):
            return candidate
    return None


def main(argv: list[str]) -> int:
    os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

    site = _find_pytest_site()
    # Appended, never prepended: QGIS's own packages must shadow Anaconda's.
    if site and site not in sys.path:
        sys.path.append(site)

    # Engine packages: the plugin (`HLDPlanning`) and the backend.
    for path in (ENGINE_DIR, os.path.join(ENGINE_DIR, "web", "backend"), REPO_ROOT):
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)

    try:
        import pytest
    except ImportError:
        print("pytest not found. Looked in:")
        for candidate in PYTEST_SITE_CANDIDATES:
            print("  " + (candidate or "(unset)"))
        print("Set QGIS_TEST_PYTEST_SITE to a site-packages containing pytest.")
        return 2

    print("interpreter : " + sys.version.split()[0])
    print("pytest      : " + pytest.__version__)
    try:
        from qgis.core import Qgis

        print("qgis        : " + Qgis.QGIS_VERSION)
    except Exception as exc:  # noqa: BLE001 - report and continue
        print("qgis        : NOT AVAILABLE (" + str(exc) + ")")

    args = argv or [DEFAULT_TARGET, "-q"]
    return pytest.main(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
