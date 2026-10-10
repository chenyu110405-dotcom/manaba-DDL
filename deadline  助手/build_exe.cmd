@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo 正在准备打包「作业泡泡.exe」...
if not exist ".venv\Scripts\python.exe" (
  echo 正在创建虚拟环境...
  py -3 -m venv .venv
  if errorlevel 1 goto failed
)

echo 正在安装依赖与 PyInstaller...
".venv\Scripts\python.exe" -m pip install -e .
".venv\Scripts\python.exe" -m pip install pyinstaller
if errorlevel 1 goto failed

echo 正在打包中，请稍候...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm 作业泡泡.spec
if errorlevel 1 goto failed

echo.
echo ========================================
echo 打包成功！
echo 生成的 EXE 文件位于：dist\作业泡泡.exe
echo ========================================
pause
exit /b 0

:failed
echo 打包失败，请检查网络或 Python 环境。
pause
exit /b 1
