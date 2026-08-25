# 开发模式：只起后端 + 系统浏览器打开（与 run.py --dev 等价，便于 IDE 调试）
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.argv.append("--dev")
from run import main  # noqa: E402

if __name__ == "__main__":
    main()
