"""一次性下载前端离线依赖到 frontend/static/vendor/（固定版本 + 体积校验）。

运行：python scripts/download_vendor.py
之后应用运行时零 CDN 依赖，断网可用。
"""
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "frontend" / "static" / "vendor"

# (目标文件名, 下载地址, 预期最小体积字节, 预期最大体积字节)
FILES = [
    ("echarts.min.js", "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js", 900_000, 1_200_000),
    ("html2canvas.min.js", "https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js", 150_000, 400_000),
    ("jspdf.umd.min.js", "https://cdn.jsdelivr.net/npm/jspdf@2.5.2/dist/jspdf.umd.min.js", 250_000, 700_000),
]


def download(url: str, dest: Path) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "quant-qa-vendor-downloader/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def main() -> int:
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    failed = False
    for name, url, lo, hi in FILES:
        dest = VENDOR_DIR / name
        try:
            data = download(url, dest)
            size = len(data)
            ok = lo <= size <= hi
            if ok:
                dest.write_bytes(data)
                sha = hashlib.sha256(data).hexdigest()[:12]
                print(f"[OK]   {name}  {size:,} bytes  sha256={sha}")
            else:
                print(f"[FAIL] {name}  体积异常 {size:,} bytes（预期 {lo:,}~{hi:,}）")
                failed = True
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {name}  下载失败: {exc}")
            failed = True
    if failed:
        print("\n部分文件未就绪，请检查网络后重试（可访问 jsdelivr CDN）。")
        return 1
    print(f"\n全部 vendor 文件就绪：{VENDOR_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
