@echo off
setlocal
title YueYue Agent Launcher
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_yueyue.ps1" %*
set EXITCODE=%ERRORLEVEL%

echo.
if not "%EXITCODE%"=="0" (
  echo YueYue launcher exited with code %EXITCODE%.
  echo Check the messages above or logs under workspace\logs.
) else (
  echo YueYue launcher stopped normally.
)
echo.
pause
exit /b %EXITCODE%
