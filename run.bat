@echo off
REM Double-click this file (or run it from a terminal) to start the app.
REM It always uses the virtual environment's own python.exe directly,
REM so it can't accidentally run with the wrong Python install.

cd /d "%~dp0"

if not exist venv (
    echo Virtual environment not found - creating it now...
    python -m venv venv
    echo Installing dependencies - this may take a minute...
    venv\Scripts\python.exe -m pip install -r requirements.txt
)

echo Starting Student Risk ^& Early Intervention System...
echo Once you see "Running on http://127.0.0.1:5050", open that address in your browser.
echo Press CTRL+C in this window to stop the server.
echo.

venv\Scripts\python.exe app.py
pause
