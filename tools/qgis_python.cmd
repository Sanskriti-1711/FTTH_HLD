@echo off
REM ---------------------------------------------------------------------------
REM Run a command inside the QGIS / OSGeo4W Python environment.
REM
REM Git Bash cannot invoke the QGIS launcher directly: its path contains a
REM space, and cmd.exe splits "C:\Program Files\..." and tries to run
REM "C:\Program". This wrapper lives at a space-free path, so calling it from
REM bash works, and QGIS's own launchers get a correctly quoted path.
REM
REM Usage:
REM   ./HLD_Planning_01/tools/qgis_python.cmd <script.py|args> [args...]
REM   ./HLD_Planning_01/tools/qgis_python.cmd -c "import qgis.core"
REM
REM Set QGIS_BAT to point at a different QGIS install if needed.
REM ---------------------------------------------------------------------------
if "%QGIS_BAT%"=="" set "QGIS_BAT=C:\Program Files\QGIS 3.44.6\bin\python-qgis.bat"
call "%QGIS_BAT%" %*
