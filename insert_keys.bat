@echo off
chcp 65001 >nul
title Vinetrimmer - Insert Keys into Vault

if "%~1"=="" (
    poetry run python .\scripts\insert_vault_keys.py
) else (
    poetry run python .\scripts\insert_vault_keys.py %*
)

echo.
pause
