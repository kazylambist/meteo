@echo off
REM Activate venv and run the app
if not exist venv\Scripts\activate.bat (
  echo Virtual environment not found. Run win-setup.bat first.
  exit /b 1
)
call venv\Scripts\activate
python mood-speculator-v2.py
