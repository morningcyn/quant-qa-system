# 桌面入口：本地后端线程 + pywebview 窗口。
#   python run.py          → 桌面窗口模式
#   python run.py --dev    → 开发模式：只起后端并用系统浏览器打开（F12 调试）
import base64
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from backend.config import APP_VERSION, DATA_DIR

APP_TITLE = "客服会话质检助手"


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_backend(port: int) -> None:
    def _run():
        uvicorn.run(
            "backend.main:app",
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


def wait_healthy(url: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


class JsApi:
    """暴露给前端的本地能力（pywebview js_api bridge）。"""

    def get_version(self):
        return APP_VERSION

    def save_file(self, suggested_name: str, b64: str):
        """弹系统保存对话框，把 base64 内容写盘，返回保存路径。"""
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG, save_filename=suggested_name
        )
        path = result[0] if isinstance(result, (list, tuple)) and result else result
        if not path:
            return None
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return str(path)

    def open_data_dir(self):
        os.startfile(str(DATA_DIR))  # noqa: S606


def main() -> None:
    port = find_free_port()  # OS 随机分配，天然规避端口冲突
    url = f"http://127.0.0.1:{port}"
    start_backend(port)
    if not wait_healthy(url):
        print("后端启动失败，请检查 Python 依赖是否完整（pip install -r requirements.txt）")
        sys.exit(1)

    if "--dev" in sys.argv:
        print(f"开发模式：浏览器访问 {url}")
        webbrowser.open(url)
        return

    try:
        import webview

        webview.create_window(
            APP_TITLE,
            url,
            width=1280,
            height=820,
            min_size=(1080, 720),
            background_color="#0d0d0d",
            js_api=JsApi(),
        )
        webview.start(gui="edgechromium")  # 主线程阻塞 GUI 循环；窗口关闭即退出
    except ImportError:
        print("未安装 pywebview，请执行：pip install pywebview")
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        print(f"桌面窗口启动失败：{exc}")
        print("请确认已安装 Microsoft Edge WebView2 Runtime（Windows 11 一般自带）。")
        print(f"已改用浏览器访问：{url}")
        webbrowser.open(url)


if __name__ == "__main__":
    main()
