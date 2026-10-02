#!/usr/bin/env python3
"""X11 原生桌面叠加窗的真协议测试（自起私有 Xvfb / picom，绝不碰用户会话）。

跑法（CI 同款；沙盒/CI 直接跑，**不用**加 xvfb-run —— 本用例自己管理 X 服务）：
    .venv/bin/python tests/test_x11_window.py

层次（与 tests/test_wayland_window.py 同构）：

1. 纯逻辑：`drag_target` 的绝对坐标算式（含副屏负坐标）；
2. 签名断言：`ArgbWindow` 把 `DesktopWindow` 契约的方法/属性配齐；
3. **无合成器**真窗：32 位 visual 建窗（ORD）/ `XPutImage` 像素回读 /
   X Shape 输入区穿透与解锁 / 拖动（xdotool 真指针）/ 改尺寸 / 整体透明度；
4. **有合成器**（picom）：截图 root，验证透明区真的透出底色、半透明真的混合
   —— 这是「逐像素透明」的最终判据（3 只能证明画对了字节）。

没有 Xvfb / picom / import / xdotool（或没有 libX11）时**明确跳过**并说明原因。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BG = (0x33, 0x66, 0x99)                    # 合成用例的 root 底色


# ---------------------------------------------------------------- 纯逻辑 / 签名


def test_pure_logic() -> None:
    """drag_target：窗口位置 = 抓取时位置 + 指针绝对位移。"""
    from vlt.platform.x11 import drag_target
    assert drag_target((100, 50), (300, 400), (300, 400)) == (100, 50), "零位移"
    assert drag_target((100, 50), (300, 400), (450, 350)) == (250, 0), "基本位移"
    assert drag_target((-1920, 0), (-1900, 100), (-2200, 130)) == (-2220, 30), "副屏负坐标"
    print("  纯逻辑 drag_target（绝对坐标 / 负坐标）OK")


def test_interface_signatures() -> None:
    """ArgbWindow 的方法/属性与 base.DesktopWindow 契约对齐（与 Wayland 后端同形）。"""
    from vlt.platform.x11 import ArgbWindow
    for name in ("set_panel", "move", "set_size", "set_alpha", "set_click_through",
                 "set_draggable", "tick", "close"):
        assert callable(getattr(ArgbWindow, name, None)), f"缺方法 {name}"
    assert isinstance(getattr(ArgbWindow, "position", None), property), "position 应为 property"
    assert getattr(ArgbWindow, "available", None) is False, "available 初始应为 False"
    print("  窗口契约签名（方法 / property / available）OK")


# ---------------------------------------------------------------- Xvfb / picom 基础设施


def _have(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None


class _Xvfb:
    """自拉一个私有 Xvfb（从 :90 起挑空闲号）；-ac 关鉴权，与用户会话完全隔离。"""

    def __init__(self) -> None:
        self.name = ""
        self.proc = None
        self._log = None
        self.tmp = Path(tempfile.mkdtemp(prefix="vlt-x11-test-"))
        for cand in range(90, 100):
            p = subprocess.Popen(["Xvfb", f":{cand}", "-screen", "0", "1280x720x24",
                                  "-ac", "-nolisten", "tcp"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.4)
            if p.poll() is not None:
                continue
            dpy = _probe_open(f":{cand}")
            if dpy:
                self.name, self.proc = f":{cand}", p
                return
            p.kill()
        self.proc = None

    @property
    def ok(self) -> bool:
        return bool(self.name)

    # ---- picom ----

    def start_compositor(self) -> bool:
        """起 picom 并等它拿到 _NET_WM_CM_S0 选主；起不来返回 False（用例明说跳过）。"""
        if not self.ok or not _have("picom"):
            return False
        env = dict(os.environ, DISPLAY=self.name)
        self._log = open(self.tmp / "picom.log", "wb")
        self.picom = subprocess.Popen(
            ["picom", "--config", "/dev/null", "--backend", "xrender"],
            env=env, stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self.picom.poll() is not None:
                return False
            if _compositor_owner() != 0:
                return True
            time.sleep(0.1)
        return False

    def stop(self) -> None:
        for attr in ("picom", "proc"):
            p = getattr(self, attr, None)
            if p is None:
                continue
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        if self._log is not None:
            try:
                self._log.close()
            except Exception:  # noqa: BLE001
                pass


def _probe_open(display: str) -> bool:
    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XCloseDisplay.restype = ctypes.c_int
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    dpy = x11.XOpenDisplay(display.encode())
    if dpy:
        x11.XCloseDisplay(dpy)
        return True
    return False


def _compositor_owner() -> int:
    """当前 DISPLAY 上 _NET_WM_CM_S0 的选主（0 = 没有合成器）。"""
    from vlt.platform import linux as L
    loaded = L._load_x11()
    if not loaded:
        return 0
    x11, _xext, dpy = loaded
    if not hasattr(x11, "_vlt_selection_declared"):
        x11.XGetSelectionOwner.restype = ctypes.c_ulong
        x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11._vlt_selection_declared = True       # type: ignore[attr-defined]
    atom = int(L._atom(x11, dpy, b"_NET_WM_CM_S0"))
    return int(x11.XGetSelectionOwner(dpy, ctypes.c_ulong(atom)))


# ---------------------------------------------------------------- 原始核对工具（不借被测路径）


def _raw() -> tuple:
    """linux.py 的共享连接（当前 DISPLAY）+ 在它上面补声明本用例要用的原始请求。"""
    from vlt.platform import linux as L
    loaded = L._load_x11()
    assert loaded, "libX11 不可用"
    x11, xext, dpy = loaded
    if not hasattr(x11, "_vlt_test_declared"):
        class _XImage(ctypes.Structure):
            _fields_ = [
                ("width", ctypes.c_int), ("height", ctypes.c_int),
                ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
                ("data", ctypes.c_void_p),
                ("byte_order", ctypes.c_int), ("bitmap_unit", ctypes.c_int),
                ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int),
                ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int),
                ("bits_per_pixel", ctypes.c_int),
            ]
        x11._XImageTest = _XImage                 # type: ignore[attr-defined]
        x11.XGetImage.restype = ctypes.POINTER(_XImage)
        x11.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
                                  ctypes.c_ulong, ctypes.c_int]
        x11.XDestroyImage.restype = ctypes.c_int
        x11.XDestroyImage.argtypes = [ctypes.POINTER(_XImage)]
        x11.XTranslateCoordinates.restype = ctypes.c_int
        x11.XTranslateCoordinates.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int,
            ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong)]
        x11.XSetWindowBackground.restype = ctypes.c_int
        x11.XSetWindowBackground.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        x11.XClearWindow.restype = ctypes.c_int
        x11.XClearWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]

        class _XSetWindowAttributes(ctypes.Structure):
            _fields_ = [
                ("background_pixmap", ctypes.c_ulong), ("background_pixel", ctypes.c_ulong),
                ("border_pixmap", ctypes.c_ulong), ("border_pixel", ctypes.c_ulong),
                ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int),
                ("backing_store", ctypes.c_int),
                ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong),
                ("save_under", ctypes.c_int),
                ("event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
                ("override_redirect", ctypes.c_int),
                ("colormap", ctypes.c_ulong), ("cursor", ctypes.c_ulong),
            ]
        x11._XSetWindowAttributesTest = _XSetWindowAttributes   # type: ignore[attr-defined]
        x11.XCreateWindow.restype = ctypes.c_ulong
        x11.XCreateWindow.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.POINTER(_XSetWindowAttributes)]
        x11.XMapWindow.restype = ctypes.c_int
        x11.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XDestroyWindow.restype = ctypes.c_int
        x11.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        xext.XShapeGetRectangles.restype = ctypes.c_void_p
        xext.XShapeGetRectangles.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        x11._vlt_test_declared = True             # type: ignore[attr-defined]
    return x11, xext, dpy


def _attrs(wid: int):
    from vlt.platform import linux as L
    x11, _xext, dpy = _raw()
    return L._attrs(x11, dpy, wid)


def _input_region_count(wid: int) -> int:
    """X Shape 输入区矩形数（0 = 完全穿透；≥1 = 恢复默认/有区域）。"""
    x11, xext, dpy = _raw()
    n, order = ctypes.c_int(), ctypes.c_int()
    ptr = xext.XShapeGetRectangles(dpy, ctypes.c_ulong(wid), 2,
                                   ctypes.byref(n), ctypes.byref(order))
    if ptr:
        x11.XFree(ptr)
    return int(n.value)


def _bounding_area(wid: int) -> int:
    """形状蒙版（Bounding=0）的矩形覆盖面积；没设过 = 默认区域 = 整窗面积。"""
    x11, xext, dpy = _raw()
    n, order = ctypes.c_int(), ctypes.c_int()
    ptr = xext.XShapeGetRectangles(dpy, ctypes.c_ulong(wid), 0,
                                   ctypes.byref(n), ctypes.byref(order))
    if not ptr:
        return 0

    class _Rect(ctypes.Structure):
        _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short),
                    ("width", ctypes.c_ushort), ("height", ctypes.c_ushort)]

    try:
        rects = ctypes.cast(ptr, ctypes.POINTER(_Rect))
        return sum(int(rects[i].width) * int(rects[i].height) for i in range(n.value))
    finally:
        x11.XFree(ptr)


def _read_window(wid: int, w: int, h: int) -> bytes:
    """XGetImage 回读窗口内容（无合成器时 = XPutImage 写进去的原始字节）。"""
    x11, _xext, dpy = _raw()
    img = x11.XGetImage(dpy, ctypes.c_ulong(wid), 0, 0, w, h,
                        ctypes.c_ulong(-1), 2)
    assert img, "XGetImage 失败"
    meta = img.contents
    raw = ctypes.string_at(meta.data, meta.bytes_per_line * h)
    stride = meta.bytes_per_line
    x11.XDestroyImage(img)                        # XGetImage 的 data 由 Xlib 分配，正常销毁
    return raw, stride


def _px(raw: bytes, stride: int, x: int, y: int) -> tuple[int, int, int, int]:
    o = y * stride + x * 4
    return tuple(raw[o:o + 4])                    # type: ignore[return-value]


def _window_pos(wid: int) -> tuple[int, int]:
    """窗口左上角的 root 绝对坐标（独立于被测代码的 position 属性）。"""
    x11, _xext, dpy = _raw()
    dx, dy, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
    root = int(x11.XDefaultRootWindow(dpy))
    assert x11.XTranslateCoordinates(dpy, ctypes.c_ulong(wid), ctypes.c_ulong(root),
                                     0, 0, ctypes.byref(dx), ctypes.byref(dy),
                                     ctypes.byref(child))
    return int(dx.value), int(dy.value)


def _xdotool(*args: object) -> None:
    subprocess.run(["xdotool", *(str(a) for a in args)], check=True,
                   capture_output=True, timeout=15)


def _pump(win, seconds: float) -> None:  # noqa: ANN001
    end = time.time() + seconds
    while time.time() < end:
        win.tick()
        time.sleep(0.01)


def _three_band_panel(size: tuple[int, int]):
    """三竖带：左=不透明红 / 中=半透明黑 / 右=全透明（一张图验三种合成）。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w, h = size
    d.rectangle([0, 0, w // 3 - 1, h - 1], fill=(255, 0, 0, 255))
    d.rectangle([w // 3, 0, 2 * w // 3 - 1, h - 1], fill=(0, 0, 0, 128))
    return img


# ---------------------------------------------------------------- 3. 无合成器真窗


def test_live_window() -> None:
    from vlt.platform.x11 import ArgbWindow

    drag_log: list[tuple[int, int]] = []
    win = ArgbWindow(size=(220, 120), alpha=1.0, click_through=True,
                     on_drag_end=lambda x, y: drag_log.append((int(x), int(y))))
    try:
        assert win.available, "原生 X11 窗建不起来"
        assert win.position == (0, 0)

        win.move(120, 90)
        _pump(win, 0.1)
        assert win.position == (120, 90)
        a = _attrs(win._win)
        assert a is not None, "窗口不存在"
        assert a.depth == 32, f"不是 32 位 visual（depth={a.depth}）"
        assert a.override_redirect == 1, "不是覆盖窗（ORD）"
        assert a.map_state == 2, "窗口没映射出来"
        assert (a.width, a.height) == (220, 120), (a.width, a.height)
        assert _window_pos(win._win) == (120, 90), "XMoveWindow 没落到 root"

        # 出图 → 像素回读（预乘 BGRA 原样）
        win.set_panel(_three_band_panel((220, 120)))
        _pump(win, 0.15)
        raw, stride = _read_window(win._win, 220, 120)
        assert _px(raw, stride, 30, 30) == (0, 0, 255, 255), _px(raw, stride, 30, 30)
        mid = _px(raw, stride, 110, 30)
        assert mid == (0, 0, 0, 128), f"半透明黑应为预乘 (0,0,0,128)：{mid}"
        assert _px(raw, stride, 200, 30) == (0, 0, 0, 0), "透明区不是全零"
        # 无合成器降级：1 位形状蒙版把透明区裁掉（这就是「黑框」的修法）——
        # 面积 = 实心像素数 = 2/3 宽（左 73 + 中 73 列）× 全高
        expect = (2 * 220 // 3) * 120
        assert _bounding_area(win._win) == expect, \
            f"无合成器没裁形：{_bounding_area(win._win)}（应 {expect}）"
        print("  建窗（32 位 / ORD / 映射）+ 像素回读（不透明 / 半透明 / 全透明）"
              f"+ 无合成器裁形（面积 {expect}）OK")

        # 穿透：输入区为空 → 真点击收不到；解锁拖动 → 恢复整窗 → 收得到
        assert _input_region_count(win._win) == 0, "click_through 初始没置空输入区"
        _xdotool("mousemove", 200, 140)
        _xdotool("click", "1")
        _pump(win, 0.3)
        assert win._buttons_seen == 0, f"穿透没生效（收到 {win._buttons_seen} 次点击）"
        win.set_draggable(True)
        _pump(win, 0.1)
        assert _input_region_count(win._win) >= 1, "解锁拖动后输入区没恢复"
        _xdotool("click", "1")
        _pump(win, 0.3)
        assert win._buttons_seen >= 1, "解锁拖动后收不到点击"
        print("  穿透（输入区置空，点击落空）/ 解锁拖动（恢复输入区，收到点击）OK")

        # 拖动：真指针（xdotool/XTEST）按下 → 移动 → 松开
        drag_log.clear()           # 上面那次「点击」也会触发一次零位移回调，这里清掉
        _xdotool("mousemove", 180, 130)
        _xdotool("mousedown", "1")
        _pump(win, 0.2)
        assert win._drag_active, "按住后没进入拖动状态"
        _xdotool("mousemove", 330, 250)
        _pump(win, 0.3)
        _xdotool("mouseup", "1")
        _pump(win, 0.3)
        assert not win._drag_active, "松开后还在拖动状态"
        assert win.position == (270, 210), f"拖动落点不对：{win.position}"
        assert _window_pos(win._win) == (270, 210), "窗口实际位置与 position 不一致"
        assert drag_log == [(270, 210)], f"拖动回调不对：{drag_log}"
        print("  拖动（xdotool 真指针，落点 270,210 对 root 核对）OK")

        # 改尺寸 + 整体透明度（重建缓冲后仍然画对；蒙版跟着新尺寸重设）
        win.set_size((260, 140))
        win.set_panel(_three_band_panel((260, 140)))
        _pump(win, 0.15)
        a = _attrs(win._win)
        assert (a.width, a.height) == (260, 140), (a.width, a.height)
        raw, stride = _read_window(win._win, 260, 140)
        assert _px(raw, stride, 30, 30) == (0, 0, 255, 255), "改尺寸后出图不对"
        expect = (2 * 260 // 3) * 140
        assert _bounding_area(win._win) == expect, "改尺寸后蒙版没重设"
        win.set_alpha(0.5)
        _pump(win, 0.15)
        raw, stride = _read_window(win._win, 260, 140)
        got = _px(raw, stride, 30, 30)
        assert all(abs(g - w) <= 2 for g, w in zip(got, (0, 0, 127, 127))), \
            f"整层 alpha=0.5 不对：{got}"
        assert _bounding_area(win._win) == expect, "蒙版不该随整层透明度变化"
        win.set_alpha(1.0)
        _pump(win, 0.1)
        print("  改尺寸（缓冲/蒙版重建）+ 整层透明度（0.5 → 预乘减半、蒙版不变）OK")
    finally:
        win.close()

    # 收尾：窗口真的销毁 + close 幂等
    assert _attrs(win._win if win._win else 0x1) is None, "close 后窗口还在"
    win.close()
    print("  关闭（销毁 + 幂等）OK")


# ---------------------------------------------------------------- 4. 有合成器（picom）


def _make_backdrop(x: int, y: int, w: int, h: int, rgb: int) -> int:
    """垫在叠加窗下面的不透明背底窗。

    ⚠️ 不能用「给 root 涂色」：picom 合成时屏幕内容走 COW，桌面区是合成器默认的
    **黑色**——黑色底上根本分不出「透明」「半透明黑」「不透明黑」。所以先放一块
    固定色的普通窗当背底，再在它上面盖叠加窗。
    """
    x11, _xext, dpy = _raw()
    root = int(x11.XDefaultRootWindow(dpy))
    attrs = x11._XSetWindowAttributesTest()      # type: ignore[attr-defined]
    attrs.background_pixel = rgb
    attrs.override_redirect = 1
    attrs.event_mask = 0
    mask = (1 << 1) | (1 << 9)                   # CWBackPixel | CWOverrideRedirect
    win = x11.XCreateWindow(dpy, ctypes.c_ulong(root), x, y, w, h, 0, 0, 1, None,
                            mask, ctypes.byref(attrs))
    assert win, "背底窗建不出来"
    x11.XMapWindow(dpy, ctypes.c_ulong(win))
    x11.XSync(dpy, 0)
    return int(win)


def test_live_compositor() -> None:
    from vlt.platform.x11 import ArgbWindow
    from PIL import Image

    x11, _xext, dpy = _raw()
    base = _make_backdrop(250, 150, 420, 240, 0x336699)
    win = ArgbWindow(size=(240, 120), alpha=1.0, click_through=True)
    try:
        assert win.available
        win.move(300, 200)
        win.set_panel(_three_band_panel((240, 120)))
        _pump(win, 0.3)

        # 等 picom 把这一帧重合成出来（轮询截图，最多 ~4s）
        shot_path = Path(tempfile.mkdtemp(prefix="vlt-x11-shot-")) / "root.png"
        shot: Image.Image | None = None
        deadline = time.time() + 4.0
        while time.time() < deadline:
            png = subprocess.run(["import", "-window", "root", str(shot_path)],
                                 capture_output=True, timeout=15)
            assert png.returncode == 0, f"import 失败：{png.stderr[:200]!r}"
            shot = Image.open(shot_path).convert("RGB")
            # 左带成红（合成帧就绪）才算数
            if shot.getpixel((330, 260))[0] > 200:
                break
            time.sleep(0.15)
        assert shot is not None

        def near(got: tuple, want: tuple, tol: int = 6) -> bool:
            return all(abs(int(a) - int(b)) <= tol for a, b in zip(got, want))

        got_left = shot.getpixel((330, 260))        # 窗内、左带（不透明红）
        got_mid = shot.getpixel((420, 260))         # 窗内、中带（50% 黑 → 底色减半）
        got_right = shot.getpixel((510, 260))       # 窗内、右带（全透明 → 底色）
        got_outer = shot.getpixel((280, 160))       # 背底上、叠加窗外（对照组）
        assert near(got_left, (255, 0, 0)), f"不透明红不对：{got_left}"
        assert near(got_mid, (BG[0] * 0.5, BG[1] * 0.5, BG[2] * 0.5)), \
            f"半透明黑合成不对：{got_mid}（应约 {tuple(int(v * 0.5) for v in BG)}）"
        assert near(got_right, BG), f"透明区没透出底色：{got_right}（应 {BG}）"
        assert near(got_outer, BG), f"背底对照不对：{got_outer}"
        # 有合成器：**不该**裁形（保持圆润的逐像素 alpha；裁了圆角会变锯齿）
        assert _bounding_area(win._win) == 240 * 120, \
            f"有合成器却裁了形：{_bounding_area(win._win)}（应整窗 {240 * 120}）"
        print(f"  picom 合成：不透明红 {got_left} / 半透明 {got_mid} / "
              f"全透明透出底色 {got_right}（窗外 {got_outer}）+ 不裁形 OK")
    finally:
        win.close()
        x11.XDestroyWindow(dpy, ctypes.c_ulong(base))
        x11.XSync(dpy, 0)


# ---------------------------------------------------------------- main


def _run(fn) -> bool:  # noqa: ANN001
    try:
        fn()
        return True
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False


def main() -> int:
    print("test_x11_window:")
    failed = 0
    for fn in (test_pure_logic, test_interface_signatures):
        if not _run(fn):
            failed += 1

    missing = [c for c in ("Xvfb", "xdotool", "import") if not _have(c)]
    if missing:
        print(f"  ⚠️ 缺工具 {missing} → 跳过真 X 用例（无 Xvfb / xdotool / import）")
        print("ALL PASSED" if not failed else f"FAILED: {failed}")
        return 1 if failed else 0

    xvfb = _Xvfb()
    if not xvfb.ok:
        print("  ⚠️ Xvfb 起不来 → 跳过真 X 用例")
        print("ALL PASSED" if not failed else f"FAILED: {failed}")
        return 1 if failed else 0

    saved_display = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = xvfb.name
    try:
        if not _run(test_live_window):
            failed += 1
        if xvfb.start_compositor():
            if not _run(test_live_compositor):
                failed += 1
        else:
            print("  ⚠️ picom 起不来 → 跳过合成断言（透明位本身已在无合成器用例里验过）")
    finally:
        xvfb.stop()
        if saved_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = saved_display

    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
