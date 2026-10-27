@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First create .venv and install requirements. See README.md.
  pause
  exit /b 2
)
".venv\Scripts\python.exe" run.py %*
set "task_exit=%errorlevel%"
if not "%task_exit%"=="0" pause
exit /b %task_exit%
