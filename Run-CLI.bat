@echo off
setlocal
set "TB_ACTIVE_TOOL="
for /f "delims=" %%I in ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Standalone-Update.ps1" -Action Resolve') do set "TB_ACTIVE_TOOL=%%I"
if defined TB_ACTIVE_TOOL (
    if not defined DEVTOOLBOX_PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "DEVTOOLBOX_PYTHON=%~dp0.venv\Scripts\python.exe"
    call "%TB_ACTIVE_TOOL%\Run-CLI.bat" %*
    exit /b
)
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "TB_PYTHON="
for /f "delims=" %%I in ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Standalone-Update.ps1" -Action Python') do set "TB_PYTHON=%%I"
if not defined TB_PYTHON (
    echo Compatible Python 3.11+ was not found. Download the package with Python included or install compatible Python.
    pause
    exit /b 5
)
"%TB_PYTHON%" -m build_runner.cli %*
exit /b %ERRORLEVEL%
