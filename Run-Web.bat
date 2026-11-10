@echo off
setlocal
if defined DEVTOOLBOX_PYTHON (
    "%DEVTOOLBOX_PYTHON%" "%~dp0build_runner\web.py" --open-browser %*
) else if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0build_runner\web.py" --open-browser %*
) else (
    where py.exe >nul 2>&1
    if errorlevel 1 (
        python "%~dp0build_runner\web.py" --open-browser %*
    ) else (
        py -3 "%~dp0build_runner\web.py" --open-browser %*
    )
)
exit /b %ERRORLEVEL%
