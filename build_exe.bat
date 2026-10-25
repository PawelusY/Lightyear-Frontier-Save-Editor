@echo off
setlocal
cd /d "%~dp0"
py -m pip install --upgrade pyinstaller
if errorlevel 1 goto error
py -m PyInstaller --noconfirm --clean --onefile --windowed --name LightyearFrontierSaveEditor LightyearFrontierSaveEditor.py
if errorlevel 1 goto error
echo.
echo Build complete: dist\LightyearFrontierSaveEditor.exe
pause
exit /b 0
:error
echo Build failed. Check that Python and pip are installed and available via the py command.
pause
exit /b 1
