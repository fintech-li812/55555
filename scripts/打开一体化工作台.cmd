@echo off
chcp 65001 >nul
rem 双击即可：先起隔离库（3307），再起 V8（8010）与主站（8000），最后打开统一工作台。
rem 首次冷启动要为隔离库做崩溃恢复并重建 undo 表空间，可能等待 1~2 分钟，属正常。
rem 只想快速看页面、不需要保存/回查/留痕时，去掉下面的 -WithDatabase。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0启动一体化工作台.ps1" -WithDatabase -OpenBrowser
