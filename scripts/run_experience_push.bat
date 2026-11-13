@echo off
REM ==========================================================================
REM BrainOS PKB - Scheduled experience push wrapper (WI-cr-2cdd9f, K475)
REM
REM Loads the compiled wiki, lessons, Z-Reports and memory facts into the KRS
REM namespace brainos-experience, through brain-os/tools/governance/experience_push.py.
REM
REM It is its OWN task (BrainOS-ExperiencePush, 01:30), not a step at the end of
REM run_compile.bat: the compile task has a 2-hour limit (K476), and a stop at
REM that limit ends the whole batch file, so a trailing step would not run on
REM long nights.
REM
REM Re-runs are cheap: KRS skips an unchanged document (SHA-256 per namespace)
REM and replaces a changed one by its source key (WI-bugfix-a224bc).
REM Exit codes: 0 ok, 1 some documents failed, 2 refused (KRS below enforce).
REM ==========================================================================

set "PY=C:\Users\Kadir Bulut\AppData\Local\Programs\Python\Python312\python.exe"
set "BRAIN_OS=C:\claude-code\brain-os"
set "LOG_FILE=C:\claude-code\claude-code-memory\scripts\scheduled_experience_push.log"

set "PATH=C:\Windows\System32;C:\Windows;%PATH%"
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%BRAIN_OS%"
echo. >> "%LOG_FILE%"
echo ===== %DATE% %TIME% ===== >> "%LOG_FILE%"
"%PY%" "%BRAIN_OS%\tools\governance\experience_push.py" >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo [exit=%EXIT_CODE%] %DATE% %TIME% >> "%LOG_FILE%"
exit /b %EXIT_CODE%
