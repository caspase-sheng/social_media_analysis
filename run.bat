@echo off
chcp 65001 >nul
REM ===============================================================
REM  Social Media Rumor Detection System - one click launcher
REM  Steps: activate conda env -> init database -> start Flask
REM
REM  NOTE 1: conda is installed at D:\conda and is NOT in the system
REM          PATH, so "conda activate" cannot be used here. We call
REM          activate.bat by its full path instead.
REM          If your conda is somewhere else, edit CONDA_ACTIVATE.
REM
REM  NOTE 2: this file is kept ASCII-only on purpose. cmd.exe reads
REM          .bat files with the OEM code page (936 on Chinese
REM          Windows), so UTF-8 Chinese comments get garbled and
REM          break the script. Do not add Chinese text here.
REM ===============================================================

set "CONDA_ACTIVATE=D:\conda\Scripts\activate.bat"

if not exist "%CONDA_ACTIVATE%" (
    echo [ERROR] Cannot find conda activate script: %CONDA_ACTIVATE%
    echo         Check where conda is installed, then update CONDA_ACTIVATE.
    pause
    exit /b 1
)

call "%CONDA_ACTIVATE%" rumor_detection
if errorlevel 1 (
    echo [ERROR] Failed to activate conda env "rumor_detection".
    echo         Run "conda env list" to see the environments you have.
    pause
    exit /b 1
)

echo [1/2] Initializing database...
REM Tables are only created when missing, so it is safe to run repeatedly.
python init_db.py
if errorlevel 1 (
    echo [ERROR] Database init failed.
    echo         Check that the MySQL service is running and that the
    echo         password in config.py is correct.
    pause
    exit /b 1
)

echo [2/2] Starting web server, open http://127.0.0.1:5000 in a browser
python app.py

pause