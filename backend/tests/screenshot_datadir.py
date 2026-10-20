# -*- coding: utf-8 -*-
"""截资源管理器窗口（指定 hwnd，PrintWindow 不被遮挡影响）。

策略：
1. 启动 explorer.exe 指向 backend/data（等 6s 渲染）
2. EnumWindows 找标题含 "data" 且类是 "CabinetWClass" 的窗口
3. SetForegroundWindow / ShowWindow 把它显示在最前
4. PrintWindow 截它（只截窗口，不截全屏 → 不受其他窗口影响）
"""
import sys
import time
import ctypes
import ctypes.wintypes as wt
import subprocess
import pathlib
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
OUT_DIR = ROOT / "docs" / "marketing" / "screenshots"
OUT_DIR.mkdir(parents=True, exist_ok=True)

DATA_DIR = ROOT / "backend" / "data"
DATA_DIR_WIN = str(DATA_DIR).replace("/", "\\")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
user32.PrintWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, wt.INT]
user32.ShowWindow.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL


def find_explorer_window(title_substr: str) -> tuple[int, str, str]:
    """找 explorer 窗口（类 CabinetWClass）。"""
    result = []

    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        class_buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, class_buf, 64)
        if "CabinetWClass" not in class_buf.value:
            return True
        title_buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title_buf, 256)
        title = title_buf.value
        if title_substr.lower() in title.lower():
            result.append((hwnd, title, class_buf.value))
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return result[0] if result else (None, "", "")


def print_window_to_png(hwnd: int, out_path: pathlib.Path) -> tuple[int, int]:
    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    print(f"[rect] {width}x{height}  ({rect.left},{rect.top})")

    hdc_window = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
    hbm = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
    gdi32.SelectObject(hdc_mem, hbm)

    # PrintWindow flags
    PW_CLIENTONLY = 0x00000001
    PW_RENDERFULLCONTENT = 0x00000002

    # 先尝试 PW_RENDERFULLCONTENT（Win 8.1+），再 PW_CLIENTONLY
    ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
    if not ok:
        print("[warn] PW_RENDERFULLCONTENT failed, retry PW_CLIENTONLY")
        ok = user32.PrintWindow(hwnd, hdc_mem, PW_CLIENTONLY)
    print(f"[printwindow] ok={ok}")

    # GetDIBits → buffer
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB

    buf = (ctypes.c_ubyte * (width * height * 4))()
    DIB_RGB_COLORS = 0
    got = gdi32.GetDIBits(hdc_mem, hbm, 0, height, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
    print(f"[getdibits] {got} lines, first byte={buf[0]:02x}")

    img = Image.frombuffer("RGBA", (width, height), bytes(buf), "raw", "BGRA", 0, 1)
    img.save(str(out_path), "PNG")

    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_window)
    return width, height


def main() -> int:
    print(f"[explorer] launching: {DATA_DIR_WIN}")
    subprocess.Popen(["explorer.exe", DATA_DIR_WIN])
    time.sleep(7)

    # 找 explorer 窗口
    hwnd, title, cls = find_explorer_window("data")
    if not hwnd:
        # 兜底：找所有 CabinetWClass
        result = []

        def cb(hwnd2, lparam):
            if not user32.IsWindowVisible(hwnd2):
                return True
            class_buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd2, class_buf, 64)
            if "CabinetWClass" in class_buf.value:
                title_buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd2, title_buf, 256)
                result.append((hwnd2, title_buf.value, class_buf.value))
            return True

        user32.EnumWindows(WNDENUMPROC(cb), 0)
        if not result:
            print("[FAIL] no CabinetWClass window")
            return 1
        print(f"[fallback] {len(result)} CabinetWClass found:")
        for h, t, c in result:
            print(f"  hwnd={h}  title='{t}'")
        hwnd, title, cls = result[0]

    print(f"[found] hwnd={hwnd}  title='{title}'  class='{cls}'")

    # 显示窗口（最大化 + 置顶）
    SW_SHOWMAXIMIZED = 3
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.5)
    user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)
    time.sleep(1)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)

    out = OUT_DIR / "06-datadir-v1.0.4-raw.png"
    width, height = print_window_to_png(hwnd, out)
    print(f"[OK] {out}  {width}x{height}  size={out.stat().st_size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
