@echo off
rem Starts the Layar HDMI PC agent. Extra arguments are passed on, e.g. --output 2
cd /d "%~dp0"
where py >nul 2>nul && (py -3 layar_agent.py %*) || (python layar_agent.py %*)
pause
