@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Building 分包打包器.exe ...
python -m pip install --disable-pip-version-check -q PySide6 pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "分包打包器" ^
  --icon "%~dp0icon.ico" --distpath "%~dp0dist" --workpath "%~dp0_build" --specpath "%~dp0_build" ^
  "%~dp0zippack_gui.py"
if errorlevel 1 (
  echo Build failed.
) else (
  echo.
  echo Done: %~dp0dist\分包打包器.exe
  echo Verify with:  dist\分包打包器.exe --selftest
)
pause
