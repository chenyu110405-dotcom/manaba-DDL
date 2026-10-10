@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto failed
echo 安装完成。请运行 run_windows.cmd。
pause
exit /b 0
:failed
echo 安装失败。请确认已安装 Python 3.10 或更新版本，并检查网络。
pause
exit /b 1
