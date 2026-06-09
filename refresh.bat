@echo off
REM ====================================================================
REM  Refresh the dashboard data.
REM  Double-click this file to pull the latest from GHL / Meta / GA4 /
REM  GSC and write it to MotherDuck. Both your local and the deployed
REM  Streamlit dashboards then read the fresh data.
REM  (Requires MOTHERDUCK_TOKEN in ..\.env so it targets MotherDuck.)
REM ====================================================================
cd /d "%~dp0"
echo ============================================================
echo   Refreshing dashboard data into MotherDuck...
echo   (this pulls the latest leads/appointments/spend - it can
echo    take a few minutes; leave this window open)
echo ============================================================
echo.
"%~dp0..\.venv\Scripts\python.exe" etl\run_etl.py
echo.
echo ============================================================
echo   Done. To see it immediately in the deployed app:
echo   Streamlit  -  Manage app  -  Reboot app.
echo ============================================================
pause
