"""需要 tkinter 的界面底层工具：字体族解析、字符宽度换算、控件读取、圆角矩形。

为什么单列一个模块（且是全仓库**唯一**给界面工具 import tkinter 的新模块）：
  · 字体族解析 / `width=` 字符宽度换算都要真起 Tk 才能 measure；
  · `combo_values` / `round_rect` 直接操作控件/画布。
  它们都不属于「纯数据」（那些在 vlt/ui_theme.py）或「纯字符串」（vlt/ui_text.py），
  所以按依赖边界单独成模块 —— 纯模块绝不 import tkinter，本模块才 import。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from .ui_theme import (
    ACCENT,
    ACCENT_ACTIVE,
    ACCENT_HOVER,
    BG,
    BORDER,
    COLOR_ERROR,
    COLOR_WARN,
    DANGER,
    DANGER_ACTIVE,
    DANGER_HOVER,
    PANEL,
    SURFACE,
    SURFACE_HOVER,
    TEXT,
    TEXT_DIM,
    TEXT_MUTED,
)
from .ui_theme import _FONT_CANDIDATES

# 下面这组是**占位**默认值，`apply_ui_font()` 会在建 Tk root 之后按平台重绑。
FONT = ("Microsoft YaHei UI", 11)          # 译文（主）
FONT_SMALL = ("Microsoft YaHei UI", 9)     # 原文（辅，小一号）
FONT_META = ("Microsoft YaHei UI", 8)
FONT_UI = ("Microsoft YaHei UI", 9)        # 控件文字
FONT_STATUS = ("Microsoft YaHei UI", 8)    # 状态栏
FONT_BOLD_SM = ("Microsoft YaHei UI", 8, "bold")    # 分区小标题
FONT_BOLD_MD = ("Microsoft YaHei UI", 12, "bold")   # 弹窗小标题
FONT_BOLD_LG = ("Microsoft YaHei UI", 13, "bold")   # 弹窗大标题（赞助）

# 解析出的字体族缓存（resolve_ui_family 首次调用后钉住；None = 还没解析过）。
_ui_family: str | None = None


def resolve_ui_family(root) -> str:
    """挑一个这台机器上**真实存在**的界面字体族。

    先按候选表找；都没有就退回 Tk 自己的默认字体族（`TkDefaultFont` 的 actual family），
    保证至少是一个有字形的真字体，而不是 `fixed`。
    """
    global _ui_family
    if _ui_family:
        return _ui_family
    try:
        available = {str(f).strip().lower() for f in tkfont.families(root)}
    except Exception:  # noqa: BLE001 — 拿不到列表就退回默认
        available = set()
    for cand in _FONT_CANDIDATES:
        if cand.lower() in available:
            _ui_family = cand
            return cand
    try:
        _ui_family = str(tkfont.nametofont("TkDefaultFont").actual("family"))
    except Exception:  # noqa: BLE001
        _ui_family = "sans-serif"
    return _ui_family


def apply_ui_font(root) -> None:
    """按当前平台重绑界面字体常量（在建 Tk root 之后、建任何控件之前调用）。"""
    global FONT, FONT_SMALL, FONT_META, FONT_UI, FONT_STATUS
    global FONT_BOLD_SM, FONT_BOLD_MD, FONT_BOLD_LG
    fam = resolve_ui_family(root)
    FONT = (fam, 11)
    FONT_SMALL = (fam, 9)
    FONT_META = (fam, 8)
    FONT_UI = (fam, 9)
    FONT_STATUS = (fam, 8)
    FONT_BOLD_SM = (fam, 8, "bold")
    FONT_BOLD_MD = (fam, 12, "bold")
    FONT_BOLD_LG = (fam, 13, "bold")


_char_width_cache: dict[tuple, int] = {}


def _font_spec_key(font_spec) -> object:
    """把字体规格压成可哈希的缓存 key（跨平台容错）。

    `font_spec` 可能是 tuple、字符串（字体名）、`tkfont.Font`，或 —— 在 Linux 上 ——
    `widget.cget("font")` 返回的 `_tkinter.Tcl_Obj`（不可迭代，直接 `tuple()`
    会抛 `TypeError: '_tkinter.Tcl_Obj' object is not iterable`）。
    """
    if isinstance(font_spec, str):
        return font_spec
    try:
        return tuple(font_spec)
    except TypeError:
        return str(font_spec)


def _int_fmt(v: float) -> str:
    """桌面字幕滑块值标签的统一格式（字号 / 像素尺寸都是整数）。"""
    return f"{int(round(v))}"


def _char_width_for(text: str, font_spec, minimum: int = 0) -> int:
    """把「这段文字需要多宽」换算成 Tk 的**字符宽度单位**（给 width= 用）。

    ⚠️ 坑（实测）：Tk 的 `width=N` 单位是**字体平均字符宽**（本机 ≈7px），而一个汉字/假名
    约等于 2 倍宽 ⇒ `width=len(text)` 对中日韩文字**必然裁字**：
    「訳文の文字サイズ」自然宽 100px，按 8 个字符只申请到 67px，屏幕上只剩「訳文の文字」。
    中文同样中招（「译文字号」需 52px、按 6 字符只给 46px），只是裁得少不容易看出来。
    这里用字体的真实 measure 换算，并留 1 个字符余量。

    结果**带缓存**：切界面语言会把整套控件重建一遍，同一个词条会被反复测量；
    而一次 `measure()` 在字体需要 fontconfig 回落的机器上要几百毫秒
    （见上方 _FONT_CANDIDATES 的说明），不缓存会明显卡顿。
    """
    key = (text, _font_spec_key(font_spec), minimum)
    hit = _char_width_cache.get(key)
    if hit is not None:
        return hit
    try:
        f = tkfont.Font(font=font_spec)
        avg = max(1, f.measure("0"))
        need = -(-f.measure(text) // avg) + 1     # 向上取整 + 1 字符余量
    except Exception:  # noqa: BLE001 — 量不出来就退回字符数（至少不比改动前差）
        need = len(text) + 1
    out = max(minimum, need)
    _char_width_cache[key] = out
    return out


def _combo_width(names, minimum: int = 9, font_spec=None) -> int:
    """下拉框宽度（字符单位）：按当前语言里最长的名字算，避免被截断。

    同样受「平均字符宽 ≠ 汉字宽」影响，所以走 `_char_width_for` 换算，不直接数字符。
    """
    f = font_spec or FONT_UI
    return max(minimum, max((_char_width_for(str(n), f) for n in names), default=0))


def combo_values(combo) -> list[str]:
    """读回 ttk.Combobox 的候选值（跨平台安全）。

    ⚠️ 坑（实测）：`combo.cget("values")` 的**返回类型依平台而变** ——
    Windows 上 Tk 返回 tuple（可直接 `list()`），Linux 上返回
    `_tkinter.Tcl_Obj`（不可迭代，`list()` 直接抛
    `TypeError: '_tkinter.Tcl_Obj' object is not iterable`）。
    这在设备下拉里是**真会走到**的路径（`_on_device_change` 要按下标取回原始设备名），
    不是只影响测试。

    用 Tk 自己的 `splitlist` 归一化：tuple / 列表 / 空格分隔的字符串 / Tcl_Obj 都能吃。
    """
    try:
        return [str(v) for v in combo.tk.splitlist(combo.cget("values"))]
    except Exception:  # noqa: BLE001 — 读不到就当空，别让「保存设备选择」这一步炸掉
        return []


def round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """圆角矩形：polygon + smooth=True 才有圆角。"""
    pts = [x1+r, y1, x2-r, y1, x2, y1, x2, y1+r, x2, y2-r, x2, y2,
           x2-r, y2, x1+r, y2, x1, y2, x1, y2-r, x1, y1+r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


def apply_theme(root) -> None:
        """统一深色主题：深灰 + 蓝。

        ⚠️ Windows 上 ttk 默认主题（vista/xpnative）由系统绘制，
        style.configure(background=...) 会被**静默忽略**——必须切到 clam。
        """
        style = ttk.Style(root)
        style.theme_use("clam")

        style.configure(".", font=FONT_UI, background=PANEL, foreground=TEXT,
                        bordercolor=BORDER, focuscolor=PANEL)
        style.configure("TFrame", background=PANEL)
        style.configure("TLabel", background=PANEL, foreground=TEXT)
        style.configure("Dim.TLabel", foreground=TEXT_DIM)
        style.configure("Muted.TLabel", foreground=TEXT_DIM, font=FONT_STATUS)
        style.configure("Status.TLabel", font=FONT_STATUS)
        # 分区小标题（设置弹窗里的「API KEY / 音频设备」）：小一号、暗色、加粗
        style.configure("Section.TLabel", foreground=TEXT_DIM,
                        font=FONT_BOLD_SM)
        # API key 状态槽位里的两个控件：**已配置 → 纯展示标签**（「⚙ 设置」是改 key 的入口，
        # 标签不可点）；**未配置 → 可点按钮**，点击用默认浏览器打开千问云开通页（QIANWEN_SIGNUP_URL）。
        style.configure("Chip.TLabel", font=FONT_STATUS, foreground=TEXT_DIM)
        style.configure("ChipWarn.TLabel", font=FONT_STATUS, foreground=COLOR_WARN)
        # 设置弹窗里「保存失败」这类就地提示：警示色，但只是文字（不抢按钮的视觉重量）
        style.configure("Warn.TLabel", font=FONT_STATUS, foreground=COLOR_WARN)
        # 「保存被拒 · 什么都没写」的就地红字：警示橙只表达"注意一下"，而这种情况下
        # 配置**一点没变**（用户以为切了线路、其实还在老线路上）—— 必须比橙更重一档。
        style.configure("Error.TLabel", font=FONT_STATUS, foreground=COLOR_ERROR)
        # 未配置按钮：暗橙底 + 警示橙字，悬停/按下亮一档 —— 警示色系但不刺眼。
        style.configure("ChipWarn.TButton", font=FONT_STATUS, foreground=COLOR_WARN,
                        background="#33291c", borderwidth=0, focusthickness=0,
                        focuscolor="#33291c", padding=(8, 2))
        style.map("ChipWarn.TButton",
                  background=[("pressed", "#453723"), ("active", "#453723")],
                  foreground=[("active", "#e8a85c")])
        # 分割线/分组竖线：用 1px 明度差表达层次，不用 3D 边框
        style.configure("TSeparator", background=BORDER)

        # 按钮：扁平、无边框（clam 的按钮边框会带亮色 bevel，直接不要边框），
        # 悬停/按下有反馈；focuscolor 设成与背景同色，去掉点状焦点框
        style.configure("TButton", background=SURFACE, foreground=TEXT,
                        borderwidth=0, focusthickness=0, focuscolor=PANEL,
                        padding=(12, 7))
        style.map("TButton",
                  background=[("pressed", SURFACE_HOVER), ("active", SURFACE_HOVER),
                              ("disabled", "#20242d")],
                  foreground=[("disabled", TEXT_MUTED)])
        # 主按钮（开始翻译）：蓝色强调
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        borderwidth=0, focusthickness=0, focuscolor=ACCENT,
                        padding=(14, 6))
        style.map("Accent.TButton",
                  background=[("pressed", ACCENT_ACTIVE), ("active", ACCENT_HOVER),
                              ("disabled", "#22374f")],
                  foreground=[("disabled", "#6b87ab")])
        # 反向动作按钮（房间的「断开连接」）：与「连接房间」同形状、**不同颜色** ——
        # 同一个位置在不同连接态下写着相反的动作，只靠文字区分容易点错，
        # 颜色是比文字快得多的提示（用户明确要求「断开用个别的颜色」）。
        style.configure("Danger.TButton", background=DANGER, foreground="#ffffff",
                        borderwidth=0, focusthickness=0, focuscolor=DANGER,
                        padding=(14, 6))
        style.map("Danger.TButton",
                  background=[("pressed", DANGER_ACTIVE), ("active", DANGER_HOVER),
                              ("disabled", "#3a2726")],
                  foreground=[("disabled", "#9c7a78")])

        # API key 输入行：不加这条会沿用 clam 的浅色默认底 —— 深色界面里出现一块白，很扎眼
        # （截图复核时发现的）。字段底/文字/插入符/边框全部对齐 SURFACE/TEXT/BORDER 体系。
        style.configure("Key.TEntry", fieldbackground=SURFACE, background=SURFACE,
                        foreground=TEXT, insertcolor=TEXT, bordercolor=BORDER,
                        lightcolor=SURFACE, darkcolor=SURFACE, padding=(8, 4))
        style.map("Key.TEntry",
                  bordercolor=[("focus", ACCENT), ("active", SURFACE_HOVER)],
                  fieldbackground=[("disabled", BG), ("readonly", SURFACE)],
                  foreground=[("disabled", TEXT_DIM)])

        # 下拉框：字段、箭头、边框都变深；readonly 下保持深色
        style.configure("TCombobox", fieldbackground=SURFACE, background=SURFACE,
                        foreground=TEXT, arrowcolor=TEXT_DIM, bordercolor=BORDER,
                        lightcolor=SURFACE, darkcolor=SURFACE, insertcolor=TEXT,
                        padding=(8, 4))
        style.map("TCombobox",
                  fieldbackground=[("readonly", SURFACE)],
                  foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", SURFACE)],   # 去掉选中文字的高亮白块
                  selectforeground=[("readonly", TEXT)],
                  bordercolor=[("focus", ACCENT), ("active", SURFACE_HOVER)],
                  arrowcolor=[("active", TEXT)])
        # 下拉弹出的列表是独立 Listbox，必须单独配色（否则弹出来是白的）
        root.option_add("*TCombobox*Listbox.background", SURFACE)
        root.option_add("*TCombobox*Listbox.foreground", TEXT)
        root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        root.option_add("*TCombobox*Listbox.font", FONT_UI)

        # 滚动条：细、暗、无箭头，跟聊天区融合
        style.layout("Vertical.TScrollbar",
                     [("Vertical.Scrollbar.trough",
                       {"children": [("Vertical.Scrollbar.thumb",
                                      {"expand": "1", "sticky": "nswe"})],
                        "sticky": "ns"})])
        style.configure("Vertical.TScrollbar", background=SURFACE, troughcolor=BG,
                        bordercolor=BG, darkcolor=BG, lightcolor=BG,
                        arrowcolor=TEXT_DIM, gripcount=0)
        style.map("Vertical.TScrollbar",
                  background=[("pressed", ACCENT_ACTIVE), ("active", SURFACE_HOVER)])

        # 设置弹窗的分页标签（Notebook）：clam 的默认 tab 是浅灰渐变，
        # 深色界面里就是一块亮斑（和「ttk.Entry 默认白底」同一类坑），必须逐状态配色。
        # tabmargins 左侧必须是 **0**：标签条的基准是 Notebook **外框**左边 ——
        # 也就是内容区那条左边框竖线（贯穿整窗、也是用户会拿来对的那条线）。
        # ⚠️ 别把它对齐到「页内分隔线的左端」：那条线本身被页面 20px 内边距缩进过，
        #    拿它当基准会让整排标签比内容区左边框右缩 22px（截图实测过，正是用户说的「没对齐」）。
        style.configure("TNotebook", background=PANEL, bordercolor=BORDER,
                        darkcolor=PANEL, lightcolor=PANEL, tabmargins=(0, 6, 10, 0))
        tab_pad = (16, 7)
        style.configure("TNotebook.Tab", font=FONT_UI, padding=tab_pad,
                        background=PANEL, foreground=TEXT_DIM, bordercolor=BORDER,
                        lightcolor=PANEL, darkcolor=PANEL, focuscolor=PANEL)
        # padding 必须逐状态映射成同一个值：只写 configure 的默认值时，selected/active
        # 会回落成 clam 自己的 tab 布局尺寸，选中标签的外框就比未选中的矮一截。
        # lightcolor/darkcolor/bordercolor 同理——任一状态回落成空值都会画出亮边。
        # 于是选中态**只靠背景色 + 前景色**区分，三态尺寸完全一致。
        # 注意两点实测坑：① ttk 取「第一个匹配的状态规格」，所以默认态必须排在最后；
        # ② 默认态**不能**写成 ("", …)——Tk 8.6 里空规格匹配任意状态，会把 selected 盖掉。
        style.map("TNotebook.Tab",
                  padding=[("selected", tab_pad), ("active", tab_pad),
                           ("!selected !active", tab_pad)],
                  background=[("selected", SURFACE), ("active", SURFACE_HOVER),
                              ("!selected !active", PANEL)],
                  foreground=[("selected", TEXT), ("active", TEXT),
                              ("!selected !active", TEXT_DIM)],
                  lightcolor=[("selected", SURFACE), ("active", SURFACE_HOVER),
                              ("!selected !active", PANEL)],
                  darkcolor=[("selected", SURFACE), ("active", SURFACE_HOVER),
                             ("!selected !active", PANEL)],
                  bordercolor=[("selected", BORDER), ("active", BORDER),
                               ("!selected !active", BORDER)])
