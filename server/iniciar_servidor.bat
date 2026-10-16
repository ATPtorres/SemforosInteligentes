@echo off
chcp 65001 >nul
title SmartTraffic - Backend
cd /d "%~dp0"

echo ============================================
echo   SMARTTRAFFIC - servidor local
echo   Dashboard: http://localhost:8000
echo ============================================

if not exist ".venv\Scripts\python.exe" (
    echo Creando entorno virtual e instalando dependencias...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)

echo.
echo Iniciando servidor...
.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
pause
