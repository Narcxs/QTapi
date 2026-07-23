@echo off
REM ---- One-click launcher for Windows ----
cd /d %~dp0

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate
echo Installing / updating dependencies...
pip install -q -r requirements.txt

echo Starting GexBot cache server (poller + API)...
python -m app.main
pause
