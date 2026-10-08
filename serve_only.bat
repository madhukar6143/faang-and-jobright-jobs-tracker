@echo off
REM ============================================================
REM  Starts ONLY the server (serves the H-1B + JobRight jobs
REM  already fetched). Does NOT fetch anything -- instant.
REM
REM  Then open http://127.0.0.1:8765/ in Chrome.
REM  To fetch fresh jobs, use run_check.bat instead.
REM ============================================================
title H-1B + JobRight server (no fetch)
cd /d "%~dp0"

echo Starting the server (serving already-fetched jobs).
echo Open http://127.0.0.1:8765/  -- leave this window open. Ctrl+C to stop.
echo.
python serve.py
if errorlevel 1 (
  echo.
  echo Server failed to start. Common cause: another serve.py is
  echo already running -- close its window, or check Task Manager.
  echo.
  pause
)
