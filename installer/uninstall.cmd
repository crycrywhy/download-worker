@echo off
rem ============================================================================
rem  Local Download Worker - uninstaller launcher
rem
rem  Removes the scheduled tasks, the firewall rule and the installation
rem  directory.  Tailscale, SSH keys and Windows OpenSSH are left alone.
rem ============================================================================
setlocal

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

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall-worker.ps1" %*
set "RC=%errorlevel%"

echo.
echo Exit code: %RC%
echo.
pause
endlocal
exit /b %RC%
