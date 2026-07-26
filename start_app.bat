@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Первый запуск — настраиваю программу, это займёт пару минут...
    echo.

    where py >nul 2>nul
    if %errorlevel%==0 (
        py -3 -m venv .venv
    ) else (
        python -m venv .venv
    )

    if not exist ".venv\Scripts\python.exe" (
        echo.
        echo Не найден Python. Установите Python 3.11 или новее:
        echo   https://www.python.org/downloads/
        echo При установке обязательно отметьте галочку "Add python.exe to PATH".
        echo Затем запустите этот файл ещё раз.
        echo.
        pause
        exit /b 1
    )

    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

".venv\Scripts\python.exe" run_app.py

echo.
echo Программа остановлена.
pause
