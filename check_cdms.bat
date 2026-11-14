@echo off
setlocal enabledelayedexpansion
title "Widevine CDM Revocation & Status Checker"

echo ======================================================================
echo           Widevine CDM Revocation ^& Working Status Checker
echo ======================================================================
echo.

set "DEVICES_DIR=%~dp0vinetrimmer\devices"
set "SCRIPT_PATH=%~dp0Standalone\check_cdms.py"
if not exist "%SCRIPT_PATH%" set "SCRIPT_PATH=%~dp0check_cdms.py"

if not exist "%SCRIPT_PATH%" (
    echo [!] Error: check_cdms.py not found at %SCRIPT_PATH%
    pause
    exit /b 1
)

if "%~1"=="" (
    echo [*] Checking all CDMs in default folder: %DEVICES_DIR%
    python "%SCRIPT_PATH%" --dir "%DEVICES_DIR%"
) else (
    echo [*] Running with custom parameters: %*
    python "%SCRIPT_PATH%" %*
)

echo.
pause
