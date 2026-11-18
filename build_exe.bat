@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo Python не найден. Установите с python.org ^(галочка Add to PATH^) & pause & exit /b 1)
python -m pip install --upgrade pyinstaller || (pause & exit /b 1)
python -m PyInstaller --onefile --noconsole --clean --name UpdateChecker-Portable update_checker.py || (pause & exit /b 1)
echo.
echo Готово: dist\UpdateChecker-Portable.exe
pause
