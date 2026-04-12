@echo off
REM Create a daily Windows Task Scheduler task to compile claude-code-memory at 22:00
REM This ensures Obsidian articles are generated even if no CC session is open in the evening.

schtasks /create /tn "BrainOS-MemoryCompile" /tr "\"C:\Users\Kadir Bulut\.local\bin\uv.exe\" run --directory \"C:\claude-code\claude-code-memory\" python \"C:\claude-code\claude-code-memory\scripts\compile.py\"" /sc daily /st 22:00 /f

if %ERRORLEVEL% EQU 0 (
    echo Task created successfully. Runs daily at 22:00.
    schtasks /query /tn "BrainOS-MemoryCompile" /v /fo list
) else (
    echo Failed to create task. Try running as Administrator.
)
