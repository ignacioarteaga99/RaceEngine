@echo off
REM Construye el grafo de una region.   Uso:  build.bat santiago_centro
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Primero ejecuta run.bat para crear el entorno.
  pause
  goto :eof
)
".venv\Scripts\python.exe" build.py %*
if errorlevel 1 pause
endlocal
