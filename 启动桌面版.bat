@echo off
rem 双击本文件 → 弹出桌面窗口（与打包 exe 相同体验）。
rem 无控制台黑框（pythonw）；启动失败信息见 errors.log。
cd /d "%~dp0"
start "" pythonw run.py
