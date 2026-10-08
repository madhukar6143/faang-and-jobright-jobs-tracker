@echo off
REM ============================================================
REM  Fetch -> HEALTH CHECK -> serve, in one window.
REM
REM  After fetching it runs check_feeds.py to verify both feeds
REM  are healthy (fresh, non-empty, <=24h, no missing fields,
REM  all connectors reporting) BEFORE it starts the server. If
REM  the check finds a problem it pauses so you can read it --
REM  the #1 catch is JobRight returning 0 jobs, which means the
REM  SESSION_ID expired.
REM
REM  Double-click this. Leave the final window open, then open
REM  http://127.0.0.1:8765/ in Chrome.
REM ============================================================
title H-1B + JobRight: fetch, check, serve
cd /d "%~dp0"

echo.
echo [1/5] Fetching H-1B company jobs (last 24h)...
cd faang
python fetch_jobs.py
if errorlevel 1 goto err

echo.
echo [2/5] Reading job descriptions (enrich)...
python enrich.py --yoe 5 --max-yoe 8

echo.
echo [3/5] Fetching JobRight recommendations (last 24h)...
cd ..\jobright
python fetch_jobright.py

echo.
echo [4/5] Building combined report...
cd ..\faang
python report.py

echo.
echo [5/5] Health check (verifying both feeds)...
cd ..
python check_feeds.py
if errorlevel 1 (
  echo.
  echo ============================================================
  echo  *** HEALTH CHECK FOUND A PROBLEM -- see the red lines above.
  echo  Most common: JobRight shows 0 jobs = expired SESSION_ID.
  echo  Fix it in jobright\fetch_jobright.py line 27, then re-run.
  echo.
  echo  Press a key to start the server anyway, or close this
  echo  window ^(X / Ctrl+C^) to stop and fix first.
  echo ============================================================
  pause
)

echo.
echo ============================================================
echo  Feeds look good. Starting the server ^(H-1B + JobRight^).
echo  Open http://127.0.0.1:8765/  -- leave this window open.
echo ============================================================
python serve.py
goto end

:err
echo.
echo A fetch step failed. Common cause: another serve.py already
echo running, or a broken connector. See the message above.
pause

:end
