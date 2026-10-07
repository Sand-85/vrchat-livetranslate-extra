"""桌面字幕面板 / 手腕屏调音台的 UI 构建与纯逻辑。

从 ``vlt.gui.TranslationGUI`` 的 C 区（约 L731-L1091 + L1912-L2097）提取而来。
所有 Tk 控件与可变状态通过 :class:`DesktopCtx` 传入，函数本身不持有 ``self`` 引用。

⚠️ 本模块 **不许** ``import vlt.gui``（防循环引用）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import ttk

from . import config as _cfg_mod
from .config_io import (
    _fmt_scalar,
    _write_config_text,
    _yaml_set_in_text,
    _yaml_set_or_create,
)
from .i18n import t
from .output.overlay import resolve_offset
from . import ui_tk
from .ui_theme import (
    ACCENT,
    FONT_MAX,
    FONT_MIN,
    PANEL,
    PANEL_H_MAX,
    PANEL_H_MIN,
    PANEL_W_MAX,
    PANEL_W_MIN,
    SETTINGS_WIDTH,
    SRC_FONT_MIN,
    SURFACE,
    TAB_INSET_X,
    TEXT,
    TEXT_DIM,
    TEXT_MUTED,
)
from .ui_tk import (
    _char_width_for,
    _combo_width,
    _int_fmt,
)


# ================================================================ 上下文

@dataclass
class DesktopCtx:
    """桌面字幕面板 / 调音台所需的全部 **控件引用** 与 **状态**。

    可变状态（``cfg`` / ``desktop_out``）由 gui.py 的薄壳方法管理，
    本模块的函数通过参数接收，不直接持有。
    """
    # ── 手腕屏调音台控件 ──
    anchor_combo: Any = None                # ttk.Combobox（锚点选择）
    tracker_var: Any = None                 # tk.StringVar（tracker 序号）
    tune_panel_w: int = 1024                # overlay 面板宽度 px
    anchor_label_to_key: dict = field(default_factory=dict)
    tune_values: dict[str, float] = field(default_factory=dict)
    tune_vars: dict[str, Any] = field(default_factory=dict)     # tk.DoubleVar
    tune_lbls: dict[str, Any] = field(default_factory=dict)     # ttk.Label
    tune_units: dict[str, str] = field(default_factory=dict)
    tune_grid: Any = None                   # ttk.Frame（供测试定位滑块）
    ov_save_job: Any = None                 # root.after 返回的 job id

    # ── 桌面字幕调音台控件 ──
    desktop_font_var: Any = None            # tk.DoubleVar
    desktop_font_lbl: Any = None            # ttk.Label
    desktop_srcfont_var: Any = None         # tk.DoubleVar
    desktop_srcfont_lbl: Any = None         # ttk.Label
    desktop_w_var: Any = None               # tk.DoubleVar
    desktop_w_lbl: Any = None               # ttk.Label
    desktop_h_var: Any = None               # tk.DoubleVar
    desktop_h_lbl: Any = None               # ttk.Label
    desktop_alpha_var: Any = None           # tk.DoubleVar
    desktop_alpha_lbl: Any = None           # ttk.Label
    desktop_drag_btn: Any = None            # ttk.Button
    desktop_tuned: set[str] = field(default_factory=set)
    desktop_save_job: Any = None
    desktop_alpha_touched: bool = False
    desktop_dragging: bool = False

    # ── 回调 ──
    set_status_fn: Optional[Callable] = None           # (level, msg) -> None

    # ── 回调（由 gui.py 注入） ──
    # 落盘 / 交互。桌面字幕的**唯一**保存在 gui_engine（那里有 Tk root、活的
    # DesktopOverlay 实例、以及与引擎 ctx 的同步）；本模块只管「控件长什么样、用户动了
    # 哪个键」，持久化一律回调出去 —— 早先这里留了一份同名的 schedule/save/toggle 副本，
    # root 没接上就成了死路（设置页滑块改了不落盘、热重载无从谈起）。
    schedule_overlay_save_fn: Optional[Callable] = None   # () -> None（手腕屏防抖落盘）
    schedule_desktop_save_fn: Optional[Callable] = None   # () -> None（桌面字幕防抖落盘）
    toggle_desktop_drag_fn: Optional[Callable] = None     # () -> None（解锁/锁定拖动）
    desktop_out_fn: Optional[Callable] = None             # () -> DesktopOverlay | None


# ================================================================ 手腕屏调音台页面

def build_tune_page(parent: ttk.Frame, ctx: DesktopCtx, overlay_dict: dict,
                    overlay_fn: Callable[[], dict] | None = None,
                    settings_width: int = SETTINGS_WIDTH) -> None:
    """「设置 → 手腕屏」页：滑块改动 → 写回 config.yaml → overlay 热重载（无需重启）。

    参数全都能拖：锚点 + 位置/旋转/大小/弯曲/透明度/字号/面板高/两块不透明度。

    *overlay_fn* 是可选的「取当前 overlay dict」闭包，供回调在运行时读到最新配置
    （gui.py 传 ``lambda: self._cfg.overlay if isinstance(self._cfg.overlay, dict) else {}``）。
    """
    ov = overlay_dict if isinstance(overlay_dict, dict) else {}
    off = ov.get("offset") or {}
    _sz = list(ov.get("size_px") or [1024, 440])
    ctx.tune_panel_w = int(_sz[0])
    # 位姿按**当前锚点**取（每个锚点各存一套；口径与两端后端共用 resolve_offset）
    ctx.anchor_label_to_key = {
        t("右手"): "right_hand", t("左手"): "left_hand",
        t("外部 tracker"): "tracker", t("头显前固定"): "hmd",
    }
    _key_to_label = {v: k for k, v in ctx.anchor_label_to_key.items()}
    pos, rot = resolve_offset(ov, str(ov.get("anchor", "right_hand")))
    pos, rot = list(pos), list(rot)
    ctx.tune_values = {
        "pos_x": float(pos[0]), "pos_y": float(pos[1]), "pos_z": float(pos[2]),
        "rot_x": float(rot[0]), "rot_y": float(rot[1]), "rot_z": float(rot[2]),
        # ⚠️ 兜底值必须等于 OverlayConfig 的默认值（由 test_tune_rot_range 钉住）
        "width_m": float(off.get("width_m", 0.23)),
        "curvature": float(off.get("curvature", 0.0)),
        "alpha": float(off.get("alpha", 0.9)),
        "font_size": float(ov.get("font_size", 36)),
        "source_font_size": float(ov.get("source_font_size", 29)),
        "panel_h": float(_sz[1]),
        "bg_alpha": float(ov.get("bg_alpha", 205)),
        "source_alpha": float(ov.get("source_alpha", 205)),
    }
    ctx.ov_save_job = None

    row = ttk.Frame(parent)
    row.pack(fill=tk.X, pady=(2, 2))
    ttk.Label(row, text=t("锚点:"), font=ui_tk.FONT_UI).pack(side=tk.LEFT)
    ctx.anchor_combo = ttk.Combobox(
        row, values=list(ctx.anchor_label_to_key),
        state="readonly", font=ui_tk.FONT_UI,
        width=_combo_width(ctx.anchor_label_to_key, 12),
    )
    ctx.anchor_combo.set(
        _key_to_label.get(str(ov.get("anchor", "right_hand")), t("右手")))
    ctx.anchor_combo.pack(side=tk.LEFT, padx=(4, 14))
    ctx.anchor_combo.bind("<<ComboboxSelected>>",
                          lambda _e: on_anchor_change(ctx, overlay_fn))
    ttk.Label(row, text=t("tracker 序号:"), font=ui_tk.FONT_UI).pack(side=tk.LEFT)
    ctx.tracker_var = tk.StringVar(value=str(ov.get("tracker_index", 0)))
    # 上限 7 = Linux role 表有 8 项（0=右腕 … 7=左脚）
    ttk.Spinbox(row, from_=0, to=7, width=3, font=ui_tk.FONT_UI,
                textvariable=ctx.tracker_var,
                command=lambda: save_overlay_cfg(ctx, overlay_fn=overlay_fn)).pack(
                    side=tk.LEFT, padx=(4, 0))
    ttk.Label(row, text=t("（仅锚点=外部 tracker 时有效）"),
              font=ui_tk.FONT_STATUS, foreground=TEXT_MUTED).pack(
                  side=tk.LEFT, padx=(10, 0))

    specs = [
        ("pos_x", t("位置X"), -0.30, 0.30, 0.005, "m"),
        ("pos_y", t("位置Y"), -0.30, 0.30, 0.005, "m"),
        ("pos_z", t("位置Z"), -0.30, 0.30, 0.005, "m"),
        ("rot_x", t("俯仰X"), -180.0, 180.0, 1.0, "°"),
        ("rot_y", t("偏航Y"), -180.0, 180.0, 1.0, "°"),
        ("rot_z", t("翻滚Z"), -180.0, 180.0, 1.0, "°"),
        ("width_m", t("大小"), 0.05, 0.80, 0.01, "m"),
        ("curvature", t("弯曲"), 0.0, 0.50, 0.01, ""),
        ("alpha", t("透明度"), 0.10, 1.00, 0.05, ""),
        ("font_size", t("译文字号"), 20, 64, 1, ""),
        ("source_font_size", t("原文字号"), 14, 48, 1, ""),
        ("panel_h", t("面板高"), 240, 560, 10, "px"),
        ("bg_alpha", t("底板不透明度"), 0, 255, 5, ""),
        ("source_alpha", t("原文不透明度"), 0, 255, 5, ""),
    ]
    label_w = max(6, max(_char_width_for(spec[1], ui_tk.FONT_UI) for spec in specs))
    ctx.tune_vars = {}
    ctx.tune_lbls = {}
    ctx.tune_units = {}
    # 一页放几列：让 Tk 自己量（先按 3 列建，装不下就降列重建）
    _avail = settings_width - 2 * TAB_INSET_X - 24
    grid = None
    for cols in (3, 2, 1):
        if grid is not None:
            grid.destroy()
            ctx.tune_vars, ctx.tune_lbls, ctx.tune_units = {}, {}, {}
        candidate = ttk.Frame(parent)
        build_tune_grid(candidate, specs, label_w, cols, ctx, overlay_fn)
        parent.update_idletasks()
        grid = candidate
        if candidate.winfo_reqwidth() <= _avail or cols == 1:
            break
    ctx.tune_grid = grid
    grid.pack(fill=tk.X, pady=(2, 2))


def build_tune_grid(grid: ttk.Frame, specs: list[tuple], label_w: int,
                    cols: int, ctx: DesktopCtx,
                    overlay_fn: Callable[[], dict] | None = None) -> None:
    """把 specs 那批「标签 + 滑块 + 值」按 `cols` 列铺进 `grid`（可重复调用重建）。"""
    for i, (key, label, lo, hi, res, unit) in enumerate(specs):
        row_i, col_i = divmod(i, cols)
        cell = ttk.Frame(grid)
        cell.grid(row=row_i, column=col_i, sticky="w", padx=(0, 18), pady=1)
        ttk.Label(cell, text=label, font=ui_tk.FONT_UI, width=label_w).pack(side=tk.LEFT)
        var = tk.DoubleVar(value=ctx.tune_values[key])
        val_lbl = ttk.Label(cell, text=f"{ctx.tune_values[key]:g}{unit}",
                            font=ui_tk.FONT_STATUS, foreground=TEXT_DIM, width=7)
        tk.Scale(cell, from_=lo, to=hi, resolution=res, orient=tk.HORIZONTAL,
                 variable=var, showvalue=False, length=104, width=10,
                 bg=PANEL, fg=TEXT, troughcolor=SURFACE, activebackground=ACCENT,
                 highlightthickness=0, bd=0, sliderrelief=tk.FLAT,
                 command=make_tune_handler(key, var, val_lbl, unit, ctx,
                                           overlay_fn)).pack(
                     side=tk.LEFT, padx=(4, 6))
        val_lbl.pack(side=tk.LEFT)
        ctx.tune_vars[key] = var
        ctx.tune_lbls[key] = val_lbl
        ctx.tune_units[key] = unit


def make_tune_handler(key: str, var, lbl, unit: str,  # noqa: ANN001
                      ctx: DesktopCtx,
                      overlay_fn: Callable[[], dict] | None = None) -> Callable:
    """创建滑块拖动回调闭包。"""
    def _on_move(_v: str) -> None:
        ctx.tune_values[key] = round(float(var.get()), 4)
        lbl.configure(text=f"{ctx.tune_values[key]:g}{unit}")
        # 落盘走注入的回调（防抖在 gui 侧；那里才有 Tk root 与活实例）
        if ctx.schedule_overlay_save_fn:
            ctx.schedule_overlay_save_fn()
    return _on_move


# ================================================================ 锚点

def current_anchor(ctx: DesktopCtx) -> str:
    """下拉当前选中的锚点键（right_hand / left_hand / tracker / hmd）。"""
    return ctx.anchor_label_to_key.get(ctx.anchor_combo.get(), "right_hand")


def load_anchor_offset(ctx: DesktopCtx, anchor: str, overlay_dict: dict) -> None:
    """把**该锚点那一份**位姿回填到滑块上（切锚点时必须做）。"""
    ov = overlay_dict if isinstance(overlay_dict, dict) else {}
    pos, rot = resolve_offset(ov, anchor)
    pairs = (list(zip(("pos_x", "pos_y", "pos_z"), pos))
             + list(zip(("rot_x", "rot_y", "rot_z"), rot)))
    for key, val in pairs:
        v = float(val)
        ctx.tune_values[key] = v
        ctx.tune_vars[key].set(v)
        ctx.tune_lbls[key].configure(text=f"{v:g}{ctx.tune_units[key]}")


def on_anchor_change(ctx: DesktopCtx,
                     overlay_fn: Callable[[], dict] | None = None) -> None:
    """换锚点：先把新锚点那一份位姿回填到滑块，再落盘。

    ⚠️ 顺序不能反：先落盘的话，写进去的是上一个锚点的位姿。
    """
    ov = overlay_fn() if overlay_fn is not None else {}
    load_anchor_offset(ctx, current_anchor(ctx), ov)
    save_overlay_cfg(ctx, overlay_fn=overlay_fn)


# ================================================================ overlay 配置保存

def schedule_overlay_save(ctx: DesktopCtx, root: tk.Misc | None = None, *,
                          overlay_fn: Callable[[], dict] | None = None) -> None:
    """拖动时不要每像素写盘：延后 200ms，停手才落盘。"""
    if ctx.ov_save_job is not None and root is not None:
        try:
            root.after_cancel(ctx.ov_save_job)
        except Exception:
            pass
    if root is not None:
        ctx.ov_save_job = root.after(200, lambda: save_overlay_cfg(ctx, overlay_fn=overlay_fn))


def save_overlay_cfg(ctx: DesktopCtx, cfg: Any = None, *,
                     overlay_fn: Callable[[], dict] | None = None) -> None:
    """把微调面板的值写回 config.yaml；overlay 侧有热重载，改完立刻生效。

    用就地改文本的方式，不整文件重写，保住注释和键顺序。
    位姿写到 `overlay.offsets.<当前锚点>`（每个锚点各存一套）。
    """
    ctx.ov_save_job = None
    p = _cfg_mod.DEFAULT_CONFIG
    if not p.exists():
        return
    try:
        text = p.read_text(encoding="utf-8")
        v = ctx.tune_values
        anchor = current_anchor(ctx)
        try:
            tracker = int(ctx.tracker_var.get())
        except (TypeError, ValueError):
            tracker = 0
        pos_s = (f"[{_fmt_scalar(v['pos_x'])}, {_fmt_scalar(v['pos_y'])}, "
                 f"{_fmt_scalar(v['pos_z'])}]")
        rot_s = (f"[{_fmt_scalar(v['rot_x'])}, {_fmt_scalar(v['rot_y'])}, "
                 f"{_fmt_scalar(v['rot_z'])}]")
        updates: list[tuple[list[str], str]] = [
            (["overlay", "anchor"], anchor),
            (["overlay", "tracker_index"], str(tracker)),
            (["overlay", "offset", "width_m"], _fmt_scalar(v["width_m"])),
            (["overlay", "offset", "curvature"], _fmt_scalar(v["curvature"])),
            (["overlay", "offset", "alpha"], _fmt_scalar(v["alpha"])),
            (["overlay", "font_size"], _fmt_scalar(v["font_size"])),
            (["overlay", "source_font_size"], _fmt_scalar(v["source_font_size"])),
            (["overlay", "size_px"], f"[{ctx.tune_panel_w}, {_fmt_scalar(v['panel_h'])}]"),
            (["overlay", "bg_alpha"], str(int(v["bg_alpha"]))),
            (["overlay", "source_alpha"], str(int(v["source_alpha"]))),
        ]
        for key_path, val in updates:
            text = _yaml_set_in_text(text, key_path, val)
        for key_path, val in ((["overlay", "offsets", anchor, "pos"], pos_s),
                              (["overlay", "offsets", anchor, "rot"], rot_s)):
            text = _yaml_set_or_create(text, key_path, val)
        _write_config_text(p, text)
        # 同步内存里的 cfg
        if cfg is not None:
            ov = cfg.overlay
            if isinstance(ov, dict):
                ov["anchor"] = anchor
                ov["offsets"] = {
                    **(ov.get("offsets") or {}),
                    anchor: {"pos": [v["pos_x"], v["pos_y"], v["pos_z"]],
                             "rot": [v["rot_x"], v["rot_y"], v["rot_z"]]},
                }
        print(f"[gui] 手腕屏参数已写入 config.yaml：anchor={anchor} "
              f"offsets.{anchor} pos={pos_s} rot={rot_s} "
              f"width={_fmt_scalar(v['width_m'])}m "
              f"curvature={_fmt_scalar(v['curvature'])} alpha={_fmt_scalar(v['alpha'])} "
              f"字号={_fmt_scalar(v['font_size'])}/{_fmt_scalar(v['source_font_size'])} "
              f"面板=[{ctx.tune_panel_w}, {_fmt_scalar(v['panel_h'])}] "
              f"底板/原文 alpha={int(v['bg_alpha'])}/{int(v['source_alpha'])}"
              f"（overlay 会热重载，无需重启）",
              flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[gui] 保存手腕屏参数失败：{exc}", flush=True)


# ================================================================ 桌面字幕调音台页面

def build_desktop_tune_page(parent: ttk.Frame, ctx: DesktopCtx,
                            desktop_cfg_dict: dict) -> None:
    """「设置 → 桌面字幕」页：桌面字幕自己的一套参数（与手腕屏互不影响）。"""
    from .output.desktop_overlay import DesktopOverlayConfig

    _dcfg = DesktopOverlayConfig.from_dict(desktop_cfg_dict)
    ctx.desktop_tuned = set()
    _w_num = max(_char_width_for("0000", ui_tk.FONT_UI, 8),
                 _char_width_for("000", ui_tk.FONT_UI, 8))
    _w_alpha = _char_width_for("0.00", ui_tk.FONT_UI, 5)

    grid = ttk.Frame(parent)
    grid.pack(fill=tk.X, pady=(4, 2))

    def _slider(cell, label, lo, hi, res, value, width, cmd):
        """一个「标签 + 滑块 + 值」单元；返回 (var, lbl)。"""
        ttk.Label(cell, text=label, font=ui_tk.FONT_UI).pack(side=tk.LEFT)
        var = tk.DoubleVar(value=float(value))
        lbl = ttk.Label(cell, text="", font=ui_tk.FONT_STATUS,
                        foreground=TEXT_DIM, width=width)
        tk.Scale(cell, from_=lo, to=hi, resolution=res, orient=tk.HORIZONTAL,
                 variable=var, showvalue=False, length=104, width=10,
                 bg=PANEL, fg=TEXT, troughcolor=SURFACE,
                 activebackground=ACCENT, highlightthickness=0, bd=0,
                 sliderrelief=tk.FLAT, command=cmd).pack(
                     side=tk.LEFT, padx=(4, 6))
        lbl.pack(side=tk.LEFT)
        return var, lbl

    # 第 1 行：字号（译文 / 原文）
    r1 = ttk.Frame(grid)
    r1.grid(row=0, column=0, sticky="w", padx=(0, 24), pady=1)
    c = ttk.Frame(r1)
    c.pack(side=tk.LEFT)
    ctx.desktop_font_var, ctx.desktop_font_lbl = _slider(
        c, t("译文字号"), FONT_MIN, FONT_MAX, 1, _dcfg.font_size, _w_num,
        lambda _v="": on_desktop_font(ctx))
    c2 = ttk.Frame(r1)
    c2.pack(side=tk.LEFT, padx=(16, 0))
    ctx.desktop_srcfont_var, ctx.desktop_srcfont_lbl = _slider(
        c2, t("原文字号"), SRC_FONT_MIN, FONT_MAX, 1, _dcfg.source_font_size,
        _w_num, lambda _v="": on_desktop_srcfont(ctx))

    # 第 2 行：面板尺寸（宽 / 高，像素）
    r2 = ttk.Frame(grid)
    r2.grid(row=1, column=0, sticky="w", padx=(0, 24), pady=1)
    c = ttk.Frame(r2)
    c.pack(side=tk.LEFT)
    ctx.desktop_w_var, ctx.desktop_w_lbl = _slider(
        c, t("面板宽度"), PANEL_W_MIN, PANEL_W_MAX, 10, _dcfg.size_px[0],
        _w_num, lambda _v="": on_desktop_width(ctx))
    c2 = ttk.Frame(r2)
    c2.pack(side=tk.LEFT, padx=(16, 0))
    ctx.desktop_h_var, ctx.desktop_h_lbl = _slider(
        c2, t("面板高度"), PANEL_H_MIN, PANEL_H_MAX, 10, _dcfg.size_px[1],
        _w_num, lambda _v="": on_desktop_height(ctx))

    # 第 3 行：透明度 + 拖动解锁
    r3 = ttk.Frame(grid)
    r3.grid(row=2, column=0, sticky="w", padx=(0, 24), pady=1)
    ttk.Label(r3, text=t("透明度"), font=ui_tk.FONT_UI).pack(side=tk.LEFT)
    ctx.desktop_alpha_var = tk.DoubleVar(value=float(_dcfg.alpha))
    ctx.desktop_alpha_lbl = ttk.Label(
        r3, text=f"{ctx.desktop_alpha_var.get():.2f}",
        font=ui_tk.FONT_STATUS, foreground=TEXT_DIM, width=_w_alpha)
    tk.Scale(r3, from_=0.20, to=1.00, resolution=0.05, orient=tk.HORIZONTAL,
             variable=ctx.desktop_alpha_var, showvalue=False, length=104, width=10,
             bg=PANEL, fg=TEXT, troughcolor=SURFACE, activebackground=ACCENT,
             highlightthickness=0, bd=0, sliderrelief=tk.FLAT,
             command=lambda _v="": on_desktop_alpha(ctx)).pack(
                 side=tk.LEFT, padx=(4, 6))
    ctx.desktop_alpha_lbl.pack(side=tk.LEFT)
    ctx.desktop_drag_btn = ttk.Button(
        r3, text=t("解锁拖动"),
        width=max(_char_width_for(t("解锁拖动"), ui_tk.FONT_UI, 6),
                  _char_width_for(t("锁定位置"), ui_tk.FONT_UI, 6)),
        command=lambda: (ctx.toggle_desktop_drag_fn()
                         if ctx.toggle_desktop_drag_fn else None))
    ctx.desktop_drag_btn.pack(side=tk.LEFT, padx=(16, 0))
    ttk.Label(parent, text=t("（字幕窗默认可穿透，先解锁再拖；改动即时生效）"),
              font=ui_tk.FONT_STATUS, foreground=TEXT_MUTED).pack(anchor="w", pady=(0, 2))

    # 初值回填到值标签
    for var, lbl, fmt in ((ctx.desktop_font_var, ctx.desktop_font_lbl, _int_fmt),
                          (ctx.desktop_srcfont_var, ctx.desktop_srcfont_lbl, _int_fmt),
                          (ctx.desktop_w_var, ctx.desktop_w_lbl, _int_fmt),
                          (ctx.desktop_h_var, ctx.desktop_h_lbl, _int_fmt)):
        lbl.configure(text=fmt(float(var.get())))


# ================================================================ 桌面字幕滑块回调

def apply_desktop_slider(key: str, var, lbl, fmt: Callable,  # noqa: ANN001
                         ctx: DesktopCtx,
                         desktop_out_fn: Callable[[], Any] | None = None) -> None:
    """桌面字幕滑块的公共动作：记「动过」+ 刷新值标签 + 防抖落盘。"""
    ctx.desktop_tuned.add(key)
    lbl.configure(text=fmt(float(var.get())))
    if ctx.schedule_desktop_save_fn:
        ctx.schedule_desktop_save_fn()


def on_desktop_font(ctx: DesktopCtx) -> None:
    apply_desktop_slider("font_size", ctx.desktop_font_var,
                         ctx.desktop_font_lbl, _int_fmt, ctx)


def on_desktop_srcfont(ctx: DesktopCtx) -> None:
    apply_desktop_slider("source_font_size", ctx.desktop_srcfont_var,
                         ctx.desktop_srcfont_lbl, _int_fmt, ctx)


def on_desktop_width(ctx: DesktopCtx) -> None:
    apply_desktop_slider("panel_width", ctx.desktop_w_var,
                         ctx.desktop_w_lbl, _int_fmt, ctx)


def on_desktop_height(ctx: DesktopCtx) -> None:
    apply_desktop_slider("panel_height", ctx.desktop_h_var,
                         ctx.desktop_h_lbl, _int_fmt, ctx)


def on_desktop_alpha(ctx: DesktopCtx, *,
                     desktop_out: Any = None) -> None:
    """透明度滑块：先改窗口（立刻见效），停手 300ms 再落盘（落盘实现在 gui_engine）。"""
    ctx.desktop_alpha_touched = True
    a = float(ctx.desktop_alpha_var.get())
    lbl = ctx.desktop_alpha_lbl
    if lbl is not None:
        lbl.configure(text=f"{a:.2f}")
    out = desktop_out if desktop_out is not None else (
        ctx.desktop_out_fn() if ctx.desktop_out_fn else None)
    if out is not None:
        out.set_alpha(a)
    if ctx.schedule_desktop_save_fn:
        ctx.schedule_desktop_save_fn()# ================================================================ 拖动解锁
