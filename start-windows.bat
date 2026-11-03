@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
set "CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"
where py >nul 2>nul && py -3 -c "%CHECK%" >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "%CHECK%" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo 没有找到可用的 Python 3.8 或更新版本。
  echo 请到 https://www.python.org/downloads/ 下载安装，安装时勾选 "Add python.exe to PATH"，装好后重新双击本文件。
  pause
  exit /b 1
)
%PY% run.py %*
if errorlevel 1 pause
