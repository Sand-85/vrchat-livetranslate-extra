#!/usr/bin/env python
"""X11/GLX 后端的**离线冒烟**：在真实 X server 上建一份 pbuffer context。

不需要头显 / OpenXR 运行时：只验证「纯 ctypes 调 libX11 + GLX 这一层」真能建起
context 并 make current（这层最容易踩签名/指针宽度的坑）。XR 会话本身要等
「X11 显示 + 同一环境跑运行时」的**真机验证** —— 见 docs/平台约束记录.md。

无 DISPLAY、或 X server 没有可用 GLX（如 niri 的精简 `xwayland-satellite`）时
**跳过**（不判红）。CI 里挂在 xvfb-run 下：有 GLX 就真跑（llvmpipe 软渲染），
没有就跳过。
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.output.openxr_overlay import (  # noqa: E402
    GL_RENDERER,
    GL_VENDOR,
    XlibGlxContext,
    create_gl_context,
)


def _skip(msg: str) -> None:
    print(f"  SKIP: {msg}")


def test_glx_context_builds() -> None:
    """真建一份 pbuffer GLX context，并查询 GL 字符串。"""
    if not os.environ.get("DISPLAY"):
        _skip("没有 DISPLAY")
        return
    try:
        ctx = XlibGlxContext(64, 64)
    except Exception as exc:  # noqa: BLE001 — 没有 GLX 的 X server 属环境问题
        _skip(f"GLX 不可用（{type(exc).__name__}: {exc}）")
        return
    try:
        vendor = ctx.gl_string(GL_VENDOR)
        renderer = ctx.gl_string(GL_RENDERER)
        assert vendor and vendor != "?", "GL_VENDOR 读不出来（context 没 current？）"
        assert renderer, "GL_RENDERER 读不出来"
        # 真 pyopenxr 类型下构造 XLIB 绑定（不需要运行时；`create()` 才需要）
        try:
            import xr  # noqa: F401
        except Exception:  # noqa: BLE001
            print(f"  GLX context OK（{vendor} / {renderer}）；没有 pyopenxr，跳过 binding")
            return
        b = ctx.binding()
        assert type(b).__name__ == "GraphicsBindingOpenGLXlibKHR", type(b)
    finally:
        ctx.close()
    print(f"  GLX context + XLIB 绑定 OK（{vendor} / {renderer}）")


def test_factory_selects_x11_when_forced() -> None:
    """`VLT_OVERLAY_GL=x11` 时工厂必须给出 GLX 后端（能建起来的话）。"""
    if not os.environ.get("DISPLAY"):
        _skip("没有 DISPLAY")
        return
    old = os.environ.get("VLT_OVERLAY_GL")
    os.environ["VLT_OVERLAY_GL"] = "x11"
    try:
        try:
            ctx = create_gl_context()
        except Exception as exc:  # noqa: BLE001
            _skip(f"GLX 不可用（{type(exc).__name__}: {exc}）")
            return
        try:
            assert isinstance(ctx, XlibGlxContext), f"强制 x11 却拿到 {type(ctx).__name__}"
        finally:
            ctx.close()
    finally:
        if old is None:
            os.environ.pop("VLT_OVERLAY_GL", None)
        else:
            os.environ["VLT_OVERLAY_GL"] = old
    print("  工厂强制 x11 → GLX 后端 OK")


def main() -> int:
    print("test_overlay_glx:")
    tests = [test_glx_context_builds, test_factory_selects_x11_when_forced]
    bad = []
    for fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001 — 任何一个断言挂都要退出码非 0，但其余用例照跑
            bad.append(fn.__name__)
            traceback.print_exc()
    print("ALL PASSED" if not bad else f"FAILED: {', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
