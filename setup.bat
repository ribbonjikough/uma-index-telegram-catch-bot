::ONLY RUN THIS ONCE, THEN RUN autorun.bat
::RUN IN YOUR CHOSEN DIRECTORY, NOT IN THE REPO ROOT

@echo off
REM Creates .venv, installs requirements and Chromium (run once)
cd /d "%~dp0"
if not exist ".venv\Scripts\activate.bat" python -m venv .venv
call ".venv\Scripts\activate.bat"
pip install -r requirements.txt
playwright install chromium
if not exist ".env" copy ".env.example" ".env"
echo.
echo Setup done. Edit .env and add your TELEGRAM_BOT_TOKEN, then run autorun.bat
pause
