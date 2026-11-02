@echo off
rem Build EncompassFieldFinder.exe locally on Windows (needs Python 3.9+ on PATH).
setlocal
cd /d "%~dp0\.."
py -3 -m venv .build-venv || python -m venv .build-venv || goto :err
call .build-venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install . keyring pyinstaller || goto :err
pyinstaller --noconfirm --onefile --console --name EncompassFieldFinder ^
  --collect-data encompass_field_finder ^
  --collect-submodules keyring.backends ^
  packaging\efinder_app_entry.py || goto :err
echo.
echo Built: %CD%\dist\EncompassFieldFinder.exe
pause
exit /b 0
:err
echo Build failed.
pause
exit /b 1
