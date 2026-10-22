@echo off
setlocal EnableExtensions
title Attach Scrap Mechanic Map Companion

tasklist /FI "IMAGENAME eq ScrapMechanic.exe" 2>nul | find /I "ScrapMechanic.exe" >nul
if errorlevel 1 (
  echo Scrap Mechanic is not running. Start it normally, enter a world, and try again.
  pause
  exit /b 1
)

set "OVERLAY_SCRIPT=%~dp0companion_overlay.py"
set "OVERLAY_EXE=%~dp0ScrapMechanicMapOverlay.exe"
set "RUNTIME_ROOT=%LOCALAPPDATA%\ScrapMechanicMapCompanion\Runtime"
set "KEY_SIGNAL=%RUNTIME_ROOT%\map_key_signal.json"
if not exist "%RUNTIME_ROOT%" mkdir "%RUNTIME_ROOT%"
if not exist "%KEY_SIGNAL%" >"%KEY_SIGNAL%" echo {"sequence":0}

if exist "%OVERLAY_EXE%" (
  start "" /B "%OVERLAY_EXE%" --signal "%KEY_SIGNAL%"
) else (
  where pythonw.exe >nul 2>nul || (
    echo Python was not found. Use ScrapMechanicSaveEditor.exe for the bundled launcher.
    pause
    exit /b 1
  )
  if not exist "%OVERLAY_SCRIPT%" (
    echo Map Companion files are incomplete in: %~dp0
    pause
    exit /b 1
  )
  start "" /B pythonw.exe "%OVERLAY_SCRIPT%" --signal "%KEY_SIGNAL%"
)
exit /b 0
