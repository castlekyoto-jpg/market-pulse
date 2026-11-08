@echo off
cd /d "%~dp0"
py -3 -c "import yfinance, pandas, requests, openpyxl" >nul 2>&1
if errorlevel 1 (
    echo [SETUP] Installing missing Python packages...
    py -3 -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Could not install Python packages.
        pause
        exit /b 1
    )
)
py -3 generate_signals.py --session auto
if errorlevel 1 (
    echo [ERROR] Price data update was unsuccessful. Previous data was preserved.
) else (
    echo [OK] Update complete. Open open_dashboard.bat to view the results.
)
pause
