@echo off
setlocal
powershell.exe -NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0start-panel.ps1"
endlocal
