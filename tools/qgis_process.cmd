@echo off
REM ---------------------------------------------------------------------------
REM Run qgis_process from a space-free path (Git Bash / cmd.exe).
REM
REM The QGIS launcher lives at "C:\Program Files\..." — cmd.exe splits the
REM unquoted path at the space, so bash never reaches it. This wrapper sits at
REM a space-free path and quotes the launcher correctly.
REM
REM Usage:
REM   ./HLD_Planning_01/tools/qgis_process.cmd run hldplanning:end_to_end_pipeline -- KEY=VAL ...
REM
REM Set QGIS_BAT to point at a different QGIS install if needed.
REM ---------------------------------------------------------------------------
if "%QGIS_BAT%"=="" set "QGIS_BAT=C:\Program Files\QGIS 3.44.6\bin\qgis_process-qgis.bat"
call "%QGIS_BAT%" %*
