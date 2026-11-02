@echo off
rem Double-click to run Encompass Field Finder on this computer (from source).
rem First run creates a private Python environment in .venv (needs Python 3.9+ and internet once).
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo Python 3 was not found on this computer.
  echo Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
  echo or run in a terminal:  winget install Python.Python.3.12
  echo Alternatively download the ready-made EncompassFieldFinder.exe - see README.md.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Setting up for first use...
  %PY% -m venv .venv || goto :err
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --quiet -e ".[app,yaml]" || goto :err
".venv\Scripts\python.exe" -m encompass_field_finder.app %*
exit /b 0
:err
echo.
echo Setup failed - see the messages above.
pause
exit /b 1
