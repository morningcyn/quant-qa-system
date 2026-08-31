# Windows DPAPI 加解密（ctypes 直调 crypt32.dll，零第三方依赖）。
# 密文格式 "dpapi:<base64>"，绑定当前 Windows 用户+机器；换机/重装后解密失败返回 None。
import base64
import ctypes
import ctypes.wintypes as wt
import sys

PREFIX = "dpapi:"
# DPAPI 不可用时仍沿用 PREFIX 作为外层格式，保证已有配置格式兼容；
# 在回退内容中加入标记，使解密时能够区分“回退 base64”与真正的 DPAPI 密文。
_FALLBACK_MARKER = b"quant-qa-b64-fallback-v1:\x00"

if sys.platform == "win32":
    crypt32 = ctypes.WinDLL("crypt32")  # noqa: PGH003
else:
    crypt32 = None


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wt.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob_from_bytes(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob


def _blob_to_bytes(blob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free_blob(blob):
    kernel32 = ctypes.WinDLL("kernel32")  # noqa: PGH003
    kernel32.LocalFree(blob.pbData)


def encrypt_secret(plain: str) -> str:
    """加密 API Key 等敏感配置，返回 ``dpapi:<base64>``。

    非 Windows 平台，或 Windows DPAPI 调用失败时，退化为带标记的 base64
    存储。外层仍使用 ``dpapi:`` 是为了兼容已有数据库格式；标记用于解密
    时避免再次把回退内容当作 DPAPI 密文处理。
    """
    if not plain:
        return ""
    if crypt32 is None:
        return PREFIX + base64.b64encode(_FALLBACK_MARKER + plain.encode("utf-8")).decode()
    data = plain.encode("utf-16-le")  # DPAPI 内部按 Unicode 处理
    blob_in = _blob_from_bytes(data)
    blob_out = _DATA_BLOB()
    if crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out)
    ):
        try:
            return PREFIX + base64.b64encode(_blob_to_bytes(blob_out)).decode()
        finally:
            _free_blob(blob_out)
    return PREFIX + base64.b64encode(_FALLBACK_MARKER + plain.encode("utf-8")).decode()


def decrypt_secret(stored: str):
    """解密，失败（换机/损坏/非 dpapi 格式）返回 None。"""
    if not stored:
        return ""
    if not stored.startswith(PREFIX):
        return None
    b64 = stored[len(PREFIX):]
    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return None
    if crypt32 is None:
        try:
            if not raw.startswith(_FALLBACK_MARKER):
                return None
            return raw[len(_FALLBACK_MARKER):].decode("utf-8")
        except Exception:  # noqa: BLE001
            return None
    blob_in = _blob_from_bytes(raw)
    blob_out = _DATA_BLOB()
    if crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out)
    ):
        try:
            dec = _blob_to_bytes(blob_out)
            return dec.decode("utf-16-le", errors="ignore").rstrip("\x00")
        finally:
            _free_blob(blob_out)
    # 兼容 DPAPI 调用失败时旧版本写入的无标记 base64 回退密文；新写入的
    # 回退密文必须带标记。损坏的 base64 或无法按 UTF-8 解码的内容仍返回 None。
    try:
        if raw.startswith(_FALLBACK_MARKER):
            return raw[len(_FALLBACK_MARKER):].decode("utf-8")
        return raw.decode("utf-8")
    except Exception:  # noqa: BLE001
        return None
