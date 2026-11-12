@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

rem MAGIC ZIP se ejecuta con Python y no necesita MagicZip.exe.
set "MAGIC_PYTHON="
set "MAGIC_PYTHON_ARGS="

rem 1) Entorno local del proyecto.
if exist "%~dp0.venv\Scripts\python.exe" set "MAGIC_PYTHON=%~dp0.venv\Scripts\python.exe"
if defined MAGIC_PYTHON call :validate_python
if defined MAGIC_PYTHON if not errorlevel 1 goto launch
set "MAGIC_PYTHON="

rem 2) Python instalado por el usuario, sin privilegios de administrador.
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "MAGIC_PYTHON=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if defined MAGIC_PYTHON call :validate_python
if defined MAGIC_PYTHON if not errorlevel 1 goto launch
set "MAGIC_PYTHON="

rem 3) Python Launcher de Windows.
where py >nul 2>&1
if errorlevel 1 goto try_path_python
set "MAGIC_PYTHON=py"
set "MAGIC_PYTHON_ARGS=-3.11"
call :validate_python
if not errorlevel 1 goto launch
set "MAGIC_PYTHON="
set "MAGIC_PYTHON_ARGS="

:try_path_python
rem 4) Python disponible en PATH.
where python >nul 2>&1
if errorlevel 1 goto no_python
set "MAGIC_PYTHON=python"
call :validate_python
if not errorlevel 1 goto launch

:no_python
echo.
echo No se encontro un entorno Python valido con Pillow y pypdf.
echo.
echo Opciones permitidas para preparar el entorno:
echo   py -3.11 -m venv .venv
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
echo Si el equipo bloquea instalaciones, solicita a TI Python 3.11+ y las dependencias del proyecto.
pause
exit /b 1

:validate_python
"%MAGIC_PYTHON%" %MAGIC_PYTHON_ARGS% -c "import sys; import PIL; import pypdf; print(sys.version.split()[0])" >nul 2>&1
exit /b %errorlevel%

:launch
echo MAGIC ZIP 2.0 - powered by FiscalOps - BY LAVA
echo Modo local: Python + navegador en 127.0.0.1
echo Mantén esta ventana abierta mientras usas la aplicacion.
echo Para detener el motor, presiona Ctrl+C aqui.
echo.
"%MAGIC_PYTHON%" %MAGIC_PYTHON_ARGS% app.py
set "MAGIC_EXIT=%errorlevel%"
if not "%MAGIC_EXIT%"=="0" (
  echo.
  echo MAGIC ZIP termino con un error. Revisa el mensaje anterior.
  pause
)
exit /b %MAGIC_EXIT%
