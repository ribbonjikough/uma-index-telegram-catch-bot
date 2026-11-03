@echo off
REM Goes to this script's own folder, activates .venv and starts the bot
cd /d "%~dp0"
if not exist ".venv\Scripts\activate.bat" (
    echo Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)
call ".venv\Scripts\activate.bat"
python bot.py
pause
