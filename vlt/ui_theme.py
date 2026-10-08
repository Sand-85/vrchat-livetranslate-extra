"""界面视觉常量：配色 / 字号占位 / 尺寸与布局常量。

为什么放在这里（而不是留在 gui.py）：
  · 这些是**纯数据**，不依赖任何 Tk 实例 —— 抽出来让 gui.py 只留「接线」；
  · 多处（主窗、设置弹窗、桌面字幕面板）共用同一套明度阶梯与留白口径，
    集中一处改不会漏改（历史上「同色写死两份」正是漂移的来源）。

约束：**不 import tkinter**（保持纯 Python，方便离线/无显示环境导入与测试）。
"""
from __future__ import annotations

from dataclasses import dataclass

# 界面字体族：**不能写死** "Microsoft YaHei UI"。
# 那个族在 Linux 上不存在，Tk 会静默回落到没有中日韩字形的 `fixed` ——
#   1) 中文靠逐字 fontconfig 回落渲染，实测**每次 measure() 要 0.3 秒**，
#      界面构建要把 4 种语言的控件全量一遍测量，慢到看起来像卡死
#      （tests/test_i18n.py 与 test_update_dialog.py 就是这样超时的）；
#   2) 观感也不对（字形族不一致）。
# 所以运行时按「这台机器真的有什么」挑一个（见 vlt/ui_tk.py 的 apply_ui_font）。
_FONT_CANDIDATES = (
    "Microsoft YaHei UI", "Microsoft YaHei",                  # Windows
    "Noto Sans CJK SC", "Source Han Sans CN", "Noto Sans SC",  # Linux（Noto / 思源）
    "WenQuanYi Micro Hei", "Noto Sans", "DejaVu Sans",         # 再兜一层
)


MAX_BUBBLES = 500


# ---- 统一配色：深灰 + 蓝（明度阶梯：聊天区最暗 → 面板次之 → 控件最亮） ----
BG            = "#14161c"   # 聊天区背景（最暗）
PANEL         = "#1b1e26"   # 顶栏 / 状态栏 / 窗口底色
SURFACE       = "#262a33"   # 按钮 / 下拉框 / 指示器底色（最亮一档）
SURFACE_HOVER = "#303541"   # 悬停
BORDER        = "#2e333d"   # 边框 / 分割线
ACCENT        = "#2f6fd0"   # 主色蓝（与"我说的"气泡同色）
ACCENT_HOVER  = "#3a7de0"
ACCENT_ACTIVE = "#2559a8"   # 按下
# 「反向动作」按钮（房间的「断开连接」）：红棕一档，明显区别于蓝色的「连接房间」。
# 刻意压暗、不用 COLOR_ERROR 那种亮红 —— 断开不是危险操作，只是"往回走"，
# 亮红会让人以为点了会出大事；跟着面板的明度体系走才不会在深色界面里跳出来。
DANGER        = "#a8443f"
DANGER_HOVER  = "#bf4f49"
DANGER_ACTIVE = "#8a3733"
TEXT          = "#e8eaee"   # 主文字
TEXT_DIM      = "#9aa1ad"   # 次要文字
TEXT_MUTED    = "#6f7480"   # 时间戳 / 占位
COLOR_MINE    = ACCENT      # 气泡：我说的
COLOR_THEIRS  = "#33363f"   # 气泡：别人说的
COLOR_TEXT    = "#ffffff"
COLOR_META    = TEXT_MUTED
COLOR_OK      = "#4a90d9"   # 状态栏 info
COLOR_WARN    = "#d9904a"
COLOR_ERROR   = "#e05a5a"
# 原文小字的颜色：比译文暗一档但仍清晰可读（按气泡底色分别取，保证对比度）
COLOR_SRC_MINE = "#c3d4ee"
COLOR_SRC_THEIRS = TEXT_DIM


# ---- 赞助弹窗 ----
SPONSOR_URL = "https://ko-fi.com/kcmnixi"
SPONSOR_QR_SIZE = 240          # 收款码等比缩放的目标边长（严禁拉伸：拉变形就扫不出来）

# ---- 赞助者名单（「设置 → 关于」页里展示）----
# 只是**名字**：专有名词，**不进词表、不翻译** —— 界面语言换成英/日/韩/俄时也照原样显示
# （`tests/test_i18n.py` 的语言守卫按**控件**显式排除这一行，见那里的说明）。
# 加人 = 往元组末尾追加一项（顺序即展示顺序）；留空元组 = 整区不显示（宁可没有，也不留空标题）。
SPONSORS: tuple[str, ...] = ("小夜",)


# ---- 千问云开通页（未配置 API key 时，状态按钮点击跳转）----
# 链接逐字符照抄，不做任何 URL 解码/重组。
# ⚠️ 这个常量**必须原样保留**：tests/test_api_key_gui.py 直接断言它的值，
# 并断言未配置态点按钮时 webbrowser.open() 收到的就是它。千问云线路的实际跳转
# 走下面的 `_signup_url()`（按当前线路取），在 provider=qianwen 时两者逐字符相同。
QIANWEN_SIGNUP_URL = "https://www.qianwenai.com/"


# ---- 设置弹窗（分页）----
# 宽度**固定**：每页的长说明都按 SETTINGS_WRAP 换行，于是各语言的窗宽一致，
# 不会因为俄语文案长就忽然变宽（也不再靠「窗口自然撑大 → 超出屏幕」）。
#
# ⚠️ 这里几个是 **100% 缩放（scale=1.0）的基准值**。HiDPI 下整套要按同一个
#    几何缩放因子放大（口径与字号一致：Windows 声明 Per-Monitor DPI 后 Tk 会放大字号，
#    Linux 的 Tk scaling 本来就放大字号）——见 `settings_metrics()` 与
#    `vlt/gui_settings.py: apply_settings_metrics()`。**不在 import 期钉死**（issue #61 同源）。
SETTINGS_WIDTH = 760
SETTINGS_WRAP = 660            # 长说明的换行宽 = 窗宽 - 左右留白(40) - 滚动条(~12) - 余量
SETTINGS_MIN_H = 360           # 再小的屏也至少给这么多高（内容靠页面滚动兜底）
SETTINGS_MAX_H = 900           # 上限：1080p 屏（可用高约 1040）也必须整窗看得见
# 点「停止翻译」后等引擎收尾的上限（**在后台线程里等**，绝不冻界面）。
# 实测正常路径：会话关闭 ≤1.8s + chatbox 排空 ≤2s → 单个引擎基本 2s 内收尾完。
STOP_WAIT_S = 5.0              # 单个引擎；收尾线程**逐个**等，两个引擎最坏 10s（但在后台）
CLOSE_WAIT_STOP_S = 6.0        # 关窗时**界面最多**等这么久，等不到就直接关
#                              （上面的收尾线程是 daemon，进程退出会释放麦克风/虚拟声卡）

# ---- 桌面字幕（PC 桌面模式那块屏幕叠加窗）的滑块范围 ----
# ⚠️ 与上面手腕屏的「微调」参数**各自独立**：桌面字幕是屏幕像素面板，单位/场景都不同，
#    配置不许共享（2026-10-04 维护者口径）。
FONT_MIN, FONT_MAX = 12, 96            # 译文字号（px）
SRC_FONT_MIN = 8                       # 原文字号下限（上限同 FONT_MAX）
PANEL_W_MIN, PANEL_W_MAX = 320, 2560   # 面板宽（px）
PANEL_H_MIN, PANEL_H_MAX = 120, 900    # 面板高（px）

SETTINGS_CHROME_H = 66         # tab 条 + 页面上下留白：算窗高时在内容高度上加这一份
# 每页内容 frame 的左右内边距（内容区位置固定，不随标签条动）
TAB_INSET_X = 20


@dataclass(frozen=True)
class SettingsMetrics:
    """设置弹窗在某个缩放档下的有效尺寸（单位 px）。"""
    width: int
    wrap: int
    min_h: int
    max_h: int
    chrome_h: int


def settings_metrics(scale: float = 1.0) -> SettingsMetrics:
    """按几何缩放因子把设置弹窗的基准尺寸换算成有效值。

    `scale` 由 `vlt/gui_layout` 的 `_resolve_scale()`（内部走 `_dpi_scale()` 或
    `ui.scale` 覆盖）给出，范围 [1, 3]。这里再夹一道，防止别处传进越界值。
    **纯函数**（不碰 Tk），便于离线测试。
    """
    s = max(1.0, min(3.0, float(scale)))
    return SettingsMetrics(
        width=int(round(SETTINGS_WIDTH * s)),
        wrap=int(round(SETTINGS_WRAP * s)),
        min_h=int(round(SETTINGS_MIN_H * s)),
        max_h=int(round(SETTINGS_MAX_H * s)),
        chrome_h=int(round(SETTINGS_CHROME_H * s)),
    )
