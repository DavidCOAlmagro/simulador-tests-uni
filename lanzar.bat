@echo off
REM Lanza el test en Windows (necesita Python 3.10 o superior de python.org)
cd /d "%~dp0"
REM pyw/pythonw abren la app sin ventana de consola, y el .bat se cierra al momento
where pyw >nul 2>nul && (start "" pyw test_app.py %* & exit /b)
where pythonw >nul 2>nul && (start "" pythonw test_app.py %* & exit /b)
REM si no hay ninguno de los dos, se abre como antes, con la consola
py test_app.py %* || python test_app.py %*
if errorlevel 1 pause
