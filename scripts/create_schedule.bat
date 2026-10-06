@echo off
REM ==========================================================================
REM BrainOS PKB — Scheduled task installer
REM
REM Registers TWO Windows Task Scheduler tasks:
REM   - BrainOS-MemoryFlush    at 20:00 daily   — forces a flush of the
REM                                               in-flight session so today's
REM                                               daily note exists even during
REM                                               multi-day sessions.
REM   - BrainOS-MemoryCompile  at 22:00 daily   — compiles any new daily logs
REM                                               into Obsidian knowledge
REM                                               articles.
REM
REM Both tasks run via wrapper .bat files (run_flush.bat / run_compile.bat)
REM which set PATH, cd into the project dir, and log every run. This fixes
REM the scheduled crash (-1073741502 / DLL init failure) caused by Task
REM Scheduler not inheriting the user's interactive environment.
REM
REM Run this ONCE (or after editing) as the user; Administrator not required
REM for per-user tasks. /f overwrites any existing task with the same name.
REM ==========================================================================

set "SCRIPT_DIR=%~dp0"

schtasks /create /tn "BrainOS-MemoryFlush" /tr "\"%SCRIPT_DIR%run_flush.bat\"" /sc daily /st 20:00 /f
if %ERRORLEVEL% NEQ 0 goto :fail

schtasks /create /tn "BrainOS-MemoryCompile" /tr "\"%SCRIPT_DIR%run_compile.bat\"" /sc daily /st 22:00 /f
if %ERRORLEVEL% NEQ 0 goto :fail

echo.
echo ============================================================
echo  Tasks registered successfully.
echo    BrainOS-MemoryFlush    at 20:00 daily
echo    BrainOS-MemoryCompile  at 22:00 daily
echo ============================================================
echo.
echo Current status:
schtasks /query /tn "BrainOS-MemoryFlush" /v /fo list | findstr /C:"TaskName" /C:"Next Run Time" /C:"Status" /C:"Last Result"
echo.
schtasks /query /tn "BrainOS-MemoryCompile" /v /fo list | findstr /C:"TaskName" /C:"Next Run Time" /C:"Status" /C:"Last Result"
goto :eof

:fail
echo.
echo Failed to register task. Try running this .bat as Administrator.
exit /b 1
