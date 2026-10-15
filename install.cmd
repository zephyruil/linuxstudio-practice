@echo off
setlocal
set PYTHONUTF8=1
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0install.py" %*
) else (
  python "%~dp0install.py" %*
)
exit /b %errorlevel%
