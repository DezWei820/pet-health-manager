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

echo Waiting for backend to be ready (be patient on first start)...
set N=0
:waitloop
timeout /t 2 /nobreak >nul
set /a N+=1
if %N% geq 120 (
    echo Backend did not start in time. Check MySQL/Redis and the backend window.
    goto :end
)
powershell -NoProfile -Command "try{$r=Invoke-WebRequest -Uri 'http://127.0.0.1:8000/docs' -UseBasicParsing -TimeoutSec 2; if($r.StatusCode -eq 200){exit 0}else{exit 1}}catch{exit 1}" >nul 2>&1
if errorlevel 1 goto waitloop
echo Backend is ready!

echo [2/2] Starting frontend...
cd /d "%FRONT%"
start "frontend-5173" cmd /k ""%NPM%" run dev"

timeout /t 4 >nul
start http://127.0.0.1:5173
echo.
echo All ready. Browser opened - data loads instantly now.
:end
pause
