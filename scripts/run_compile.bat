@echo off
REM ==========================================================================
REM BrainOS PKB — Scheduled compile wrapper
REM
REM Task Scheduler spawns this without a proper shell profile, so we:
REM   1. Set PATH to include uv.exe's bin dir explicitly (PATH alone isn't
REM      inherited reliably from the user's interactive session).
REM   2. Change to the project directory so relative paths in compile.py work.
REM   3. Redirect all output (including DLL init errors → -1073741502) to a
REM      rolling log so future failures are diagnosable.
REM
REM Registered by create_schedule.bat (updated 2026-04-16).
REM ==========================================================================

set "UV_BIN=C:\Users\Kadir Bulut\.local\bin"
set "PROJECT_DIR=C:\claude-code\claude-code-memory"
set "LOG_FILE=%PROJECT_DIR%\scripts\scheduled_compile.log"

set "PATH=%UV_BIN%;C:\Windows\System32;C:\Windows;%PATH%"

cd /d "%PROJECT_DIR%"
echo. >> "%LOG_FILE%"
echo ===== %DATE% %TIME% ===== >> "%LOG_FILE%"
"%UV_BIN%\uv.exe" run --directory "%PROJECT_DIR%" python "%PROJECT_DIR%\scripts\compile.py" >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [exit=%EXIT_CODE%] %DATE% %TIME% >> "%LOG_FILE%"
exit /b %EXIT_CODE%
