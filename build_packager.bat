@echo off
setlocal enabledelayedexpansion
title Shaka Packager Build from Source

echo =======================================================
echo          Shaka Packager Source Builder
echo =======================================================
echo.

:: 1. Check Git
where git >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Git is not installed or not found in PATH!
    echo Please install Git or add it to PATH before running this script.
    pause
    exit /b 1
)

:: 2. Check CMake
set "CMAKE_EXE="
where cmake >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    set "CMAKE_EXE=cmake"
) else (
    if exist "C:\Program Files\CMake\bin\cmake.exe" (
        set "CMAKE_EXE=C:\Program Files\CMake\bin\cmake.exe"
    ) else if exist "C:\Program Files\Microsoft Visual Studio\18\Enterprise\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" (
        set "CMAKE_EXE=C:\Program Files\Microsoft Visual Studio\18\Enterprise\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    ) else if exist "C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" (
        set "CMAKE_EXE=C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    ) else if exist "C:\Program Files\Microsoft Visual Studio\2022\Professional\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" (
        set "CMAKE_EXE=C:\Program Files\Microsoft Visual Studio\2022\Professional\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    ) else if exist "C:\Program Files\Microsoft Visual Studio\2022\Enterprise\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" (
        set "CMAKE_EXE=C:\Program Files\Microsoft Visual Studio\2022\Enterprise\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    )
)

if "%CMAKE_EXE%"=="" (
    echo [ERROR] CMake is not found!
    echo Please install CMake (https://cmake.org/download/) or Visual Studio C++ workload.
    pause
    exit /b 1
)

echo [INFO] Git: Found
echo [INFO] CMake: %CMAKE_EXE%
echo.

:: 3. Prepare source directory
set "SRC_DIR=%~dp0tools\shaka-packager"
if not exist "%~dp0tools" mkdir "%~dp0tools"

if not exist "%SRC_DIR%\.git" (
    echo [INFO] Cloning shaka-packager with submodules...
    git clone --recurse-submodules https://github.com/shaka-project/shaka-packager.git "%SRC_DIR%"
    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] Failed to clone shaka-packager repository!
        pause
        exit /b %ERRORLEVEL%
    )
) else (
    echo [INFO] Updating existing shaka-packager repository...
    pushd "%SRC_DIR%"
    git pull origin main --rebase
    git submodule update --init --recursive
    popd
)

:: 4. Build with CMake
echo.
echo [INFO] Configuring CMake...
cd /d "%SRC_DIR%"
"%CMAKE_EXE%" -B build
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] CMake configuration failed!
    cd /d "%~dp0"
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [INFO] Compiling Shaka Packager (Release)...
"%CMAKE_EXE%" --build build --parallel --config Release
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Compilation failed!
    cd /d "%~dp0"
    pause
    exit /b %ERRORLEVEL%
)

:: 5. Copy binary to binaries/ folder
set "BUILT_BIN="
if exist "%SRC_DIR%\build\packager\Release\packager.exe" (
    set "BUILT_BIN=%SRC_DIR%\build\packager\Release\packager.exe"
) else if exist "%SRC_DIR%\build\packager\packager.exe" (
    set "BUILT_BIN=%SRC_DIR%\build\packager\packager.exe"
)

if "%BUILT_BIN%"=="" (
    echo [ERROR] Could not find compiled packager.exe in build directory!
    cd /d "%~dp0"
    pause
    exit /b 1
)

cd /d "%~dp0"
if not exist ".\binaries" mkdir ".\binaries"

if exist ".\binaries\packager.exe" (
    if exist ".\binaries\packager_backup.exe" del /f /q ".\binaries\packager_backup.exe"
    copy /y ".\binaries\packager.exe" ".\binaries\packager_backup.exe" >nul
    echo [INFO] Backed up old packager.exe to binaries\packager_backup.exe
)

copy /y "%BUILT_BIN%" ".\binaries\packager.exe" >nul
echo [SUCCESS] Copied %BUILT_BIN% to .\binaries\packager.exe
echo.

:: 6. Verify binary
echo Verifying built Shaka Packager version:
".\binaries\packager.exe" --version
echo.
echo =======================================================
echo Build completed successfully!
echo =======================================================
pause
