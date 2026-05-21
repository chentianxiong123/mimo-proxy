@echo off
cd /d "%~dp0"
echo Starting MiMo Proxy...
echo.
echo   Dashboard: http://127.0.0.1:8899/dashboard
echo.
echo   Tips:
echo     - Set MIMO_API_KEY env var to avoid config.yaml exposing your key
echo     - set MIMO_API_KEY=sk-xxx ^&^& python -m src.main
echo.
python -m src.main
if errorlevel 1 (
    echo.
    echo Failed. Check:
    echo   1. config.yaml exists (copy from config.example.yaml)
    echo   2. Dependencies: pip install -r requirements.txt
    pause
)