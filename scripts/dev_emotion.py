# 情绪分析专用开发界面：只起后端 + 系统浏览器打开（便于测试报告页【客户情绪分析】）。
# 与 run.py --dev 的区别：禁用断点续跑（resume_all）——绝不触碰正在运行的批量批次。
# 用法：python scripts/dev_emotion.py
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn

from backend.main import create_app
from backend.services.batch import manager as mgr_mod

# 禁用断点续跑：本实例只用于情绪分析验收，不得启动 worker 干扰用户正在运行的批次
# （lifespan 调 mgr.resume_all()，mgr 是实例 → 必须替换实例属性）
mgr_mod.mgr.resume_all = lambda: None  # type: ignore[method-assign]

app = create_app()

from run import find_free_port  # noqa: E402

port = find_free_port()
url = f"http://127.0.0.1:{port}"
print(f"情绪分析开发界面：{url}  （浏览器已打开，F12 可调试）")
print("提示：报告页【客户情绪分析】卡片未生成时点「生成情绪分析」，POST /api/emotion/analyze")
webbrowser.open(url)
uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
