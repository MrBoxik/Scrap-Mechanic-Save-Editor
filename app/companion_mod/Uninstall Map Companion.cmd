@echo off
title Uninstall Scrap Mechanic Map Companion
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall_companion_mod.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo Uninstall failed with exit code %RESULT%.
pause
exit /b %RESULT%
