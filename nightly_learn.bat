@echo off
REM ============================================================
REM Nightly Learning Loop wrapper
REM   1. Activates anaconda env
REM   2. Runs eod_whatif_backtest for today
REM   3. Embeds new whatif_trades into ChromaDB
REM Schedule via Windows Task Scheduler at 16:00 IST weekdays.
REM ============================================================

set CONDAPATH=C:\ProgramData\anaconda3
call %CONDAPATH%\Scripts\activate.bat %CONDAPATH%

cd /d C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI

python -m rag.nightly_learn
exit /b %ERRORLEVEL%
