@echo off
rem Subtle setup: installs everything into this folder (run again to repair or update).
title Subtle setup
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1" %*
if errorlevel 1 pause
