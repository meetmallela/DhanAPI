@echo off
title Trading System
color 0A

:: Refresh instruments on start or restart (not stop/status/watchdog)
if /i "%1"=="start"   goto refresh
if /i "%1"=="restart" goto refresh
goto run

:refresh
echo.
echo ============================================================
echo   STEP 1/2 : Refreshing instruments CSV from Dhan...
echo ============================================================
C:\ProgramData\anaconda3\python.exe "%~dp0generate_instruments_csv_dhan.py"
if errorlevel 1 (
    echo.
    echo [WARN] Instruments refresh FAILED - continuing with existing CSV
    echo.
) else (
    echo [OK] Instruments CSV updated.
    echo.
)
echo ============================================================
echo   STEP 2/2 : Starting trading system processes...
echo ============================================================

:run
C:\ProgramData\anaconda3\python.exe "%~dp0start_trading_system.py" %1
pause
