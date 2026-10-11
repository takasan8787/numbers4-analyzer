@echo off
rem タスクスケジューラから呼び出す起動用バッチ
chcp 65001 > nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
py -3 "%~dp0podcast_updater.py" %*
exit /b %ERRORLEVEL%
