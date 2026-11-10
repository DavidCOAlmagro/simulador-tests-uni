@echo off
REM Lanza el test en Windows (necesita Python 3.10 o superior de python.org)
cd /d "%~dp0"
py test_app.py %* || python test_app.py %*
if errorlevel 1 pause
