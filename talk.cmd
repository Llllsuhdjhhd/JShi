@echo off
REM Default: one-line prompt. Full screen: talk.cmd --tui
REM Use conda env py3125. PATH python is base; torch will fail there.
cd /d "%~dp0"
set "PYTHONPATH=src"
if exist "%~dp0..\Jshi_memory\src\rems\port.py" (
  set "PYTHONPATH=%~dp0src;%~dp0..\Jshi_memory\src"
)
set "PY3125=%USERPROFILE%\anaconda3\envs\py3125\python.exe"
if exist "%PY3125%" (
  "%PY3125%" -m jshi.app.cli talk %*
) else (
  echo [jshi] py3125 not found, falling back to PATH python. Recall may fail. 1>&2
  python -m jshi.app.cli talk %*
)
