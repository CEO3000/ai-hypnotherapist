@echo off
rem Starts the AI Hypnotherapist test server and opens it in Microsoft Edge (Edge has the natural voices).
rem Patient app: http://127.0.0.1:8000   Admin dashboard: http://127.0.0.1:8000/admin (set its password with
rem set-admin-password.bat first)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The app is not installed yet. In this folder, run once ^(Python 3.12 or newer^):
  echo   py -3 -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)
if not exist ".env" (
  echo Missing .env file - copy .env.example to .env and fill it in.
  pause
  exit /b 1
)
rem Edge opens once the server answers, which is after the practice voice has loaded (about 10-20 seconds).
start "" powershell -NoProfile -WindowStyle Hidden -Command "for ($i = 0; $i -lt 180; $i++) { try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8000/api/config | Out-Null; Start-Process msedge http://127.0.0.1:8000; break } catch { Start-Sleep -Seconds 1 } }"
".venv\Scripts\python.exe" -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
pause
