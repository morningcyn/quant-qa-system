# -*- coding: utf-8 -*-
"""打包「助理会话质检助手」为单文件 exe（PyInstaller）。
用法：python scripts/build_exe.py
产物：dist/助理会话质检助手.exe（onefile，含前端静态资源与 pywebview 运行时）
运行：exe 同级自动创建 data/（SQLite 库与本地数据，可随包拷贝迁移）。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable)
NAME = "助理会话质检助手"
SEP = os.pathsep  # Windows 下为 ";"，PyInstaller --add-data 分隔符

args = [
    str(PY),
    "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--onefile", "--windowed",
    "--name", NAME,
    "--add-data", f"{ROOT / 'frontend'}{SEP}frontend",
    # pywebview edgechromium 平台需要 WebView2Loader.dll（6.2.1 自带在 webview/lib/runtimes）
    "--hidden-import", "webview.platforms.edgechromium",
    "--collect-all", "webview",
    "--collect-all", "backend",
    str(ROOT / "run.py"),
]
print("PyInstaller 构建中（首次约 2~4 分钟）…")
subprocess.run(args, cwd=str(ROOT), check=True)

dist_exe = ROOT / "dist" / f"{NAME}.exe"
if not dist_exe.exists():
    sys.exit(f"构建失败：{dist_exe} 不存在")
print(f"\n构建完成：{dist_exe}（{dist_exe.stat().st_size / 1024 / 1024:.1f} MB）")
