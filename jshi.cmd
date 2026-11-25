@echo off
setlocal

set "JSHI_ROOT=%~dp0"
pushd "%JSHI_ROOT%" >nul

if defined JSHI_PYTHON (
    set "JSHI_PYTHON_EXE=%JSHI_PYTHON%"
) else if exist "%JSHI_ROOT%.jshi\python-path.txt" (
    set /p "JSHI_PYTHON_EXE="<"%JSHI_ROOT%.jshi\python-path.txt"
) else if defined CONDA_PREFIX (
    set "JSHI_PYTHON_EXE=%CONDA_PREFIX%\python.exe"
) else if exist "%JSHI_ROOT%.venv\Scripts\python.exe" (
    set "JSHI_PYTHON_EXE=%JSHI_ROOT%.venv\Scripts\python.exe"
) else if exist "%USERPROFILE%\miniconda3\python.exe" (
    set "JSHI_PYTHON_EXE=%USERPROFILE%\miniconda3\python.exe"
) else (
    set "JSHI_PYTHON_EXE=python"
)

set "PYTHONPATH=%JSHI_ROOT%src;%PYTHONPATH%"
if /i "%~1"=="model-desk" goto model_desk
"%JSHI_PYTHON_EXE%" -m jshi.app.cli --data-dir "%JSHI_ROOT%.jshi" %*
goto finished

:model_desk
if /i "%~2"=="--setup" goto model_desk_setup
set "PYTHONUTF8=1"
"%JSHI_PYTHON_EXE%" "%JSHI_ROOT%tools\model_desk\manager.py"
goto finished

:model_desk_setup
"%JSHI_PYTHON_EXE%" -m pip install -e ".[voice,vision]"

:finished
set "JSHI_EXIT_CODE=%ERRORLEVEL%"

popd >nul
endlocal & exit /b %JSHI_EXIT_CODE%
