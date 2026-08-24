@echo off
REM ═══════════════════════════════════════════════════════════════════════
REM  RaceEngine V2 - arranque en Windows
REM  Doble clic, o desde CMD/PowerShell:  .\run.bat
REM  No usa PowerShell, asi que la politica de ejecucion no le afecta.
REM ═══════════════════════════════════════════════════════════════════════
setlocal
cd /d "%~dp0"

set "PYEXE="
call :probe py -3.13
if not defined PYEXE call :probe py -3.12
if not defined PYEXE call :probe python
if not defined PYEXE call :probe python3
if not defined PYEXE goto :nopython

if not exist ".venv\Scripts\python.exe" (
  echo ^> creando entorno virtual...
  %PYEXE% -m venv .venv
  if errorlevel 1 goto :venvfail
  echo ^> instalando dependencias...
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
  if errorlevel 1 goto :pipfail
)

".venv\Scripts\python.exe" serve.py %*
if errorlevel 1 pause
goto :end

:probe
%* -c "import sys;sys.exit(0 if sys.version_info>=(3,12) else 1)" >nul 2>&1
if not errorlevel 1 set "PYEXE=%*"
goto :eof

:nopython
echo.
echo ──────────────────────────────────────────────────────────
echo   No se encontro Python 3.12 o superior.
echo.
echo   Descargalo de https://www.python.org/downloads/
echo   IMPORTANTE: marca "Add python.exe to PATH" al instalar.
echo ──────────────────────────────────────────────────────────
echo.
pause
goto :end

:venvfail
echo.
echo   ERROR: no se pudo crear el entorno virtual.
echo   Prueba a ejecutar:  %PYEXE% -m pip install --user virtualenv
pause
goto :end

:pipfail
echo.
echo   ERROR: fallo la instalacion de dependencias.
echo   Revisa tu conexion y vuelve a ejecutar run.bat
pause

:end
endlocal
