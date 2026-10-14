@echo off
setlocal
cd /d "%~dp0"

set "PYEXE="
if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PYEXE if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python314\python.exe"

if not defined PYEXE (
  echo [ERROR] Cannot find Python under LocalAppData\Programs\Python
  echo Install Python 3.11+ or run dist\SwitchBotMeter\start.bat
  pause
  exit /b 1
)

set "PYWEXE=%PYEXE:python.exe=pythonw.exe%"
if not exist "%PYWEXE%" set "PYWEXE=%PYEXE%"

echo Using: %PYEXE%

"%PYEXE%" -c "import bleak, winotify" >nul 2>&1
if errorlevel 1 goto :install_deps
goto :check_app

:install_deps
echo Installing requirements...
"%PYEXE%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] pip install failed
  pause
  exit /b 1
)

:check_app
"%PYEXE%" -c "import core; import app" 2> "%TEMP%\switchbot_meter_boot.err"
if errorlevel 1 (
  echo [ERROR] Startup check failed:
  type "%TEMP%\switchbot_meter_boot.err"
  pause
  exit /b 1
)

start "" "%PYWEXE%" "%~dp0app.py"
endlocal
exit /b 0
