#!/usr/bin/env python3
"""Linux 桌面字幕后端（X11）的真 X 冒烟测试。

跑法（CI 同款；会起真窗口，别对着正在用的桌面直接跑）：
    xvfb-run -a .venv/bin/python tests/test_desktop_overlay_linux.py

只测 Xlib 层事实（找窗 / 几何 / 形状 / WM_HINTS），不依赖窗口管理器；
无 DISPLAY 或没有 libX11 时**明确跳过**（返回 0 + 一行说明，不是静默）。
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TITLE = "VLT Linux Backend 测试窗"
NEEDLE = "vlt linux backend"
GEOM = (50, 60, 200, 100)          # x, y, w, h


def main() -> int:
    if not os.environ.get("DISPLAY"):
        print("  ⚠️ 没有 DISPLAY → 跳过（本用例要真 X 服务；CI 由 xvfb-run 提供）")
        return 0

    from vlt import platform as P
    from vlt.platform import linux as L

    loaded = L._load_x11()
    if loaded is None:
        print("  ⚠️ libX11/libXext 或 X 连接不可用 → 跳过")
        return 0
    x11, xext, dpy = loaded

    import tkinter as tk

    win = tk.Tk(className="vlt-backend-test")
    win.title(TITLE)
    win.geometry(f"{GEOM[2]}x{GEOM[3]}+{GEOM[0]}+{GEOM[1]}")
    win.update()

    inner = int(win.winfo_id())
    outer = int(P.top_level_hwnd(inner))
    root = int(x11.XDefaultRootWindow(dpy))

    # ---- 原始验证工具（尽量不借被测代码的路径）----
    xext.XShapeGetRectangles.restype = ctypes.c_void_p
    xext.XShapeGetRectangles.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]

    def raw_parent(wid: int) -> int:
        r, p = ctypes.c_ulong(), ctypes.c_ulong()
        ch = ctypes.POINTER(ctypes.c_ulong)()
        n = ctypes.c_uint()
        x11.XQueryTree(dpy, ctypes.c_ulong(wid), ctypes.byref(r), ctypes.byref(p),
                       ctypes.byref(ch), ctypes.byref(n))
        if ch:
            x11.XFree(ch)
        return int(p.value)

    def raw_input_shape_count(wid: int) -> int:
        n = ctypes.c_int()
        order = ctypes.c_int()
        ptr = xext.XShapeGetRectangles(dpy, ctypes.c_ulong(wid), 2,
                                       ctypes.byref(n), ctypes.byref(order))
        if ptr:
            x11.XFree(ptr)
        return int(n.value)

    def raw_wm_hints(wid: int) -> tuple[int, int]:
        atom = int(L._atom(x11, dpy, b"WM_HINTS"))
        a_type, a_fmt = ctypes.c_ulong(), ctypes.c_int()
        n_items, after = ctypes.c_ulong(), ctypes.c_ulong()
        prop = ctypes.POINTER(ctypes.c_ubyte)()
        status = x11.XGetWindowProperty(
            dpy, ctypes.c_ulong(wid), ctypes.c_ulong(atom), 0, 9, 0, ctypes.c_ulong(0),
            ctypes.byref(a_type), ctypes.byref(a_fmt), ctypes.byref(n_items),
            ctypes.byref(after), ctypes.byref(prop))
        if status != 0 or not prop:
            return (-1, -1)
        try:
            vals = ctypes.cast(prop, ctypes.POINTER(ctypes.c_ulong))
            return (int(vals[0]), int(vals[1]))
        finally:
            x11.XFree(prop)

    cases: list[tuple[str, object]] = []

    def case(name: str):
        def deco(fn):
            cases.append((name, fn))
            return fn
        return deco

    @case("top_level_hwnd：换成 root 的直接子窗口（且幂等）")
    def _( ) -> None:
        assert outer != 0, "top_level_hwnd 返回 0"
        assert raw_parent(outer) == root, f"顶层窗的父不是 root（parent={raw_parent(outer):#x}）"
        assert int(P.top_level_hwnd(outer)) == outer, "对顶层窗应幂等"
        if outer == inner:
            print("      （提示：本环境 Tk 没有包装层，winfo_id 即顶层窗）")

    @case("门面探针：desktop_window_backend() 能拿到 Linux 后端")
    def _( ) -> None:
        assert P.desktop_window_backend() is not None, "探针没认出 Linux 后端"

    @case("find_window_by_title：子串命中（大小写不敏感）、排除清单生效")
    def _( ) -> None:
        found = P.find_game_window(NEEDLE, ())
        assert found is not None, "没找到自己的测试窗"
        assert P.find_game_window(NEEDLE.upper(), ()) is not None, "大写子串应同样命中"
        assert P.find_game_window(NEEDLE, (outer,)) is None, "排除清单没生效"

    @case("window_client_rect：尺寸正确、原点与 Tk 自报一致")
    def _( ) -> None:
        rect = P.window_client_rect(outer)
        assert rect is not None, "取不到客户区"
        assert (rect[2] - rect[0], rect[3] - rect[1]) == (GEOM[2], GEOM[3]), \
            f"尺寸不对：{rect[2] - rect[0]}x{rect[3] - rect[1]}"
        assert (rect[0], rect[1]) == (win.winfo_rootx(), win.winfo_rooty()), (
            f"原点不一致：rect={rect[:2]} tk=({win.winfo_rootx()},{win.winfo_rooty()})")

    @case("set_click_through：开→输入区为空；关→恢复默认")
    def _( ) -> None:
        assert P.set_click_through(outer, True) is True
        x11.XSync(dpy, 0)
        assert raw_input_shape_count(outer) == 0, "开了穿透但输入区不为空"
        assert P.set_click_through(outer, False) is True
        x11.XSync(dpy, 0)
        assert raw_input_shape_count(outer) >= 1, "关了穿透但输入区没恢复"

    @case("set_tool_window：WM_HINTS 的 input=False 真写上")
    def _( ) -> None:
        assert P.set_tool_window(outer) is True
        x11.XSync(dpy, 0)
        flags, input_hint = raw_wm_hints(outer)
        assert flags & 0x1, f"InputHint 未置位（flags={flags:#x}）"
        assert input_hint == 0, f"input={input_hint}，应为 0"

    @case("screen_work_area：是块合法矩形")
    def _( ) -> None:
        left, top, right, bottom = P.screen_work_area()
        assert right > left and bottom > top, f"工作区非法：{(left, top, right, bottom)}"

    @case("is_window：窗口在 → True；无效句柄 → False")
    def _( ) -> None:
        assert P.is_window(outer) is True
        assert P.is_window(0) is False

    bad: list[str] = []
    for name, fn in cases:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as exc:  # noqa: BLE001 — 单条失败不挡其余用例
            bad.append(name)
            print(f"  ✗ {name}\n      {type(exc).__name__}: {exc}")

    # 收尾：销毁窗口后句柄必须失效（放最后跑，前面的用例还要用窗口）
    try:
        win.destroy()
        deadline = time.time() + 2.0
        while time.time() < deadline and P.is_window(outer):
            time.sleep(0.02)
        assert not P.is_window(outer), "销毁后 is_window 仍为 True"
        print("  ✓ is_window：销毁后为 False")
    except Exception as exc:  # noqa: BLE001
        bad.append("is_window-after-destroy")
        print(f"  ✗ is_window 销毁后检查\n      {type(exc).__name__}: {exc}")

    print("ALL PASSED" if not bad else f"FAILED: {', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
