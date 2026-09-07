@echo off
REM Lancement sous Windows : double-cliquez ce fichier, ou lancez-le depuis un terminal.
setlocal
cd /d "%~dp0"

REM Environnement virtuel local s'il existe, sinon le lanceur Python de Windows.
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
)

%PY% launch.py
if errorlevel 1 pause
endlocal
