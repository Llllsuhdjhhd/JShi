@echo off
REM 默认原来的一行输入。全屏套壳：talk.cmd --tui（需 pip install textual；推荐 Windows Terminal）
cd /d "%~dp0"
set PYTHONPATH=src
python -m jshi.app.cli talk %*
