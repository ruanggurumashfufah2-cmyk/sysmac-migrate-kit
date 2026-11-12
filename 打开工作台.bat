@echo off
title Sysmac 迁移工作台
cd /d "%~dp0"
echo 正在启动 Sysmac 迁移工作台，浏览器会自动打开；用完关闭本窗口即可。
python "%~dp0scripts\workbench\server.py" --root "%~dp0."
if errorlevel 1 (
  echo.
  echo 启动失败：请确认已安装 Python，并在本目录运行过  pip install -r requirements.txt
  pause
)
