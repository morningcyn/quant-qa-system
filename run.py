# 桌面入口：本地后端线程 + pywebview 窗口。
#   python run.py          → 桌面窗口模式
#   python run.py --dev    → 开发模式：只起后端并用系统浏览器打开（F12 调试）
import base64
import os
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser

import uvicorn

from backend.config import APP_VERSION, DATA_DIR, ROOT_DIR

APP_TITLE = "助理会话质检助手"

if sys.stdout is None or sys.stderr is None:
    # 无控制台环境（PyInstaller --windowed / pythonw 双击启动）：
    # sys.stdout/stderr 为 None，uvicorn 配置日志时调用 .isatty() 会崩溃，
    # 统一重定向到 devnull（不影响 _log 落盘 errors.log）。
    import io

    _null = io.StringIO()
    if sys.stdout is None:
        sys.stdout = _null
    if sys.stderr is None:
        sys.stderr = _null

ERROR_LOG = ROOT_DIR / "errors.log"


def _log(msg: str) -> None:
    """开发模式输出控制台；打包模式（无控制台）落盘到 exe 同级 errors.log。"""
    print(msg)
    if getattr(sys, "frozen", False):
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except Exception:  # noqa: BLE001
            pass


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_backend(port: int) -> threading.Thread:
    def _run():
        try:
            uvicorn.run(
                "backend.main:app",
                host="127.0.0.1",
                port=port,
                log_level="warning",
            )
        except Exception:  # noqa: BLE001 捕获 uvicorn 启动异常，打包模式落日志
            _log("uvicorn 启动失败：\n" + traceback.format_exc())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


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
    backend_thread = start_backend(port)
    if not wait_healthy(url):
        _log("后端启动失败，请检查 Python 依赖是否完整（pip install -r requirements.txt）")
        sys.exit(1)

    if "--dev" in sys.argv:
        _log(f"开发模式：浏览器访问 {url}")
        webbrowser.open(url)
        # 保持进程存活：join 后端线程（uvicorn 常驻；Ctrl+C 或关闭终端即退出）。
        # 不 join 的话 daemon 线程随主线程 return 被强杀，dev 模式后端必然死掉。
        backend_thread.join()
        return

    try:
        import webview

        _log(f"后端就绪：{url}")
        webview.create_window(
            APP_TITLE,
            url,
            width=1280,
            height=820,
            min_size=(1080, 720),
            background_color="#0d0d0d",
            js_api=JsApi(),
        )
        _log("启动 pywebview 窗口（edgechromium）…")
        webview.start(gui="edgechromium")  # 主线程阻塞 GUI 循环；窗口关闭即退出
        _log("窗口已关闭，进程退出")
    except ImportError:
        _log("未安装 pywebview，请执行：pip install pywebview")
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        _log(f"桌面窗口启动失败：{exc}")
        _log(traceback.format_exc())
        _log("已改用浏览器访问：" + url)
        webbrowser.open(url)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 最外层兜底：任何未捕获异常都落日志
        _log("程序启动异常：\n" + traceback.format_exc())
        sys.exit(1)
