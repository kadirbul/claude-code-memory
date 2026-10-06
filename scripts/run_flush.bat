@echo off
REM ==========================================================================
REM BrainOS PKB — Scheduled daily-flush wrapper
REM
REM Solves the "long session loses days" problem. The Stop hook only fires
REM when a Claude Code session ends. If a session stays open across multiple
REM days (e.g. a multi-day work sprint), no daily notes get written because
REM the Stop hook never fires. This scheduled task runs backfill.py every day
REM at 20:00 for the current date — backfill reads the in-flight transcript
REM and forces a flush, so today's daily note exists even mid-session.
REM
REM Registered by create_schedule.bat (updated 2026-04-16).
REM ==========================================================================

set "UV_BIN=C:\Users\Kadir Bulut\.local\bin"
set "PROJECT_DIR=C:\claude-code\claude-code-memory"
set "LOG_FILE=%PROJECT_DIR%\scripts\scheduled_flush.log"

set "PATH=%UV_BIN%;C:\Windows\System32;C:\Windows;%PATH%"

REM Build today's date as YYYY-MM-DD (PowerShell for timezone safety)
for /f "usebackq tokens=*" %%d in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd'"`) do set "TODAY=%%d"

cd /d "%PROJECT_DIR%"
echo. >> "%LOG_FILE%"
echo ===== %DATE% %TIME% — flush for %TODAY% ===== >> "%LOG_FILE%"
"%UV_BIN%\uv.exe" run --directory "%PROJECT_DIR%" python "%PROJECT_DIR%\scripts\backfill.py" --since %TODAY% >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [exit=%EXIT_CODE%] %DATE% %TIME% >> "%LOG_FILE%"
exit /b %EXIT_CODE%
