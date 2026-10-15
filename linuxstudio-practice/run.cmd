@echo off
setlocal
set PYTHONUTF8=1
if exist "%~dp0.venv\Scripts\python.exe" goto venv
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0scripts\launcher.py" %*
) else (
  python "%~dp0scripts\launcher.py" %*
)
exit /b %errorlevel%
:venv
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\linuxstudio.py" %*
exit /b %errorlevel%
