@echo off
echo Starting sample build
echo Argument %~2
if "%~1"=="gate" (
  "%BUILD_RUNNER_TEST_PYTHON%" -c "import pathlib, time; pathlib.Path('ready.txt').touch(); next((None for _ in range(200) if pathlib.Path('release.txt').exists() or time.sleep(0.05)), None)"
  echo - Done -
  exit /b 0
)
if "%~1"=="hold" (
  "%BUILD_RUNNER_TEST_PYTHON%" -c "import time; time.sleep(30)"
  exit /b 0
)
if "%~1"=="success" (
  echo - Done -
  exit /b 0
)
echo build.py : error 7000: command failed
echo sample.c(12): error C2065: unknown identifier 1>&2
echo NMAKE : fatal error U1077: command failed
echo - Failed -
exit /b 1
