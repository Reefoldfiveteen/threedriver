@echo off
REM 3Driver one-click launcher (Windows). Double-click to start the local web UI.
cd /d %~dp0
py -3 --version >nul 2>&1 || (
  echo Python 3 tidak ditemukan. Install Python 3.10+ dari python.org, lalu coba lagi.
  pause
  exit /b 1
)
if not exist .venv (
  echo Membuat virtual environment...
  py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
start "" http://127.0.0.1:8765
python scripts\serve.py
pause
