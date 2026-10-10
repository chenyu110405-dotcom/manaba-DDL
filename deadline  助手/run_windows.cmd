@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 请先双击运行 setup_windows.cmd 安装环境。
  pause
  exit /b 1
)
start http://127.0.0.1:8765/
".venv\Scripts\python.exe" -m manaba_deadline serve
if errorlevel 1 pause
