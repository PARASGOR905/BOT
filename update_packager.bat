@echo off
setlocal enabledelayedexpansion
title Shaka Packager Updater

cd /d "%~dp0"
python scripts\update_packager.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Update process encountered an error (Code: %ERRORLEVEL%).
)

echo.
pause
