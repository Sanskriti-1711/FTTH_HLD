@echo off
REM ---------------------------------------------------------------------------
REM Run every engine test suite.
REM
REM The engine spans two interpreters, so there is no single `pytest` invocation
REM that covers it:
REM
REM   QGIS plugin algorithms (HLDPlanning/algorithms) need QGIS's CPython 3.12
REM   because `qgis.core` is a compiled module; the Anaconda 3.11 interpreter the
REM   backend uses cannot import it.
REM
REM   The backend / FastAPI suite needs Anaconda, because the tests do
REM   `from main import ...` and Anaconda's fastapi/pydantic pair does not match
REM   the pydantic QGIS ships.
REM
REM Usage (from anywhere):  HLD_Planning_01\tools\run_all_tests.cmd
REM ---------------------------------------------------------------------------
setlocal
set "TOOLS=%~dp0"
set "ENGINE=%TOOLS%.."

REM Resolve the backend interpreter BEFORE touching QGIS: QGIS's launcher
REM prepends its own bin to PATH, which would otherwise hijack `python`.
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYEXE set "PYEXE=%%P"
if not defined PYEXE (
    echo RESULT: no `python` on PATH; cannot run the backend suite.
    exit /b 1
)

REM The launchers warn about this: a QGIS site-packages on PYTHONPATH shadows
REM the Anaconda interpreter and makes Django report "Pillow is not installed".
REM PYTHONHOME can redirect Anaconda to QGIS's Python 3.12 standard library too.
set "PYTHONPATH="
set "PYTHONHOME="
set "FAIL="

echo.
echo === QGIS plugin algorithms ^(QGIS Python 3.12^) ===
REM Child `cmd /c`: the QGIS launcher rewrites PATH in-process, and `call`
REM would leak that into the backend run below.
cmd /c call "%TOOLS%qgis_python.cmd" "%TOOLS%run_qgis_tests.py"
if errorlevel 1 set "FAIL=1"

echo.
echo === Backend / FastAPI ^(Anaconda 3.11^) ===
echo using: %PYEXE%
pushd "%ENGINE%\web\backend"
"%PYEXE%" -m pytest tests -q
if errorlevel 1 set "FAIL=1"
popd

echo.
if defined FAIL (
    echo RESULT: FAILURES ABOVE
    exit /b 1
)
echo RESULT: all suites passed
exit /b 0
