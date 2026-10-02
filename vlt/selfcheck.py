"""打包产物的**内建自检入口**（给验收脚本用，不面向用户）。

## 为什么需要它

AppImage 换成 PyInstaller 布局后，包里**没有独立解释器**了 ——
`scripts/verify_appimage.py` 原来那套「用包内 python -c ... 跑探针」的办法失效。
所以把探针做进产物本身，由验收脚本以子进程方式调用：

    VRChatLiveTranslate --verify-imports [--allow-fat]
    VRChatLiveTranslate --verify-render <out.png>
    VRChatLiveTranslate --verify-tk-fonts

## 返回码语义（与 verify_appimage.py 的判定对齐）

    0 = 通过；1 = 失败（原因写 stderr）；2 = 跳过（环境原因，如无显示器）

⚠️ 这些开关只在 `run_gui.py` 的最前面被拦截（进不去 GUI）；别把它们当普通 CLI 用。
⚠️ `--verify-imports` 必须在**冻结产物里**跑才有意义：它断言的是「打进包里的那份
    代码/数据」的状态（xr 瘦身、图层 flag、禁列模块），源码运行会误报。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 与 verify_appimage.py 的判定阈值一致（xr 瘦身后实测 ~23MB；未瘦身 ~126MB）
XR_SLIM_MAX_MB = 60

# 调试用 API layer：我们不 enable，构建脚本会把它们连 .so 带 .json 删掉
XR_DEBUG_LAYERS = (
    "libXrApiLayer_api_dump.so",
    "libXrApiLayer_core_validation.so",
    "libXrApiLayer_best_practices_validation.so",
)


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)


def verify_imports(allow_fat: bool = False) -> int:
    """包内导入 + 图层 alpha flag + 反向排除 + xr 瘦身。

    `allow_fat=True`（对应构建脚本的 --no-slim）时跳过 xr 瘦身相关的断言。
    """
    # ① 核心模块导入（PyInstaller 漏收动态导入的话，这里就是第一现场）
    import vlt.engine  # noqa: F401
    import vlt.gui  # noqa: F401
    import vlt.output.openxr_overlay  # noqa: F401
    import vlt.platform  # noqa: F401
    import vlt.platform.wayland  # noqa: F401 — 桌面字幕原生窗（漏收 = 用户侧回落 Tk）
    import vlt.platform.x11  # noqa: F401 — 桌面字幕原生窗（X11 ARGB；漏收 = 回落 Tk）
    from vlt.output.openxr_overlay import layer_alpha_flags

    # ② 图层 alpha 开关必须在**打进包里的那份代码**里（少了它，手腕屏面板整层按不透明
    #    合成 = 蓝框外面一圈黑边，贴图里的透明边距被当实心黑画出来）
    flags = int(layer_alpha_flags())
    if not (flags & 0x2 and flags & 0x4):
        _fail(f"图层 alpha flag 不对：0x{flags:x}（应同时带 BLEND 0x2 / UNPREMULTIPLIED 0x4）")
        return 1

    # ③ 反向排除：Linux 产物里不许有 Windows 独占模块
    import importlib

    for bad in ("vlt.platform.win", "vlt.output.openvr_overlay"):
        try:
            importlib.import_module(bad)
        except ImportError:
            continue
        _fail(f"Linux 产物里不该有 {bad}")
        return 1

    # ④ xr 瘦身：让**包内的 pyopenxr 自己报**当前平台目录，再断言除它以外一个不剩。
    #    绝不硬编码目录名 —— pyopenxr 改过命名（win32→windows_x86_64 等），
    #    硬编码的黑名单会和构建脚本的删除**一起**静默失效（v0.5.0 的教训）。
    if not allow_fat:
        import os

        import xr.api_layer
        import xr.library
        from xr.api_layer.layer_path import py_layer_library_path

        api_root = Path(py_layer_library_path()).parent                  # …/xr/api_layer/<平台>
        lib_root = Path(xr.library.openxr_loader_library._name).parent  # …/xr/library/<平台>
        want_api = {api_root.name}
        for p in os.environ.get("XR_API_LAYER_PATH", "").split(os.pathsep):
            if p.strip():
                want_api.add(Path(p).name)
        want_lib = {lib_root.name}

        for base, want in ((api_root.parent, want_api), (lib_root.parent, want_lib)):
            have = {d.name for d in base.iterdir() if d.is_dir() and d.name != "__pycache__"}
            extra = sorted(have - want)
            if extra:
                _fail(f"xr/{base.name} 残留其他平台目录（瘦身没生效）：{extra}")
                return 1
            if not (want & have):
                _fail(f"xr/{base.name} 缺当前平台目录：{sorted(want)}")
                return 1

        for name in XR_DEBUG_LAYERS:
            if (api_root / name).exists():
                _fail(f"xr 调试层没被清掉：{api_root.name}/{name}（构建脚本的瘦身规则失效了？）")
                return 1
        if not (api_root / "libXrApiLayer_python.so").exists():
            _fail("xr 缺 libXrApiLayer_python.so（import xr 会直接失败）")
            return 1

        size = sum(f.stat().st_size for f in (api_root.parent.parent).rglob("*") if f.is_file())
        if size > XR_SLIM_MAX_MB * 1024 * 1024:
            _fail(f"xr 瘦身后仍有 {size // 1024 // 1024}MB（上限 {XR_SLIM_MAX_MB}MB）")
            return 1
        print(f"xr 只留当前平台：{sorted(want_api)}（{size / 1024 / 1024:.0f}MB）")

    # ⑤ X11（XLIB）绑定结构体：**只构造、不建上下文** —— 钉住「PyOpenGL 的 GLX 类型
    #    真在包里」。与 OpenGL.platform.egl 同一类坑：这些模块靠运行时 import 名
    #    解析，静态分析看不到；缺了的话 X11 用户建 session 时才炸。
    import ctypes as _ct

    import xr
    _f = {n: tp for n, tp in xr.GraphicsBindingOpenGLXlibKHR._fields_}
    xr.GraphicsBindingOpenGLXlibKHR(
        x_display=_ct.cast(_ct.c_void_p(1), _f["x_display"]),
        visualid=1,
        glx_fbconfig=_ct.cast(_ct.c_void_p(2), _f["glx_fbconfig"]),
        glx_drawable=3,
        glx_context=_ct.cast(_ct.c_void_p(4), _f["glx_context"]),
    )
    print("X11/XLIB 绑定结构体可构造（PyOpenGL GLX 类型在包里）")

    print("OK")
    return 0


def verify_render(out_path: str) -> int:
    """离线渲染一帧手腕面板（不碰 VR 运行时 / 音频 / 显示器）。

    参数与 `vlt/output/overlay.py --demo` 的默认一致 —— 验收脚本拿产物渲染的 PNG
    检查「边距全透明 + 底板半透明」。
    """
    from vlt.output.overlay import OverlayConfig, render_panel

    cfg = OverlayConfig()
    img = render_panel(
        "Hello! I'm Nixi. This sentence is being translated in real time, "
        "let's see how it looks on your wrist.",
        "你好，我是逆袭。这句话正在被实时翻译，看看贴在你手腕上是什么效果。",
        cfg,
    )
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"RENDERED:{out} {img.size[0]}x{img.size[1]}")
    return 0


def verify_tk_fonts() -> int:
    """Tk 能不能看见宿主机 fontconfig 里的中日韩字体。

    返回码：0 = 有中日韩族；1 = 没有（Xft 版 Tk 坏了 / 宿主没字体）；
    2 = 没有显示器可用（跳过 = 未验证，不判红）。
    """
    try:
        import tkinter as tk
        import tkinter.font as tkfont

        root = tk.Tk()
        root.withdraw()
    except Exception as exc:  # noqa: BLE001 — 无显示器（xvfb 未起）就走「跳过」
        print(f"SKIP:{type(exc).__name__}")
        return 2
    fams = list(tkfont.families(root))
    root.destroy()
    cjk = [f for f in fams if any(k in f for k in ("CJK", "Source Han", "Noto Sans SC", "WenQuanYi"))]
    print(f"FAMS:{len(fams)} CJK:{len(cjk)}" + (f" FIRST:{cjk[0]}" if cjk else ""))
    return 0 if cjk else 1


def main(argv: list[str]) -> int:
    args = list(argv)
    try:
        if "--verify-imports" in args:
            return verify_imports(allow_fat="--allow-fat" in args)
        if "--verify-render" in args:
            i = args.index("--verify-render")
            if i + 1 >= len(args):
                print("用法：--verify-render <out.png>", file=sys.stderr)
                return 2
            return verify_render(args[i + 1])
        if "--verify-tk-fonts" in args:
            return verify_tk_fonts()
    except Exception as exc:  # noqa: BLE001 — 自检探针不许把裸 traceback 糊到用户脸上
        _fail(f"{type(exc).__name__}: {exc}")
        return 1
    print("用法：--verify-imports [--allow-fat] | --verify-render <out.png> | --verify-tk-fonts",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
