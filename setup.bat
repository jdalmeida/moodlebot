@echo off
REM ============================================================
REM   Moodlebot - Setup automatico para Windows
REM   Cria venv, instala dependencias, garante um navegador,
REM   configura o .env, cria o banco e faz o login no Moodle.
REM ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"

echo ============================================================
echo   Moodlebot - Setup automatico (Windows)
echo ============================================================
echo.

REM --- 1. Localizar o Python -------------------------------------------------
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE (
  where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo [ERRO] Python nao encontrado no PATH.
  echo        Instale o Python 3.11+ em https://www.python.org/downloads/
  echo        Marque "Add python.exe to PATH" durante a instalacao e rode de novo.
  echo.
  pause
  exit /b 1
)
echo [OK] Python encontrado:
%PYEXE% --version
echo.

REM --- 2. Ambiente virtual --------------------------------------------------
if exist ".venv\Scripts\python.exe" (
  echo [OK] venv ja existe ^(.venv^) - reutilizando.
) else (
  echo [..] Criando ambiente virtual em .venv ...
  %PYEXE% -m venv .venv
  if errorlevel 1 (
    echo [ERRO] Falha ao criar o ambiente virtual.
    pause
    exit /b 1
  )
  echo [OK] venv criado.
)
set "VENVPY=.venv\Scripts\python.exe"
echo.

REM --- 3. Dependencias ------------------------------------------------------
echo [..] Atualizando pip ...
"%VENVPY%" -m pip install --upgrade pip >nul 2>nul
echo [..] Instalando dependencias (pode demorar alguns minutos) ...
"%VENVPY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERRO] Falha ao instalar dependencias.
  echo        Se o erro for de compilacao ^(lxml/pydantic-core^), tente o
  echo        Python 3.12 ou 3.13, que tem wheels prontas para Windows.
  pause
  exit /b 1
)
echo [OK] Dependencias instaladas.
echo.

REM --- 4. Navegador para o Playwright ---------------------------------------
echo [..] Procurando um navegador instalado ...
set "BROWSER_CHANNEL=chrome"
set "CHROME_FOUND="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"

if defined CHROME_FOUND (
  echo [OK] Google Chrome detectado - usando BROWSER_CHANNEL=chrome
  set "BROWSER_CHANNEL=chrome"
  goto browser_done
)

set "EDGE_FOUND="
if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "EDGE_FOUND=1"
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "EDGE_FOUND=1"
if defined EDGE_FOUND (
  echo [OK] Microsoft Edge detectado - usando BROWSER_CHANNEL=msedge
  set "BROWSER_CHANNEL=msedge"
  goto browser_done
)

echo [..] Nenhum Chrome/Edge encontrado. Baixando o Chromium via Playwright ...
"%VENVPY%" -m playwright install chromium
if errorlevel 1 (
  echo [ERRO] Falha ao baixar o Chromium do Playwright.
  pause
  exit /b 1
)
set "BROWSER_CHANNEL="
echo [OK] Chromium do Playwright instalado.
:browser_done
echo.

REM --- 5. Arquivo .env ------------------------------------------------------
if exist ".env" (
  echo [OK] .env ja existe - mantendo a configuracao atual.
  echo      ^(Apague o .env e rode de novo para reconfigurar.^)
  goto env_done
)
call :make_env
:env_done
echo.

REM --- 6. Banco de dados SQLite ---------------------------------------------
echo [..] Inicializando o banco SQLite ...
"%VENVPY%" scripts\init_db.py
if errorlevel 1 (
  echo [ERRO] Falha ao inicializar o banco.
  pause
  exit /b 1
)
echo.

REM --- 7. Login institucional no Moodle -------------------------------------
set "DOLOGIN=S"
if exist "data\storage_state.json" (
  echo [OK] Sessao do Moodle ja existe ^(data\storage_state.json^).
  set "DOLOGIN=N"
  set /p "DOLOGIN=Refazer o login agora? [s/N]: "
)
if /i not "!DOLOGIN!"=="S" goto after_login
echo.
echo [..] Vai abrir o navegador para o login do Moodle.
echo      Faca o SSO completo, espere aparecer o dashboard e entao
echo      volte ao terminal e pressione ENTER.
echo.
"%VENVPY%" scripts\first_login.py
if errorlevel 1 (
  echo [AVISO] O login pode nao ter sido concluido. Voce pode refazer com:
  echo         .venv\Scripts\python.exe scripts\first_login.py
)
:after_login
echo.

echo ============================================================
echo   Setup concluido!
echo ============================================================
echo  Para iniciar o agente, rode:
echo      run.bat
echo  ou diretamente:
echo      .venv\Scripts\python.exe main.py
echo.
pause
exit /b 0

REM ==========================================================================
REM  Subrotina: cria o .env interativamente
REM ==========================================================================
:make_env
echo Vamos configurar o arquivo .env
echo ------------------------------------------------------------
set "RUN_MODE=terminal"
set /p "RUN_MODE=Modo de execucao [terminal/telegram] (terminal): "
if /i "!RUN_MODE!"=="telegram" (set "RUN_MODE=telegram") else (set "RUN_MODE=terminal")

set "MOODLE_BASE_URL=https://portalvirtual.unisc.br/moodle"
set "INP="
set /p "INP=URL base do Moodle (!MOODLE_BASE_URL!): "
if not "!INP!"=="" set "MOODLE_BASE_URL=!INP!"

set "GOOGLE_API_KEY="
set /p "GOOGLE_API_KEY=Chave da API do Google Gemini (necessaria para o agente): "

set "TELEGRAM_BOT_TOKEN="
set "TELEGRAM_ALLOWED_USER_IDS="
if /i "!RUN_MODE!"=="telegram" (
  set /p "TELEGRAM_BOT_TOKEN=Token do bot Telegram (BotFather): "
  set /p "TELEGRAM_ALLOWED_USER_IDS=Seu user_id Telegram (varios separados por virgula): "
)

(
  echo RUN_MODE=!RUN_MODE!
  echo MOODLE_BASE_URL=!MOODLE_BASE_URL!
  echo MOODLE_STORAGE_STATE=data/storage_state.json
  echo MOODLE_HEADLESS=false
  echo MOODLE_REQUEST_TIMEOUT_MS=30000
  echo BROWSER_CHANNEL=!BROWSER_CHANNEL!
  echo BROWSER_EXECUTABLE_PATH=
  echo TELEGRAM_BOT_TOKEN=!TELEGRAM_BOT_TOKEN!
  echo TELEGRAM_ALLOWED_USER_IDS=!TELEGRAM_ALLOWED_USER_IDS!
  echo DATABASE_PATH=data/moodlebot.db
  echo POLL_INTERVAL_MINUTES=15
  echo LOG_LEVEL=INFO
  echo GOOGLE_API_KEY=!GOOGLE_API_KEY!
  echo LLM_MODEL=gemini-2.5-flash
  echo MOODLE_DEBUG=false
) > ".env"
echo [OK] .env criado.
goto :eof
