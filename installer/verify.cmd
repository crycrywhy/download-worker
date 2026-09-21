@echo off
rem ============================================================================
rem  Local Download Worker - read-only verification
rem
rem  No administrator rights needed (a few checks are skipped without them).
rem ============================================================================
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify-install.ps1" %*
set "RC=%errorlevel%"

echo.
echo Exit code: %RC%   (0 = everything that was checked passed)
echo.
pause
endlocal
exit /b %RC%
