"""UI 构建布局：从 ``vlt.gui`` 提取的 Tk 控件构造代码。

⚠️ 本模块 **不许** ``import vlt.gui``（防循环引用）。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import ui_tk
from . import gui_voice
from .i18n import t
from .platform import IS_WINDOWS
from .paths import BUNDLE_DIR
from .ui_text import (
    SOURCE_LANGS, TARGET_LANGS,
    _lang_label,
)
from .ui_theme import (
    ACCENT, BORDER, PANEL, SURFACE, TEXT,
)
from .ui_tk import _combo_width, _char_width_for


def apply_ui_font(root) -> None:
    """按当前平台重绑界面字体常量。"""
    ui_tk.apply_ui_font(root)
    # 同步 gui 模块全局（延迟导入避免循环）
    import vlt.gui as _g
    for attr in ("FONT", "FONT_SMALL", "FONT_META", "FONT_UI", "FONT_STATUS",
                 "FONT_BOLD_SM", "FONT_BOLD_MD", "FONT_BOLD_LG"):
        setattr(_g, attr, getattr(ui_tk, attr))


def apply_power_state(btn, state: str) -> None:
    """把主控制行那个「开始 / 停止」单按钮刷成 *state* 对应的外观。

    文案 + 颜色 + 可用态是**同一个状态的三个面** —— 分三处各改一次必然漂移
    （本项目踩过：只改文案忘了改样式，翻译中看着像「还能再点一次开始」）。

    *state*：``"idle"`` 蓝底「开始翻译」；``"running"`` 红底「停止翻译」；
    ``"stopping"`` 红底「停止翻译」但置灰（旧引擎还在关麦克风/虚拟声卡，放开会抢设备）。
    """
    if btn is None:
        return                          # headless 下没有控件
    if state == "running":
        text, style, wstate = t("停止翻译"), "PowerDanger.TButton", tk.NORMAL
    elif state == "stopping":
        text, style, wstate = t("停止翻译"), "PowerDanger.TButton", tk.DISABLED
    else:                               # 其它任何值（含 "idle"）→ 蓝底「开始翻译」，防御：绝不抛
        text, style, wstate = t("开始翻译"), "Power.TButton", tk.NORMAL
    try:
        btn.configure(text=text, style=style, state=wstate)
    except Exception as exc:            # noqa: BLE001
        # 界面刷新失败不该把调用方（引擎启停）带崩，但**必须留痕**：
        # 静默吞掉的话，症状是「按钮永远停在旧状态」，日志里一个字都没有。
        print(f"[gui] ⚠️ 刷新开始/停止按钮失败：{type(exc).__name__}: {exc}", flush=True)


def _enable_windows_dpi_awareness() -> None:
    """Windows 上声明 Per-Monitor DPI 感知（必须在创建 Tk root **之前**调用）。

    不声明的话 Tk 被系统做位图拉伸渲染：高缩放屏（如 150%）上整个窗文字发虚。
    只影响渲染清晰度，不影响音频/网络路径。失败静默（老系统没有 shcore 就退
    回 user32，再不行就保持原样）。
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes
    except Exception:  # noqa: BLE001
        return
    try:
        # Per-Monitor v2（Win 10 1703+）
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 — 老系统没有 shcore
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001 — 两个都失败就保持系统默认，界面照常起
            pass


def _windows_dpi_scale(gui) -> float:
    """取 Tk 自己算出的 DPI 缩放（1.0 = 100%）。非 Windows 恒返回 1.0。

    声明 DPI 感知后 Tk 会把 `tk scaling` 按真实屏幕 DPI 重算（点→像素换算），
    字号因此自动变大变清晰；但窗口/弹窗的几何尺寸是**像素**，不按同比例放大就会
    显得整窗变小、文案挤在一起。这里按同一比例放大基准尺寸，两者口径才一致。

    ⚠️ 只在 Windows 生效：Linux 上 Tk scaling 反映的是 Xft.dpi（HiDPI 下也可能
    >1.333），跟着缩放就等于**改变了 Linux 既有的窗口几何口径** —— 那不是本改动的
    目的，也会让两岸行为分叉。非 Windows 一律返回 1.0，几何逐字保持原样。
    """
    if not IS_WINDOWS:
        return 1.0
    try:
        s = float(gui._root.tk.call("tk", "scaling")) / (96.0 / 72.0)
    except Exception:  # noqa: BLE001
        return 1.0
    return max(1.0, min(3.0, s))


def build_ui(gui) -> None:
    """构建完整界面。"""
    _enable_windows_dpi_awareness()
    gui._root = tk.Tk()
    gui._root.title(t("VRChat 实时同传"))
    gui._dpi_scale = _windows_dpi_scale(gui)
    if gui._dpi_scale > 1.0:
        gui._root.geometry(f"{int(940 * gui._dpi_scale)}x{int(600 * gui._dpi_scale)}")
    else:
        gui._root.geometry("940x600")
    gui._root.minsize(int(928 * gui._dpi_scale), int(460 * gui._dpi_scale))
    gui._root.configure(bg=PANEL)
    apply_ui_font(gui._root)
    set_window_icon(gui)
    gui._apply_theme()
    build_controls(gui)
    build_output_row(gui)
    gui._build_room_row()
    _divider(gui)
    gui._build_chat()
    _divider(gui)
    gui._build_input_row()
    gui._build_status()
    gui._build_settings_dialog()
    _apply_dark_titlebar(gui)
    gui._root.protocol("WM_DELETE_WINDOW", gui._on_close)
    gui._update_direction_langs()
    gui._fit_window_width()
    gui._check_api_key()
    gui._poll()
    gui._start_device_scan()
    gui._update_check_job = gui._root.after(3000, gui._schedule_update_check)
    gui._check_pending_update_at_startup()
    gui._schedule_version_changed_hint()
    # ↓ 本仓库增强：启动后延迟拉「本账号自定义音色」与「范本样本更新」
    # ⚠️ 都用 `after()` **延迟**触发，别在这里直接起线程：它们持着 GUI 的绑定方法，
    #    而 CI 那些「真开窗口」的用例不跑 mainloop → `after` 永不触发（干净）；
    #    真跑时启动几秒后才联网，也避开启动高峰。（之前直接起线程，把 Tk 拆卸竞态
    #    `Tcl_AsyncDelete` 的间歇红放大了。）
    gui._root.after(4000, gui._kick_tts_voice_list)   # 拉本账号自定义音色（只读、免费）
    gui._root.after(4500, gui._kick_sample_check)     # 查范本样本更新（只读、几 KB）


def fit_window_width(gui) -> None:
    """按当前界面语言定窗口宽度。"""
    from . import i18n as _i18n_mod
    s = max(1.0, float(getattr(gui, "_dpi_scale", 1.0)))
    base_w = int(940 * s)
    try:
        gui._root.update_idletasks()
        need = gui._root.winfo_reqwidth() + 8
        want = max(base_w, need)
        screen = int(gui._root.winfo_screenwidth() or 0)
        if screen:
            want = min(want, max(int(760 * s), screen - 16))
        height = max(int(600 * s), gui._root.winfo_reqheight())
        gui._root.geometry(f"{want}x{height}")
        gui._root.minsize(min(want, need), int(460 * s))
        if want > base_w:
            print(f"[ui] 界面语言 {_i18n_mod.current_language()}：文案较宽，"
                  f"窗口按需求开到 {want}px（基准 {base_w} / 需求 {need}）", flush=True)
        if need > want:
            print(f"[ui] ⚠️ 屏幕只有 {screen}px，内容需要 {need}px 装不下，"
                  f"窗口开到 {want}px", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[ui] ⚠️ 窗口宽度自适应失败：{type(exc).__name__}: {exc}", flush=True)


def set_window_icon(gui) -> None:
    """窗口图标。"""
    assets = BUNDLE_DIR / "assets"
    try:
        if IS_WINDOWS:
            ico = assets / "app.ico"
            if ico.exists():
                gui._root.iconbitmap(default=str(ico)); return
        png = assets / "app.png"
        if png.exists():
            gui._icon_img = tk.PhotoImage(file=str(png))
            gui._root.iconphoto(True, gui._icon_img); return
        print(f"[gui] ⚠️ 没找到窗口图标（{assets}）", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[gui] ⚠️ 设置窗口图标失败：{exc}", flush=True)


def _apply_dark_titlebar(gui, win=None) -> None:
    """Windows 深色标题栏。"""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        w = win if win is not None else gui._root
        w.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(w.winfo_id())
        value = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def _divider(gui) -> None:
    tk.Frame(gui._root, bg=BORDER, height=1, bd=0,
             highlightthickness=0).pack(fill=tk.X)


def _vsep(parent) -> None:
    ttk.Separator(parent, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y,
                                                    padx=12, pady=5)


def _indicator_kw() -> dict:
    return dict(bg=PANEL, fg=TEXT, activebackground=PANEL,
                activeforeground="#ffffff", selectcolor=SURFACE,
                highlightthickness=0, bd=0, font=ui_tk.FONT_UI)


def _attach_edit_menu(gui, widget) -> None:
    """给文本输入控件挂右键菜单。"""
    try:
        menu = tk.Menu(widget, tearoff=0, bg=SURFACE, foreground=TEXT,
                       activebackground=ACCENT, activeforeground="#ffffff", bd=0)
        for label, action in ((t("剪切"), "<<Cut>>"), (t("复制"), "<<Copy>>"),
                              (t("粘贴"), "<<Paste>>"), (t("全选"), "<<SelectAll>>")):
            menu.add_command(label=label,
                             command=lambda a=action: widget.event_generate(a))
        widget._edit_menu = menu
        def _popup(event):
            try:
                widget.focus_set()
                menu.tk_popup(event.x_root, event.y_root)
            except Exception as exc:  # noqa: BLE001
                print(f"[ui] ⚠️ 右键菜单失败：{exc}", flush=True)
            finally:
                menu.grab_release()
        widget.bind("<Button-3>", _popup, add="+")
    except Exception as exc:  # noqa: BLE001
        print(f"[ui] ⚠️ 挂右键菜单失败：{exc}", flush=True)


# ================================================================ 第一行：会话控制

def build_controls(gui) -> None:
    """第一行 = 开/停 | 方向 | 语言对。"""
    ctrl = ttk.Frame(gui._root, padding=(14, 12, 14, 8))
    ctrl.pack(fill=tk.X)
    gui._settings_btn = ttk.Button(ctrl, text=t("⚙ 设置"),
                                    command=gui._open_settings)
    gui._settings_btn.pack(side=tk.RIGHT)
    gui._sponsor_btn = ttk.Button(ctrl, text=t("☕ 赞助"),
                                   command=gui._open_sponsor)
    gui._sponsor_btn.pack(side=tk.RIGHT, padx=(0, 8))
    gui._power_btn = ttk.Button(ctrl, text=t("开始翻译"), style="Power.TButton",
                                command=gui._on_power)
    gui._power_btn.pack(side=tk.LEFT)
    _vsep(ctrl)
    ttk.Label(ctrl, text=t("方向:")).pack(side=tk.LEFT)
    dir_frame = ttk.Frame(ctrl)
    dir_frame.pack(side=tk.LEFT)
    gui._direction_var = tk.StringVar(
        value=str((gui._cfg.ui or {}).get("direction", "mine")))
    if gui._direction_var.get() not in ("mine", "theirs", "dual"):
        gui._direction_var.set("mine")
    radio_kw = dict(variable=gui._direction_var,
                    command=gui._on_direction_change, **_indicator_kw())
    tk.Radiobutton(dir_frame, text=t("我说"), value="mine",
                   **radio_kw).pack(side=tk.LEFT, padx=(8, 0))
    tk.Radiobutton(dir_frame, text=t("别人说"), value="theirs",
                   **radio_kw).pack(side=tk.LEFT, padx=(10, 0))
    tk.Radiobutton(dir_frame, text=t("双向同时"), value="dual",
                   **radio_kw).pack(side=tk.LEFT, padx=(10, 0))
    _vsep(ctrl)
    gui._source_combo = ttk.Combobox(
        ctrl, values=[_lang_label(k) for k in SOURCE_LANGS], state="readonly",
        width=_combo_width([_lang_label(k) for k in SOURCE_LANGS]))
    gui._source_combo.pack(side=tk.LEFT)
    gui._source_combo.bind("<<ComboboxSelected>>", gui._on_lang_change)
    ttk.Label(ctrl, text="→", style="Dim.TLabel").pack(side=tk.LEFT, padx=6)
    gui._target_combo = ttk.Combobox(
        ctrl, values=[_lang_label(k) for k in TARGET_LANGS], state="readonly",
        width=_combo_width([_lang_label(k) for k in TARGET_LANGS]))
    gui._target_combo.pack(side=tk.LEFT)
    gui._target_combo.bind("<<ComboboxSelected>>", gui._on_lang_change)


# ================================================================ 第二行：输出面

def build_output_row(gui) -> None:
    """第二行 = 输出勾选 + key 状态入口。"""
    out_frame = ttk.Frame(gui._root, padding=(14, 0, 14, 10))
    out_frame.pack(fill=tk.X)
    gui._key_slot = ttk.Frame(out_frame)
    gui._key_slot.pack(side=tk.RIGHT, padx=(0, 4))
    gui._key_chip = ttk.Label(gui._key_slot, text="", style="Chip.TLabel")
    gui._key_btn = ttk.Button(gui._key_slot, text="", style="ChipWarn.TButton",
                               command=gui._open_qianwen_signup)
    gui._key_chip.pack()
    ttk.Label(out_frame, text=t("输出:"), style="Dim.TLabel").pack(side=tk.LEFT)
    gui._chatbox_var = tk.BooleanVar(
        value=bool((gui._cfg.ui or {}).get("chatbox", True)))
    gui._overlay_var = tk.BooleanVar(
        value=bool((gui._cfg.ui or {}).get("overlay", False)))
    _audio_cfg = ((gui._cfg.output.get("audio") or {})
                  if isinstance(gui._cfg.output, dict) else {})
    gui._vmic_var = tk.BooleanVar(value=bool(_audio_cfg.get("enabled", False)))
    ik = _indicator_kw()
    tk.Checkbutton(out_frame, text="chatbox", variable=gui._chatbox_var,
                   command=gui._on_chatbox_toggle, **ik).pack(side=tk.LEFT, padx=(6, 0))
    # 气泡显示**原文 / 译文**（互斥二选一，只影响 chatbox 气泡）。chatbox 没勾时置灰 ——
    # 与下面「🎙 原声」按钮同一套双保险：控件默认态是 NORMAL，不显式刷一次就会看着
    # 能点、点了没用（见 voice_mode_btn 那段注释）。宽度按两种文案里更宽的那个申请，
    # 否则切到「原文」时中/日/俄文案会被裁（test_i18n 的固定宽度守卫会抓）。
    gui._chatbox_text_btn = ttk.Button(
        out_frame, text="",
        width=max(_char_width_for(t("🌐 气泡: 译文"), ui_tk.FONT_UI, 6),
                  _char_width_for(t("📝 气泡: 原文"), ui_tk.FONT_UI, 6)),
        command=gui._on_chatbox_text_toggle)
    gui._chatbox_text_btn.pack(side=tk.LEFT, padx=(6, 0))
    gui._refresh_chatbox_text_btn()
    tk.Checkbutton(out_frame, text=t("手腕屏"), variable=gui._overlay_var,
                   command=gui._on_overlay_toggle, **ik).pack(side=tk.LEFT, padx=(10, 0))
    tk.Checkbutton(out_frame, text=t("译音输出"), variable=gui._vmic_var,
                   command=gui._save_audio_flag, **ik).pack(side=tk.LEFT, padx=(10, 0))
    # 原声/译音切换按钮（依赖 proxy；无 proxy 时禁用）
    from .ui_tk import FONT_UI as _FONT_UI
    gui._voice_mode_btn = ttk.Button(
        out_frame, text=t("🎙 原声"), width=max(_char_width_for(t("🎙 原声"), _FONT_UI, 6),
                                                  _char_width_for(t("🗣 译音"), _FONT_UI, 6)),
        command=lambda: gui_voice.toggle_voice_mode(
            gui._voice_ctx, vmic_get=lambda: bool(gui._vmic_var.get()),
            set_status=gui._set_status, refresh_btn=gui._refresh_voice_mode_btn))
    gui._voice_mode_btn.pack(side=tk.LEFT, padx=(10, 0))
    gui._voice_ctx.voice_mode_btn = gui._voice_mode_btn  # 同步到 ctx
    # ★ 建完立刻对齐一次档位/可用态，**不能**等到有人点它才刷新：
    #   本仓库当前 `_proxy` 恒为 None（Linux 永远无 proxy；Windows 的麦克风代理尚未接线），
    #   而 `refresh_voice_mode_btn` 对「无 proxy」的判定是**禁用**。少了这一行，按钮会以
    #   NORMAL 出厂，成了一颗看着能点、点了只 `return` 的死按钮（Linux 实测）。
    gui._refresh_voice_mode_btn()
    gui._desktop_var = tk.BooleanVar(
        value=bool((gui._cfg.ui or {}).get("desktop_overlay", False)))
    tk.Checkbutton(out_frame, text=t("桌面字幕"), variable=gui._desktop_var,
                   command=gui._on_desktop_toggle, **ik).pack(side=tk.LEFT, padx=(10, 0))
