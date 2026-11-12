@echo off
rem Starts Subtle (tray icon + control panel). The desktop shortcut does the same without this window.
cd /d "%~dp0"
if exist "runtime\venv\Scripts\pythonw.exe" (
  start "" "runtime\venv\Scripts\pythonw.exe" "server\supervisor.py"
) else if exist "server\.venv\Scripts\pythonw.exe" (
  start "" "server\.venv\Scripts\pythonw.exe" "server\supervisor.py"
) else (
  echo Subtle is not installed yet - run "Install Subtle.bat" first.
  pause
)
