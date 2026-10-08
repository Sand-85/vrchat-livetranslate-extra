"""界面 DPI 几何缩放（`ui.scale`）：主窗 + 设置弹窗的运行时口径。

## 背景

Tk 的 `tk scaling`（点→像素）本来就按系统 DPI 把**字号**放大；但窗口/弹窗几何是像素，
不跟着放大会「字大窗小」。PR #70 只在 Windows 声明 Per-Monitor DPI 感知后按同一因子放大
主窗几何；本改动把这个口径**推广到 Linux**（`tk scaling` 反映 Xft.dpi / 屏 DPI），
并覆盖到设置弹窗。

## 判据

1. `ui_theme.settings_metrics()` 是**纯函数**：基准 × scale、夹到 [1, 3]；
2. `gui_layout._resolve_scale()`：`ui.scale: auto|缺失|非法` → 跟随 `tk scaling`；
   数值 → 覆盖（同样夹取）；
3. 起真窗口：`ui.scale: 1.5` 的 `_dpi_scale`、设置有效尺寸、主窗几何都随 1.5 放大，
   且**严格大于** `ui.scale: 1.0`（同一块屏、同一语言）。

## 全程沙箱（不强加副作用）

临时 `HOME`/`USERPROFILE` + 临时 config；`_start_proxy` / `_start_device_scan` 打桩 ——
**绝不**声明虚拟声卡 / 碰用户 PipeWire（见 docs/平台约束记录.md 第四节）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 干净环境（CI）没有 API key；load_config 用 require_key=False，但仍显式给一个，
# 免得任何路径顺手读用户凭据。
os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-dpi-" + "0123456789abcdef")


# ---------------------------------------------------------------- 沙箱工具

def _isolate_env() -> tuple[Path, dict]:
    tmp = Path(tempfile.mkdtemp(prefix="vlt-dpi-"))
    saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "DASHSCOPE_API_KEY")}
    os.environ["HOME"] = str(tmp)
    os.environ["USERPROFILE"] = str(tmp)
    os.environ.pop("DASHSCOPE_API_KEY", None)
    return tmp, saved


def _restore_env(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _stub_proxy() -> tuple:
    """打桩「一起来就起麦克风代理 / 扫设备」两个**真方法**（不碰用户运行时）。"""
    import vlt.gui as gui_mod
    T = gui_mod.TranslationGUI
    saved = (T._start_proxy, T._start_device_scan)
    T._start_proxy = lambda self: None
    T._start_device_scan = lambda self: None
    return saved


def _unstub_proxy(saved: tuple) -> None:
    import vlt.gui as gui_mod
    gui_mod.TranslationGUI._start_proxy, gui_mod.TranslationGUI._start_device_scan = saved


def _cancel_jobs(gui) -> None:
    for attr in ("_update_check_job", "_updated_hint_job"):
        job = getattr(gui, attr, None)
        if job is not None:
            try:
                gui._root.after_cancel(job)
            except Exception:  # noqa: BLE001
                pass
            setattr(gui, attr, None)


def _build(tmp: Path, scale: str):
    """用临时 config（ui.lang=zh, ui.scale=scale）起一个真窗口。"""
    cfg = tmp / "config.yaml"
    body = "ui:\n  lang: zh\n"
    if scale is not None:
        body += f"  scale: {scale}\n"
    cfg.write_text(body, encoding="utf-8")

    import vlt.config as cfg_mod
    import vlt.gui as gui_mod
    cfg_mod.DEFAULT_CONFIG = cfg
    gui_mod.DEFAULT_CONFIG = cfg

    from vlt.gui import TranslationGUI
    gui = TranslationGUI()
    _cancel_jobs(gui)
    gui._root.update()
    return gui


def _wh(win) -> tuple[int, int]:
    """从 geometry 字符串取 (宽, 高)。"""
    wh = win.geometry().split("+", 1)[0]
    w, h = wh.split("x")
    return int(w), int(h)


# ---------------------------------------------------------------- ① 纯函数

def test_settings_metrics_pure() -> None:
    from vlt.ui_theme import settings_metrics

    base = settings_metrics(1.0)
    assert (base.width, base.wrap, base.min_h, base.max_h, base.chrome_h) == \
        (760, 660, 360, 900, 66), base
    big = settings_metrics(1.5)
    assert (big.width, big.wrap, big.min_h, big.max_h, big.chrome_h) == \
        (1140, 990, 540, 1350, 99), big
    # 夹取：<1 抬到 1.0（缩小无意义）、>3 压到 3.0
    assert settings_metrics(0.3).width == 760, "scale<1 必须夹到 1.0"
    assert settings_metrics(9.0).width == 760 * 3, "scale>3 必须夹到 3.0"
    print("  ✓ settings_metrics：基准 / 放大 / 上下夹取 OK")


# ---------------------------------------------------------------- ② 取值规则

class _FakeTk:
    def __init__(self, scaling: float) -> None:
        self._scaling = scaling
        self.sets: list = []

    def call(self, *a):  # noqa: ANN002
        if len(a) == 3 and a[0] == "tk" and a[1] == "scaling":
            self.sets.append(a[2])
            self._scaling = float(a[2])
            return ""
        return self._scaling


class _FakeRoot:
    def __init__(self, scaling: float) -> None:
        self.tk = _FakeTk(scaling)


class _FakeGui:
    def __init__(self, scale, scaling: float = 1.3333333) -> None:
        self._cfg = type("C", (), {"ui": {"scale": scale}})()
        self._root = _FakeRoot(scaling)


def test_apply_scale_rules() -> None:
    from vlt.gui_layout import _DPI_PT, _apply_scale

    # auto / 缺失 / 空 / 非法 → 跟随 tk scaling（2.0 / (96/72) = 1.5），且**不**改 tk scaling
    for val in ("auto", "Auto", None, "", "nonsense", []):
        g = _FakeGui(val, scaling=2.0)
        assert abs(_apply_scale(g) - 1.5) < 1e-9, (val, _apply_scale(g))
        assert g._root.tk.sets == [], f"{val!r} 不该去改 tk scaling（auto 只读）"
    # 数值覆盖（含夹取）：必须把 tk scaling 设成 s×96/72，字号跟着整体放大
    for val, exp in ((1.0, 1.0), ("1.25", 1.25), (2.0, 2.0), (9, 3.0), (0.1, 1.0)):
        g = _FakeGui(val, scaling=1.3333333)
        got = _apply_scale(g)
        assert abs(got - exp) < 1e-9, (val, got)
        assert g._root.tk.sets and abs(float(g._root.tk.sets[-1]) - exp * _DPI_PT) < 1e-9, \
            (val, g._root.tk.sets)
    print("  ✓ _apply_scale：auto 只读 / 数值覆盖时同步改 tk scaling（字号+几何） OK")


# ---------------------------------------------------------------- ③ 真窗口联动

def test_window_scaling_integration() -> None:
    tmp, saved_env = _isolate_env()
    stub = _stub_proxy()
    # Xvfb（CI 默认 640×480）太小，会把设置窗宽也夹住、掩盖「760→1140」的放大。
    # 给这一条固定一块 1920×1080 的虚拟屏（只改 Tk 的 Python 侧方法，测完还原）。
    import tkinter
    _sw, _sh = tkinter.Misc.winfo_screenwidth, tkinter.Misc.winfo_screenheight
    tkinter.Misc.winfo_screenwidth = lambda self: 1920
    tkinter.Misc.winfo_screenheight = lambda self: 1080
    g10 = g15 = None
    try:
        g10 = _build(tmp, "1.0")
        s10 = float(g10._root.tk.call("tk", "scaling"))     # tk scaling 是进程级，建下一个前先读
        g15 = _build(tmp, "1.5")
        s15 = float(g15._root.tk.call("tk", "scaling"))

        # 缩放档位定死（ui.scale 覆盖生效，不看宿主 DPI）
        assert g10._dpi_scale == 1.0, g10._dpi_scale
        assert g15._dpi_scale == 1.5, g15._dpi_scale
        # 显式 ui.scale 必须**连字号一起**放大：tk scaling 被设成 s×96/72
        # （Tk 回读有小幅量化，放宽容差）
        assert abs(s10 - 1.0 * (96 / 72)) < 0.01, s10
        assert abs(s15 - 1.5 * (96 / 72)) < 0.01, s15
        # 字号真的变大了：同一套文案的自然宽度 1.5 档 > 1.0 档
        assert g15._root.winfo_reqwidth() > int(g10._root.winfo_reqwidth() * 1.2), \
            (g10._root.winfo_reqwidth(), g15._root.winfo_reqwidth())

        # 设置弹窗有效尺寸（纯口径；打桩屏幕够大 → 不被夹）
        assert g10._settings_metrics.width == 760, g10._settings_metrics
        assert (g15._settings_metrics.width, g15._settings_metrics.wrap) == (1140, 990), \
            g15._settings_metrics

        # 主窗几何：**只判单调**（1.5 严格大于 1.0），不做等值复算。
        # ⚠️ 不能用「按屏幕复算 + 等值比较」：Windows 上 Tk 会把顶层窗口尺寸夹到**真实**
        #    屏幕/工作区，请求的 1410×900 在小的 CI runner 上会回读成更小的值（实测
        #    1335×749），而复算用的是这里被打桩的虚拟屏 → 必然对不上。Xvfb 会照单全收，
        #    所以只有 Windows 暴露过（见 PR #72 的 CI）。
        w10, h10 = _wh(g10._root)
        w15, h15 = _wh(g15._root)
        assert w15 > w10, f"1.5 的窗宽 {w15} 没有大于 1.0 的 {w10}"
        assert h15 > h10, f"1.5 的窗高 {h15} 没有大于 1.0 的 {h10}"
        print(f"  ✓ 主窗 1.0={w10}x{h10} → 1.5={w15}x{h15}；设置宽 760→1140、换行 660→990 OK")
    finally:
        for g in (g10, g15):
            if g is not None:
                try:
                    g._on_close()
                except Exception:  # noqa: BLE001
                    pass
        tkinter.Misc.winfo_screenwidth, tkinter.Misc.winfo_screenheight = _sw, _sh
        _unstub_proxy(stub)
        _restore_env(saved_env)


# ---------------------------------------------------------------- ④ 屏幕兜底

def test_settings_metrics_clamped_to_screen() -> None:
    """窄屏时设置窗宽度与**换行宽**都要跟着夹回来。

    换行宽尤其关键：它是建页时按 `wraplength=` 定死的，若只夹宽度不夹它，
    长说明会按超宽的 wraplength 排版、建完就横向溢出被裁（事后改不回来）。
    """
    import vlt.gui_settings as gs
    import vlt.ui_theme as ut

    saved = (gs.SETTINGS_WIDTH, gs.SETTINGS_WRAP, gs.SETTINGS_MIN_H,
             gs.SETTINGS_MAX_H, gs.SETTINGS_CHROME_H)

    class _R:
        def winfo_screenwidth(self):
            return 1280

    class _G:
        _dpi_scale = 2.0
        _root = _R()

    try:
        g = _G()
        gs.apply_settings_metrics(g)
        m = g._settings_metrics
        assert m.width == 1280 - 16, m
        assert m.wrap < ut.settings_metrics(2.0).wrap, \
            f"换行宽没有随宽度一起收窄：{m.wrap}"
        # 收窄量正好等于宽度被夹掉的量（保持「wrap = width - 固定留白」的关系）
        assert ut.settings_metrics(2.0).wrap - m.wrap == ut.settings_metrics(2.0).width - m.width
        print(f"  ✓ 窄屏设置窗：width 1520→{m.width}、wrap 1320→{m.wrap}（同步收窄）OK")
    finally:
        (gs.SETTINGS_WIDTH, gs.SETTINGS_WRAP, gs.SETTINGS_MIN_H,
         gs.SETTINGS_MAX_H, gs.SETTINGS_CHROME_H) = saved


def test_minsize_and_geometry_clamped_to_screen() -> None:
    """缩放后窗口别比屏幕还大：minsize 与 geometry 都要被夹到可用区。

    直接给已建好的窗口打桩一个很小的「屏幕」（1280×720），再跑一次 `_fit_window_width`
    —— 不依赖 CI 真实分辨率（CI 的 Xvfb 只有 640×480），也因此能稳定复现「放大后超屏」。
    用 1.5 档（`760×1.5=1140` 仍在屏内，floor 不会盖过夹取，结论可预期）。
    """
    tmp, saved_env = _isolate_env()
    stub = _stub_proxy()
    gui = None
    try:
        gui = _build(tmp, "1.5")
        root = gui._root
        s = gui._dpi_scale
        root.winfo_screenwidth = lambda: 1280       # type: ignore[assignment]
        root.winfo_screenheight = lambda: 720       # type: ignore[assignment]
        gui._fit_window_width()
        root.update_idletasks()
        root.update()               # 让 wm geometry 反映新尺寸后再读
        w, h = _wh(root)
        # 宽度：不夹的话是 base_w=1410，这里必须被夹到屏内
        assert w <= 1280 - 16, f"窗宽 {w} 超过了打桩屏幕 1280"
        assert w < int(940 * s), f"窗宽 {w} 没被夹小（未夹应为 {int(940 * s)}）"
        # 高度夹取的下限是 460×s（保证内容可见），但仍必须小于未夹的 600×s
        assert h <= max(int(460 * s), 720 - 90), f"窗高 {h} 越过夹取上限"
        assert h < int(600 * s), f"窗高 {h} 没被夹小（未夹应为 {int(600 * s)}）"
        min_w, min_h = root.minsize()
        assert min_w <= 1280 - 16, f"minsize 宽 {min_w} 超过了屏幕（窗口会锁死）"
        assert min_h <= 720 - 90, f"minsize 高 {min_h} 超过了屏幕（窗口会锁死）"
        print(f"  ✓ 小屏兜底：geometry={w}x{h}、minsize={min_w}x{min_h} 都在屏内 OK")
    finally:
        if gui is not None:
            try:
                gui._on_close()
            except Exception:  # noqa: BLE001
                pass
        _unstub_proxy(stub)
        _restore_env(saved_env)


def test_settings_dialog_clamped_to_screen() -> None:
    """小屏 + 高 DPI：设置弹窗宽高都得夹进屏内（尤其高度——`min_h=360×s` 会超屏）。

    设置窗是 **Toplevel**（`winfo_screenheight` 取的是它自己的屏），所以按**类**打桩屏幕，
    而不是像主窗那样打桩 `gui._root` 实例。
    """
    import tkinter

    tmp, saved_env = _isolate_env()
    stub = _stub_proxy()
    _sw, _sh = tkinter.Misc.winfo_screenwidth, tkinter.Misc.winfo_screenheight
    tkinter.Misc.winfo_screenwidth = lambda self: 1280
    tkinter.Misc.winfo_screenheight = lambda self: 720
    gui = None
    try:
        gui = _build(tmp, "2.0")           # 建窗时即按打桩屏幕夹取
        gui._size_settings_window()
        gui._root.update_idletasks()
        gui._root.update()
        wh, _, _rest = gui._settings_win.geometry().partition("+")
        w, h = (int(v) for v in wh.split("x"))
        assert w <= 1280 - 16, f"设置窗宽 {w} 超过屏幕 1280"
        assert h <= 720 - 90, f"设置窗高 {h} 超过屏幕可用区（min_h 没夹住）"
        # 1.0 档宽 760、2.0 档需 1520 → 在 1280 屏上必须被夹到 1264。
        # 精确值判在**纯量** `_settings_metrics` 上；窗口回读只判 ≤（Windows 上 Tk 还会把
        # 顶层窗口夹到真实屏幕/工作区，可能比 1264 更小）。
        assert gui._settings_metrics.width == 1280 - 16, gui._settings_metrics
        print(f"  ✓ 小屏设置窗兜底：{w}x{h}（≤ 1264x630，有效宽 {gui._settings_metrics.width}）OK")
    finally:
        if gui is not None:
            try:
                gui._on_close()
            except Exception:  # noqa: BLE001
                pass
        tkinter.Misc.winfo_screenwidth, tkinter.Misc.winfo_screenheight = _sw, _sh
        _unstub_proxy(stub)
        _restore_env(saved_env)


if __name__ == "__main__":
    print("test_dpi_scale:")
    test_settings_metrics_pure()
    test_apply_scale_rules()
    test_settings_metrics_clamped_to_screen()
    test_window_scaling_integration()
    test_minsize_and_geometry_clamped_to_screen()
    test_settings_dialog_clamped_to_screen()
    print("ALL PASSED")
