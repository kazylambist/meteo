@echo off
REM Create a virtual environment and install dependencies
py -3 -m venv venv
if errorlevel 1 goto :error
call venv\Scripts\pip install --upgrade pip
if errorlevel 1 goto :error
call venv\Scripts\pip install -r requirements.txt
if errorlevel 1 goto :error
echo.
echo Setup OK.
goto :eof

:error
echo.
echo [ERROR] Setup failed. Make sure Python 3 is installed and on PATH.
exit /b 1
