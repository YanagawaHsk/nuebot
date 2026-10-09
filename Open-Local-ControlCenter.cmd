@echo off
setlocal
set "NUEBOT_PANEL_PROFILE=local"
powershell.exe -NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0start-panel.ps1"
endlocal
