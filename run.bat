@echo off
rem ===========================================================================
rem  jvdata-store - open the control panel of the data screen.
rem
rem    Double-click        : opens the control panel. If the server is stopped,
rem                          it starts the server and opens the browser.
rem    run.bat --db D:\keiba\jvdata.duckdb --port 9000
rem
rem    Start / reopen / stop the server from the panel. Closing the panel does
rem    not stop the server. Without a panel: uv run jvstore serve --open
rem
rem  NOTE: keep this file ASCII-only (cmd.exe reads it with the console code page).
rem ===========================================================================

cd /d "%~dp0"

where uv >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uv was not found on PATH.
    goto :failed
)

uv sync --quiet
if errorlevel 1 (
    echo [ERROR] "uv sync" failed.
    goto :failed
)

rem pythonw has no console window, so this window can close right away.
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m jvstore.cli panel %*
) else (
    uv run python -m jvstore.cli panel %*
    if errorlevel 1 goto :failed
)
exit /b 0

:failed
echo.
pause
exit /b 1
