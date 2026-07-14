:: start-qgis-backend.cmd — launch the HLD Planner backend with QGIS wired up.
:: Edit the QGIS version below if it changes. Place this file at the repo root
:: (next to web/) and double-click, or invoke from any shell:
::     .\start-qgis-backend.cmd
@echo off
setlocal

:: 1. Configure these two to match your machine.
set "REPO_ROOT=%~dp0"
set "QGIS_BIN=C:\Program Files\QGIS 3.44.6\bin"
if not exist "%QGIS_BIN%\qgis_process-qgis.bat" (
    echo ERROR: qgis_process-qgis.bat not found under "%QGIS_BIN%".
    echo Edit QGIS_BIN above to point at your QGIS install location.
    exit /b 1
)

:: 2. Point the backend at qgis_process explicitly. Do not prepend QGIS_BIN
::    to PATH before launching Python, because that can make Windows pick
::    QGIS's embedded python.exe instead of the normal project Python.
set "QGIS_EXECUTABLE=%QGIS_BIN%\qgis_process-qgis.bat"

:: 3. Tell QGIS where to look for plugins. The repo root contains HLDPlanning/
::    and pulls in qgis_process the moment the env var is set.
set "QGIS_PLUGINPATH=%REPO_ROOT%"

:: 4. QGIS_PREFIX_PATH lets the headless PyQGIS path find its libs (we still
::    ship subprocess-first, but this matters the moment someone enables the
::    in-process path on Windows).
set "QGIS_PREFIX_PATH=%QGIS_BIN%\.."

:: 5. No GUI: offscreen Qt suppresses any modal dialog a provider might pop up.
set "QT_QPA_PLATFORM=offscreen"

echo Using QGIS_BIN=%QGIS_BIN%
echo Using QGIS_PLUGINPATH=%QGIS_PLUGINPATH%
echo.

cd /d "%REPO_ROOT%"
cd /d "%REPO_ROOT%web\backend"
python -m uvicorn main:app --host 0.0.0.0 --port 8000
endlocal
