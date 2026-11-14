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
if defined DEVTOOLBOX_PYTHON (
    "%DEVTOOLBOX_PYTHON%" -m build_runner.cli %*
) else if exist "%~dp0runtime\python\python.exe" (
    "%~dp0runtime\python\python.exe" -m build_runner.cli %*
) else if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -m build_runner.cli %*
) else (
    where py.exe >nul 2>&1
    if errorlevel 1 (
        python -m build_runner.cli %*
    ) else (
        py -3 -m build_runner.cli %*
    )
)
exit /b %ERRORLEVEL%
