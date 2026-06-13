@echo off
REM ============================================================
REM KB Growth Audit wrapper
REM Fired daily by Task Scheduler; the script self-gates so audits
REM only happen every 2 trading sessions, starting 2026-05-15.
REM ============================================================

set CONDAPATH=C:\ProgramData\anaconda3
call %CONDAPATH%\Scripts\activate.bat %CONDAPATH%

cd /d C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI

python -m rag.audit_kb_growth
exit /b %ERRORLEVEL%
