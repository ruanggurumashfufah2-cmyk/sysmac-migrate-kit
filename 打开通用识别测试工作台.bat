@echo off
cd /d "%~dp0."
set PYTHONIOENCODING=utf-8
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scripts\workbench\server.py --root . --port 8766
) else (
  python scripts\workbench\server.py --root . --port 8766
)
if errorlevel 1 pause
