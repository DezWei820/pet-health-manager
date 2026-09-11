@echo off
title Pet Health Manager
set PY=C:\Users\weidezhao\PycharmProjects\PythonProject\.venv\Scripts\python.exe
set NPM=C:\Users\weidezhao\tools\nodejs\node-v22.23.2-win-x64\npm.cmd
set FRONT=C:\Users\weidezhao\PycharmProjects\PythonProject\pet-frontend

echo ============================================
echo   Pet Health Manager - One-Click Start
echo   Backend : http://127.0.0.1:8000
echo   Frontend: http://127.0.0.1:5173
echo ============================================
echo.
echo [1/2] Starting backend (first start takes 30-60s)...
cd /d C:\Users\weidezhao\PycharmProjects\PythonProject
start "backend-8000" cmd /k ""%PY%" -m uvicorn backend:app --host 127.0.0.1 --port 8000"

echo [2/2] Starting frontend...
cd /d "%FRONT%"
start "frontend-5173" cmd /k ""%NPM%" run dev"

timeout /t 5 >nul
start http://127.0.0.1:5173
echo.
echo Browser opened. Keep both windows running.
echo Wait for "Application startup complete" in backend window, then use the page.
pause
