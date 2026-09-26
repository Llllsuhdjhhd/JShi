@echo off
setlocal

set "JSHI_ROOT=%~dp0"
pushd "%JSHI_ROOT%" >nul

if defined JSHI_PYTHON (
    set "JSHI_PYTHON_EXE=%JSHI_PYTHON%"
) else if defined CONDA_PREFIX (
    set "JSHI_PYTHON_EXE=%CONDA_PREFIX%\python.exe"
) else if exist "%USERPROFILE%\miniconda3\python.exe" (
    set "JSHI_PYTHON_EXE=%USERPROFILE%\miniconda3\python.exe"
) else (
    set "JSHI_PYTHON_EXE=python"
)

set "PYTHONPATH=%JSHI_ROOT%src;%PYTHONPATH%"
"%JSHI_PYTHON_EXE%" -m jshi.app.cli --data-dir "%JSHI_ROOT%.jshi" %*
set "JSHI_EXIT_CODE=%ERRORLEVEL%"

popd >nul
endlocal & exit /b %JSHI_EXIT_CODE%
