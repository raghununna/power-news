@echo off
rem Daily Power & EV News Digest - Windows launcher
rem Generates the digest and opens it in your browser.
rem (If you use this in Task Scheduler and do NOT want the browser to
rem  pop open automatically, delete the last line.)
cd /d "%~dp0"
where py >nul 2>nul && (py -3 agent.py) || (python agent.py)
start "" "digests\latest.html"
