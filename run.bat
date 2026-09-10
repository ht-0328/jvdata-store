@echo off
rem Open the standalone JRA-VAN data screen. Keep this file ASCII-only.
cd /d "%~dp0"
where uv >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uv was not found on PATH.
    goto :failed
)
uv run python -m jvstore.cli serve --open %*
if errorlevel 1 goto :failed
exit /b 0
:failed
echo.
pause
exit /b 1
