@echo off
setlocal
cd /d "%~dp0"
rem Read local runtime records only. No scanning, broker calls or order submission.
python -m alpaca_agents.dashboard
if errorlevel 1 (
    echo.
    echo Dashboard generation failed. Check the message above.
    echo If the package is not installed, run: python -m pip install -e .
    pause
    exit /b 1
)
start "" "%CD%\runtime\dashboard.html"
