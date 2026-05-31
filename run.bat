@echo off
REM Inicia o Moodlebot usando o ambiente virtual criado pelo setup.bat
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"

if not exist ".venv\Scripts\python.exe" (
  echo [ERRO] Ambiente virtual nao encontrado. Rode setup.bat primeiro.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" main.py
pause
