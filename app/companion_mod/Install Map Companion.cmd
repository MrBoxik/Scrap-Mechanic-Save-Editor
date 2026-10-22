@echo off
title Install Scrap Mechanic Map Companion
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_companion_mod.ps1" -Mode Survival
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Installation failed with exit code %RESULT%.
pause
exit /b %RESULT%
