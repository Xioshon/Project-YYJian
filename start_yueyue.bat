@echo off
chcp 65001 >nul
title YueYue Launcher
cd /d C:\Agent

echo ==========================================
echo YueYue Launcher
echo ==========================================
echo.
echo This is the only file you need to double-click.
echo.

:CHECK
echo [1/2] Running CheckOnly...
powershell -NoProfile -ExecutionPolicy Bypass -File ".\start_yueyue.ps1" -CheckOnly
if errorlevel 1 (
    echo.
    echo [ERROR] CheckOnly failed.
    echo Please fix the audit/runtime error first.
    echo.
    pause
    exit /b 1
)

:RUN
echo.
echo [2/2] Starting YueYue Telegram bot...
echo.
echo Network unstable? YueYue will auto-reconnect polling.
echo If Python exits completely, this launcher will restart it after 5 seconds.
echo Press Ctrl+C twice to stop.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File ".\start_yueyue.ps1"

echo.
echo [WARN] YueYue process exited.
echo Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto RUN
