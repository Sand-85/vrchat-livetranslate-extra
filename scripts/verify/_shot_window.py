"""按窗口句柄用 PrintWindow 截图（真机验收脚本共用的小工具，Windows 专用）。

**作用**：让**窗口自己把内容画进内存 DC**，不经过屏幕合成 —— 所以在远程/已断开的
会话里也能截到窗口（此时 PIL 的 `ImageGrab` 会直接 `OSError: screen grab failed`）。

**需要的环境**：仅 Windows（`user32` / `gdi32`）。Pillow 已在 `requirements.txt` 里。
本文件是 `scripts/verify/` 下几个验收脚本的内部依赖，不是给人单独跑的入口。

**会不会写盘**：不会。这里只返回 PIL 图像，落盘由调用方决定（一律落在 `out/` 沙箱）。

为什么放在这里（而不是各脚本各抄一份）：`verify_room_button.py` 与
`verify_room_i18n.py` 都要截 Tk 窗口，抄两份必然漂移。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

if sys.platform != "win32":          # 缺平台就**立刻**炸，别让调用方跑到一半才异常
    raise RuntimeError("_shot_window 仅支持 Windows（依赖 user32 / gdi32）")

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 物理像素坐标，避免截图错位
except Exception:                                     # noqa: BLE001
    pass

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

PW_RENDERFULLCONTENT = 0x00000002
BI_RGB = 0
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def find_windows(substr: str) -> list[tuple[int, str]]:
    """按标题子串找顶层窗口，返回 `[(hwnd, title), ...]`。

    ⚠️ **不要求 `IsWindowVisible`**：断开的会话里窗口可能被判定为不可见。
    """
    found: list[tuple[int, str]] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if substr.lower() in buf.value.lower():
            found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(proc(cb), 0)
    return found


def rect_of(hwnd: int) -> tuple[int, int, int, int]:
    """窗口外框矩形（left, top, right, bottom，屏幕坐标）。"""
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def capture_printwindow(hwnd: int, w: int, h: int):
    """让窗口自绘到内存 DC，取回 PIL 图像（不经过屏幕合成）。"""
    from PIL import Image

    hdc_win = user32.GetWindowDC(hwnd)
    if not hdc_win:
        raise RuntimeError("GetWindowDC 失败")
    memdc = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(memdc, bmp)
    try:
        ok = user32.PrintWindow(hwnd, memdc, PW_RENDERFULLCONTENT)
        if not ok:
            raise RuntimeError("PrintWindow 返回 0")

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # 负数 = top-down，省一次翻转
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
        if got == 0:
            raise RuntimeError("GetDIBits 失败")
        return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).copy()
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(hwnd, hdc_win)


def capture_screen(bbox):
    """抓屏幕一块区域（需要有**真实**的桌面会话；断开时会抛 screen grab failed）。"""
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=bbox, all_screens=True)
