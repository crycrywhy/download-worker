@echo off
rem ============================================================================
rem  Local Download Worker - installer launcher
rem
rem  Why a .cmd: batch files are not governed by the PowerShell execution policy,
rem  so this runs on a stock Windows machine (default policy: Restricted) where
rem  "powershell -File install-worker.ps1" would be refused with
rem  "running scripts is disabled on this system".
rem
rem  Usage: double-click, or from a console:
rem      install.cmd [-LinuxUser <user> -LinuxHost <host> -LinuxTunnelPort <port> ...]
rem  Any arguments are passed through to install-worker.ps1.
rem ============================================================================
setlocal

rem ---- administrator rights -------------------------------------------------
net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo Administrator rights are required - opening an elevated window...
    if "%~1"=="" (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b
)

rem ---- run the installer ----------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-worker.ps1" %*
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" (
    echo Installer finished. Scroll up for the summary, or run verify.cmd to check it.
) else (
    echo Installer FAILED with exit code %RC% - scroll up for the error message.
)
echo.
pause
endlocal
exit /b %RC%
