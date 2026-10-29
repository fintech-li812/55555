@echo off
chcp 65001 >nul
rem 停止本机的一体化工作台后端（主站 8000 + V8 8010）。
rem 通过 scripts/serve_local.py --stop 按 pid 文件与端口精确回收，不会误杀别的 Python 进程。
set PY=C:\Users\33847\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python
"%PY%" "%~dp0serve_local.py" --stop
echo.
pause
