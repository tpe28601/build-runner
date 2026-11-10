@echo off
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
if defined DEVTOOLBOX_PYTHON (
    "%DEVTOOLBOX_PYTHON%" -m build_runner.cli %*
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
