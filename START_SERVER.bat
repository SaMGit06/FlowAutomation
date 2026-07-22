@echo off
title SAM's Flow Automation Server
color 0A
echo.
echo  ====================================
echo    SAM's Flow Automation Server
echo    Starting on http://0.0.0.0:5000
echo  ====================================
echo.
cd /d "%~dp0"
python server.py
echo.
echo  Server stopped. Press any key to exit.
pause >nul
