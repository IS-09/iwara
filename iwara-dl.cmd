@echo off
setlocal
chcp 65001 >nul 2>&1

set "PS1=%~dp0iwara-dl.ps1"
if not exist "%PS1%" (
    echo [ERROR] iwara-dl.ps1 not found next to this cmd file.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "RC=%ERRORLEVEL%"
exit /b %RC%
