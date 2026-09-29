"""手腕 Overlay 输出：把译文渲染成贴图，挂到手腕/前臂 tracker/HMD 前的 SteamVR overlay 上。

设计要点（见 notes/VRChat实时同传-架构设计-端到端路线.md §4.4）：
- 纯 Python（pyopenvr + Pillow），不需要 Unity / XSOverlay / OVR Toolkit
- **中文/日文用 Pillow 画成图**，绕开 chatbox 与 avatar 头顶字的字符集限制
- 锚点：左手 / 右手 / 前臂 VIVE Ultimate Tracker / HMD 前固定偏移；位置·旋转·尺寸·透明度可配 + 热重载
- SteamVR 没跑时**优雅降级**（禁用 overlay，不阻塞其他输出）
- 离线可验证：`--demo` 只渲染 PNG，不碰 SteamVR
"""
from __future__ import annotations

import ctypes
import math
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 调试帧是"跑完要看"的产物 → 可写目录（exe 旁），不是临时解包目录
from ..paths import APP_DIR as ROOT


# ---------------------------------------------------------------- 配置
def _as_float_tuple(raw, default: tuple[float, ...]) -> tuple[float, ...]:  # noqa: ANN001
    """配置里的数字列表 → float 元组；空 / 脏值 / 非正数整组回落到默认（绝不抛异常）。"""
    try:
        vals = tuple(float(x) for x in raw)
    except Exception:  # noqa: BLE001
        return default
    if not vals or any(v <= 0 for v in vals):
        return default
    return vals


def _as_str_tuple(raw, default: tuple[str, ...]) -> tuple[str, ...]:  # noqa: ANN001
    """配置里的字符串列表 → str 元组；脏值回落默认。

    只写一个字符串时按「一个元素」处理（`split_content: mine` 等价于 `[mine]`），
    否则会被逐字符拆成 ('m','i','n','e')，栏内容筛选就全空 —— 实测容易踩。
    """
    if raw is None:
        return default
    if isinstance(raw, str):
        return (raw,)
    try:
        return tuple(str(x) for x in raw)
    except Exception:  # noqa: BLE001
        return default


@dataclass
class OverlayConfig:
    enabled: bool = True
    anchor: str = "right_hand"          # left_hand | right_hand | tracker | hmd
    tracker_index: int = 0              # anchor=tracker 时用第几个 tracker
    pos: tuple[float, float, float] = (0.0, 0.06, 0.02)      # 相对锚点，米
    rot: tuple[float, float, float] = (-47.0, -16.0, 0.0)    # 欧拉角，度
    width_m: float = 0.23
    curvature: float = 0.0
    alpha: float = 0.9
    size_px: tuple[int, int] = (1024, 440)
    font: str = "C:/Windows/Fonts/msyh.ttc"
    font_size: int = 36                      # 译文字号（面板 1024x440 时 36~44 都清晰）
    source_font_size: int = 29               # 原文小字号
    # 配色：统一白色系，层级只靠字号 + 透明度区分（避免出现"蓝原文 + 白译文"这种像残留色的观感）
    color_translation: tuple[int, int, int] = (255, 255, 255)
    color_source: tuple[int, int, int] = (255, 255, 255)
    source_alpha: int = 205                  # 原文透明度（0-255）；150 在白底上会显灰像另一个颜色，取 205 更"同色系"
    color_border: tuple[int, int, int] = (110, 170, 205)
    border_alpha: int = 120
    color_bg: tuple[int, int, int] = (12, 14, 20)
    bg_alpha: int = 205
    # 对话视图：我说的 / 别人说的 外缘竖条（与 GUI 气泡同色系）
    color_mine: tuple[int, int, int] = (47, 111, 208)
    color_theirs: tuple[int, int, int] = (110, 116, 128)
    separator: bool = True                   # 原文与译文之间的细分隔线（同色系，低透明度）
    max_lines: int = 3
    show_source: bool = True
    fade_after_s: float = 0.0           # >0 则最后一条文本显示 fade_after_s 秒后淡出
    overlay_key: str = "vlt.wrist.panel"
    # ---- 分栏（方案 A：一块面板里并排两个「窗口」，各自宽度可自定义）----
    # split=true → 走 render_split()：仍是**同一块 overlay、同一张贴图**，只在画面里分区。
    split: bool = False
    # 每栏宽度：全 ≤1 视为**比例**（[0.3, 0.7]）；有 >1 的视为**像素/份数**（[300, 700]）——
    # 两种写法都行，内部一律按权重归一化到面板内宽，所以多一栏少一栏都能用。
    split_panes: tuple[float, ...] = (0.5, 0.5)
    split_gap_px: int = 10                           # 栏间空隙（像素）
    split_divider: bool = True                       # 栏间竖分隔线
    split_content: tuple[str, ...] = ("theirs", "mine")   # 每栏放谁的内容：mine / theirs / peer / all
    split_labels: tuple[str, ...] = ("", "")         # 每栏顶部小标题（空 = 不画）

    @staticmethod
    def from_dict(d: dict) -> "OverlayConfig":
        d = d or {}
        off = d.get("offset") or {}
        return OverlayConfig(
            enabled=bool(d.get("enabled", True)),
            anchor=d.get("anchor", "right_hand"),
            tracker_index=int(d.get("tracker_index", 0)),
            pos=tuple(off.get("pos", (0.0, 0.06, 0.02))),          # type: ignore[arg-type]
            rot=tuple(off.get("rot", (-47.0, -16.0, 0.0))),         # type: ignore[arg-type]
            width_m=float(off.get("width_m", 0.23)),
            curvature=float(off.get("curvature", 0.0)),
            alpha=float(off.get("alpha", d.get("alpha", 0.9))),
            size_px=tuple(d.get("size_px", (1024, 440))),          # type: ignore[arg-type]
            font=d.get("font", "C:/Windows/Fonts/msyh.ttc"),
            font_size=int(d.get("font_size", 36)),
            source_font_size=int(d.get("source_font_size", 29)),
            color_translation=tuple(d.get("color_translation", (255, 255, 255))),   # type: ignore[arg-type]
            color_source=tuple(d.get("color_source", (255, 255, 255))),             # type: ignore[arg-type]
            source_alpha=int(d.get("source_alpha", 205)),
            color_border=tuple(d.get("color_border", (110, 170, 205))),             # type: ignore[arg-type]
            border_alpha=int(d.get("border_alpha", 120)),
            color_bg=tuple(d.get("color_bg", (12, 14, 20))),                        # type: ignore[arg-type]
            bg_alpha=int(d.get("bg_alpha", 205)),
            separator=bool(d.get("separator", True)),
            max_lines=int(d.get("max_lines", 3)),
            show_source=bool(d.get("show_source", True)),
            fade_after_s=float(d.get("fade_after_s", 0.0)),
            overlay_key=d.get("overlay_key", "vlt.wrist.panel"),
            split=bool(d.get("split", False)),
            split_panes=_as_float_tuple(d.get("split_panes"), (0.5, 0.5)),
            split_gap_px=int(d.get("split_gap_px", 10)),
            split_divider=bool(d.get("split_divider", True)),
            split_content=_as_str_tuple(d.get("split_content"), ("theirs", "mine")),
            split_labels=_as_str_tuple(d.get("split_labels"), ("", "")),
        )


# ---------------------------------------------------------------- 渲染（离线可测）
# 中文禁则处理：这些标点不允许出现在行首（行尾溢出一点也比行首标点好读）
CLOSING_PUNCT = "。，、；：！？）」』】》〉·…—.,;:!?)]}\"'"
OPENING_PUNCT = "（「『【《〈([{"


# 用空格分词的字母文字（拉丁/西里尔/希腊…）算「整词」；CJK 逐字切，
# 韩文用空格分词所以也算整词。见 _is_word_char。
_CJK_RANGES = ((0x3000, 0x303F),      # CJK 标点
               (0x3040, 0x30FF),      # 平假名 / 片假名
               (0x3400, 0x4DBF),      # 扩展 A
               (0x4E00, 0x9FFF),      # 统一表意
               (0xF900, 0xFAFF),      # 兼容表意
               (0x20000, 0x2FA1F))    # 扩展 B 及以上


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _CJK_RANGES)


def _is_word_char(ch: str) -> bool:
    """该字符算「同一个词内」吗（整词保护用，别把单词从中间劈开）。"""
    if _is_cjk(ch):
        return False                       # 中日文没有空格 → 逐字断行
    if ch.isascii():
        return ch.isalnum() or ch in "-_'/"
    return ch.isalnum()                    # 西里尔 П、希腊 ω、带音标拉丁 é


def _tokens(text: str) -> list[str]:
    """切分：整词为一个 token（拉丁/西里尔/希腊/韩文…），CJK 逐字，标点单独成 token。

    纯按字符切会把英文单词劈开（实测出现 transla/ted、y/our），
    纯按词切又对中文无效（中文没有空格）——所以必须混合切。

    ⚠️ 原实现只把 **ASCII** 字母数字当词内字符，于是所有非 ASCII 的拼音文字
    （俄语、希腊语、带音标的拉丁语…）都退化成逐字切、单词被从中间劈开
    （实测俄语 `строк` → `стро` + `к`，看着像排版坏了）。
    """
    tokens: list[str] = []
    buf = ""
    for ch in text:
        if _is_word_char(ch):
            buf += ch
        else:
            if buf:
                tokens.append(buf)
                buf = ""
            tokens.append(ch)
    if buf:
        tokens.append(buf)
    return tokens


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """按像素宽度折行，带拉丁整词保护 + 中文避头尾（行首禁则标点）。"""
    lines: list[str] = []
    cur = ""
    for tk in _tokens(text):
        if tk == "\n":
            lines.append(cur)
            cur = ""
            continue
        if tk == " " and not cur:          # 行首不留空格
            continue
        if draw.textlength(cur + tk, font=font) <= max_w:
            cur += tk
            continue
        # 放不下：若该 token 是禁则标点 → 悬挂在本行末尾（避免行首标点）
        if tk in CLOSING_PUNCT and cur:
            cur += tk
            lines.append(cur)
            cur = ""
            continue
        if cur:
            lines.append(cur.rstrip())
        if tk == " ":                      # 空格放不下 = 就在这里断行
            cur = ""
            continue
        # 单个整词就比整行还宽（超长单词 / 一长串无空格字符）→ 按字符硬切，
        # 否则整词独占一行会直接溢出面板边缘（比断词更难看）。
        while len(tk) > 1 and draw.textlength(tk, font=font) > max_w:
            cut = 1
            while cut < len(tk) and draw.textlength(tk[:cut + 1], font=font) <= max_w:
                cut += 1
            lines.append(tk[:cut])
            tk = tk[cut:]
        cur = tk
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def render_panel(text: str, source: str = "", cfg: OverlayConfig | None = None) -> Image.Image:
    """把（原文, 译文）渲染成 RGBA 面板。纯函数，便于离线预览与单测。"""
    cfg = cfg or OverlayConfig()
    w, h = cfg.size_px
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角半透明底板
    pad = 12
    d.rounded_rectangle([pad, pad, w - pad, h - pad], radius=28,
                        fill=(*cfg.color_bg, cfg.bg_alpha),
                        outline=(*cfg.color_border, cfg.border_alpha), width=3)

    def _font(size: int) -> ImageFont.FreeTypeFont:
        try:
            return ImageFont.truetype(cfg.font, size)
        except Exception:
            return ImageFont.load_default()

    inner_w = w - 4 * pad
    y = pad * 2

    # 原文（小字、灰蓝）—— 3.8 默认就会返回源语言识别结果，双行显示零额外成本
    if cfg.show_source and source:
        sf = _font(cfg.source_font_size)
        src_lines = wrap_text(d, source, sf, inner_w)[-2:]
        for ln in src_lines:
            d.text((pad * 2, y), ln, font=sf, fill=(*cfg.color_source, cfg.source_alpha))
            y += cfg.source_font_size + 8
        y += 6
        if cfg.separator:                    # 同色系细分隔线，替代"靠换色分层"的做法
            d.line([(pad * 2, y), (w - pad * 2, y)],
                   fill=(*cfg.color_source, max(60, cfg.source_alpha // 2)), width=2)
            y += 12

    # 译文（大字、白）
    tf = _font(cfg.font_size)
    text_lines = wrap_text(d, text, tf, inner_w)
    if len(text_lines) > cfg.max_lines:      # 同传场景保留最新内容
        text_lines = text_lines[-cfg.max_lines:]
    for ln in text_lines:
        d.text((pad * 2, y), ln, font=tf, fill=(*cfg.color_translation, 255))
        y += cfg.font_size + 10
    return img


# 多人房间的固定调色板：按 peer_id 稳定哈希选色，同一个人任何时候都是同一种颜色。
# 颜色都取得比较饱和、彼此区分度高，且都明显不同于底板色(12,14,20)、
# mine 蓝(47,111,208) 与 theirs 灰(110,116,128)，方便「谁在说」一眼分辨、也方便整列扫描断言。
PEER_PALETTE: tuple[tuple[int, int, int], ...] = (
    (224, 122, 91),    # 砖红
    (96, 165, 250),    # 蓝
    (74, 158, 116),    # 绿
    (240, 173, 78),    # 琥珀
    (167, 139, 250),   # 紫
    (45, 152, 168),    # 青
    (236, 72, 153),    # 洋红
    (132, 169, 87),    # 橄榄
    (250, 204, 21),    # 黄
    (56, 189, 248),    # 天蓝
)


def peer_color(peer_id: str) -> tuple[int, int, int]:
    """按 `peer_id` 稳定哈希到调色板里的一种颜色。

    ⚠️ 必须用 `zlib.crc32` 这类**确定性**哈希，绝不能用内置 `hash()` —— 后者每个进程
    随机化（PYTHONHASHSEED），重连/重启一次同一个人就换了颜色，正是 BRIEF 明令禁止的。
    也绝不按出现顺序分配：顺序会随谁先说话而变。
    """
    pid = (peer_id or "").strip()
    if not pid:
        return PEER_PALETTE[0]
    return PEER_PALETTE[zlib.crc32(pid.encode("utf-8")) % len(PEER_PALETTE)]


def _peer_id_of(who) -> str | None:  # noqa: ANN001
    """`who == "peer:<id>"` → 返回 `<id>`；其它形态（mine/theirs）返回 None。"""
    if isinstance(who, str) and who.startswith("peer:"):
        return who[len("peer:"):]
    return None


def _unpack_entry(raw) -> tuple[str, str, str, str]:  # noqa: ANN001
    """把一条 entry 拆成 `(who, source, text, label)`，兼容旧的 3 元组（label 视作空）。

    3 元组是现有形态，必须继续支持；4 元组多出来的第 4 项是说话人昵称（小字）。
    """
    if isinstance(raw, (list, tuple)):
        if len(raw) >= 4:
            return raw[0], raw[1], raw[2], raw[3]
        if len(raw) == 3:
            return raw[0], raw[1], raw[2], ""
    return "", "", "", ""


def _draw_entries(d, entries, cfg: OverlayConfig, *, x0: int, x1: int, y_bottom: int,
                  budget: int, tf, sf) -> int:  # noqa: ANN001
    """在一块矩形区域里**从下往上**排「最近几句对话」，返回实际占用高度。

    整块面板（`render_conversation`）与分栏里的每一栏（`render_split`）共用这一段 ——
    两处必须逐像素一致，否则同一份内容在开/关分栏时会跳。

    x0 / x1  : 可绘制区左右边界（同时也是「我说的」右缘竖条 / 「别人说的」左缘竖条的位置）
    y_bottom : 内容底部（= 面板高 - 2*pad，与整块面板一致）
    budget   : 这块区域能用的总高度（分栏时已扣掉栏标题的高度）
    """
    inner_w = x1 - x0
    asc_t = cfg.font_size + 10
    asc_s = cfg.source_font_size + 6

    shown: list[tuple[str, list[str], list[str], int]] = []
    used = 0
    for raw in reversed(list(entries or [])):
        who, source, text, label = _unpack_entry(raw)
        t = (text or "").strip()
        if not t:
            continue
        tl = wrap_text(d, t, tf, inner_w - 24)
        # 小字区：昵称（标明谁在说，始终画）+ 原文（受 show_source 控制）。
        # mine/theirs 没有 label → 这一段与改版前逐像素一致。
        sl: list[str] = []
        lab = (label or "").strip()
        if lab:
            sl += wrap_text(d, lab, sf, inner_w - 24)
        src = (source or "").strip()
        if cfg.show_source and src:
            sl += wrap_text(d, src, sf, inner_w - 24)
        blk_h = len(sl) * asc_s + (4 if sl else 0) + len(tl) * asc_t + 14
        if shown and used + blk_h > budget:      # 塞不下更早的就停（保留最新）
            break
        shown.append((who, sl, tl, blk_h))
        used += blk_h
    shown.reverse()                              # 最新的在最下面，和聊天区一致

    y = y_bottom - used
    for who, sl, tl, blk_h in shown:
        pid = _peer_id_of(who)
        if who == "mine":                        # 外缘竖条 + 右对齐
            d.rounded_rectangle([x1 - 5, y + 2, x1, y + blk_h - 8],
                                radius=2, fill=(*cfg.color_mine, 230))
            tx, anchor = x1 - 16, "ra"
        else:                                    # theirs / peer:<id> 都靠左
            col = peer_color(pid) if pid is not None else cfg.color_theirs
            d.rounded_rectangle([x0, y + 2, x0 + 5, y + blk_h - 8],
                                radius=2, fill=(*col, 230))
            tx, anchor = x0 + 16, "la"
        yy = y
        for ln in sl:                            # 昵称 / 原文小字在上
            d.text((tx, yy), ln, font=sf, fill=(*cfg.color_source, cfg.source_alpha), anchor=anchor)
            yy += asc_s
        if sl:
            yy += 4
        for ln in tl:                            # 译文（房间里=源文）大字在下
            d.text((tx, yy), ln, font=tf, fill=(*cfg.color_translation, 255), anchor=anchor)
            yy += asc_t
        y += blk_h
    return used


def _pane_boxes(x0: int, x1: int, panes, gap: int) -> list[tuple[int, int]]:  # noqa: ANN001
    """把 [x0, x1] 按配置切成 N 栏，返回每栏的 (左, 右)。

    宽度两种写法都吃：**全 ≤1 视为比例**（[0.3, 0.7]），**有 >1 的视为像素/份数**（[300, 700]）——
    内部一律按权重归一化到「内宽 − 空隙」，所以改栏数不用改配置的语义。
    """
    weights = [float(p) for p in (panes or (1.0,))]
    n = max(1, len(weights))
    gap = max(0, int(gap))
    if x1 <= x0:                                 # 面板比留白还窄：退化成一个 1px 宽的区
        return [(x0, x0 + 1)]
    total = (x1 - x0) - gap * (n - 1)
    if total <= n:                               # 面板太窄：退化成不切分，别算出负宽度
        return [(x0, x1)]
    s = sum(weights) or float(n)
    boxes: list[tuple[int, int]] = []
    x = x0
    for i, wt in enumerate(weights):
        wpx = int(round(total * wt / s))
        if i == n - 1:                           # 最后一栏吃掉取整误差，保证正好铺满
            wpx = x1 - x
        wpx = max(1, wpx)
        boxes.append((x, min(x1, x + wpx)))
        x = x + wpx + gap
    return boxes


def _content_at(seq, i: int, default: str = "all") -> str:  # noqa: ANN001
    """取第 i 栏的配置值；配置里的栏数少于实际栏数时回落默认。"""
    try:
        return str(seq[i])
    except Exception:  # noqa: BLE001
        return default


def _filter_entries(entries, sel: str) -> list:  # noqa: ANN001
    """按栏的内容筛选：`all` / `mine` / `theirs` / `peer`（peer = 房间里别人的话）。"""
    sel = (sel or "all").strip().lower()
    if sel in ("", "all", "*"):
        return list(entries or [])
    out = []
    for e in entries or []:
        who = _unpack_entry(e)[0]
        if sel == "peer":
            if _peer_id_of(who) is not None:
                out.append(e)
        elif str(who).strip().lower() == sel:
            out.append(e)
    return out


def render_split(entries, cfg: OverlayConfig | None = None) -> Image.Image:
    """**一块面板、左右两栏**（方案 A）：每栏宽度可自定义，内容按栏筛选。

    与 `render_conversation` 只差「分区」——外框、配色、字号、换行规则、溢出策略全部沿用，
    所以同一份内容在开/关分栏时观感一致；换行宽度取**该栏**宽度，不是整块面板。
    仍然是**同一块 SteamVR overlay**：不新增 handle，也就不引入新的失败面。
    """
    cfg = cfg or OverlayConfig()
    w, h = cfg.size_px
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = 12
    d.rounded_rectangle([pad, pad, w - pad, h - pad], radius=28,
                        fill=(*cfg.color_bg, cfg.bg_alpha),
                        outline=(*cfg.color_border, cfg.border_alpha), width=3)

    def _f(size: int) -> ImageFont.FreeTypeFont:
        try:
            return ImageFont.truetype(cfg.font, size)
        except Exception:
            return ImageFont.load_default()

    tf, sf = _f(cfg.font_size), _f(cfg.source_font_size)
    boxes = _pane_boxes(pad * 2, w - pad * 2, cfg.split_panes, cfg.split_gap_px)
    has_label = any(str(s).strip() for s in (cfg.split_labels or ()))
    label_h = (cfg.source_font_size + 10) if has_label else 0

    for i, (x0, x1) in enumerate(boxes):
        if i and cfg.split_divider:              # 栏间竖线（画在两栏之间那条空隙的中线）
            gx = (boxes[i - 1][1] + x0) // 2
            d.line([gx, pad * 2 + 4, gx, h - pad * 2 - 4],
                   fill=(*cfg.color_border, max(40, cfg.border_alpha // 2)), width=2)
        lab = _content_at(cfg.split_labels, i, "").strip()
        if lab:
            d.text(((x0 + x1) // 2, pad * 2), lab, font=sf,
                   fill=(*cfg.color_source, cfg.source_alpha), anchor="ma")
        _draw_entries(d, _filter_entries(entries, _content_at(cfg.split_content, i, "all")),
                      cfg, x0=x0, x1=x1, y_bottom=h - pad * 2,
                      budget=(h - 3 * pad) - label_h, tf=tf, sf=sf)
    return img


def render_conversation(entries, cfg: OverlayConfig | None = None) -> Image.Image:
    """把「最近的对话」渲染成**一块**面板 —— 镜像 GUI 的聊天区（别人在左、我在右）。

    entries: 每条是 3 元组 `(who, source, translation)`（现有形态）或
    4 元组 `(who, source, translation, label)`，`label` = 说话人昵称（小字）。
    `who ∈ {"mine","theirs"}` 或 `"peer:<peer_id>"`（房间里 N 个不同的人）。**最后一条最新**。

    `peer:<id>` 按 theirs 那样靠左，但外缘竖条的颜色取自固定调色板、按 peer_id 稳定哈希
    （同一个人任何时候都同色，重连也不变）；昵称画成小字（沿用原文小字的字号/颜色体系）。

    尺寸固定为 cfg.size_px（不改物理宽高比，避免手腕上的面板忽大忽小）；
    从最新往回塞，塞不下的更早条目直接不画 —— 永远优先显示最新内容。
    """
    cfg = cfg or OverlayConfig()
    w, h = cfg.size_px
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = 12
    d.rounded_rectangle([pad, pad, w - pad, h - pad], radius=28,
                        fill=(*cfg.color_bg, cfg.bg_alpha),
                        outline=(*cfg.color_border, cfg.border_alpha), width=3)

    def _f(size: int) -> ImageFont.FreeTypeFont:
        try:
            return ImageFont.truetype(cfg.font, size)
        except Exception:
            return ImageFont.load_default()

    tf, sf = _f(cfg.font_size), _f(cfg.source_font_size)
    _draw_entries(d, entries, cfg, x0=pad * 2, x1=w - pad * 2,
                  y_bottom=h - pad * 2, budget=h - 3 * pad, tf=tf, sf=sf)
    return img


def build_matrix(pos: tuple[float, float, float], rot_deg: tuple[float, float, float]):
    """构造 OpenVR 的 3x4 位姿矩阵（行主序）。需要 openvr 才能返回其 ctypes 类型。"""
    import openvr

    rx, ry, rz = (math.radians(a) for a in rot_deg)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    # Rz * Ry * Rx
    r = [
        [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
        [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
        [-sy, cy * sx, cy * cx],
    ]
    m = openvr.HmdMatrix34_t()
    for i in range(3):
        for j in range(3):
            m.m[i][j] = r[i][j]
        m.m[i][3] = pos[i]
    return m


# ---------------------------------------------------------------- overlay 本体
class WristOverlay:
    """SteamVR overlay 生命周期管理 + 文本刷新 + 配置热重载。"""

    # 健康心跳间隔（秒）：日志里留一条「面板还活着 / 已经多久没成功上传」的定期证据。
    # 用户报的「隔一阵手腕屏就消失」没有这行时，事后完全看不出是哪个时刻掉的。
    HEARTBEAT_S = 30.0

    def __init__(self, cfg: OverlayConfig, config_path: Path | None = None, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.dry_run = dry_run
        self._frames_dir = ROOT / "out" / "overlay_frames"
        self._vr = None
        self._overlay = None
        self._handle = None
        self._device = None
        self._last_render: tuple[str, str] | None = None
        self._last_entries: tuple | None = None
        self._upload_fails = 0             # 贴图连续上传失败次数（用于降噪 + 触发重建）
        self._last_cfg_mtime = 0.0
        self._last_text_at = 0.0
        # ---- 自愈状态机 + 诊断（用户实测「手腕屏隔一阵就消失」，见 _upload 的注释）----
        self._last_ok_at = 0.0             # 最后一次成功上传贴图的时刻（monotonic）
        self._recover_stage = "none"       # none / rebuild / reinit
        self._fails_in_stage = 0           # 本阶段内连续失败次数（到 3 就升级到下一阶段）
        self._rebuilds_since_ok = 0        # 自上次成功以来重建几次
        self._reinits_since_ok = 0         # 自上次成功以来硬重启几次
        self._rebuilds = 0                 # 累计
        self._reinits = 0
        self._last_heartbeat = 0.0
        self.available = False
        self.frames_updated = 0

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        """尝试接管 SteamVR；没跑就返回 False 并禁用（不抛异常、不阻塞）。"""
        if not self.cfg.enabled:
            print("[overlay] 配置里已禁用")
            return False
        if self.dry_run:
            self._frames_dir.mkdir(parents=True, exist_ok=True)
            print(f"[overlay][dry-run] 不接管 SteamVR，渲染结果写到 {self._frames_dir}")
            return False
        try:
            import openvr
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] openvr 不可用：{exc}")
            return False
        try:
            self._vr = openvr.init(openvr.VRApplication_Background)
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ⚠️ SteamVR 未运行或不可用，overlay 已禁用（其他输出不受影响）：{exc}")
            return False
        try:
            # ⚠️ overlay 接口是**模块级工厂函数** `openvr.IVROverlay()`，
            # 不是 IVRSystem 的属性 —— 写 `self._vr.overlay` 会抛
            # AttributeError: 'IVRSystem' object has no attribute 'overlay'，
            # 而且必须放在 try 里：否则异常会冒到引擎，把整条翻译腿一起打死。
            self._overlay = openvr.IVROverlay()
            try:
                self._handle = self._overlay.createOverlay(self.cfg.overlay_key, "VLT 手腕屏")
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__
                if "KeyInUse" not in name and "KeyInUse" not in str(exc):
                    raise
                # 同 key 的**残留 overlay**（上次进程异常退出没清干净）→ 清掉再建一次。
                # 用户实测过这个报错：双引擎同时挂同一个 key 会撞
                # `OverlayError_KeyInUse`（GUI 侧已改成只给 owner 那条腿挂）；
                # 这里兜的是「上一次没清干净」的情况，否则手腕屏会一直被卡住。
                print(f"[overlay] ⚠️ key '{self.cfg.overlay_key}' 已被占用（{name}），"
                      f"尝试清理残留 overlay 后重建", flush=True)
                try:
                    stale = self._overlay.findOverlay(self.cfg.overlay_key)
                    self._overlay.destroyOverlay(stale)
                    print("[overlay]   已清理残留 overlay", flush=True)
                except Exception as exc2:  # noqa: BLE001
                    print(f"[overlay]   清理残留 overlay 未成功（继续尝试重建）："
                          f"{type(exc2).__name__}: {exc2}", flush=True)
                self._handle = self._overlay.createOverlay(self.cfg.overlay_key, "VLT 手腕屏")
            self._device = self._resolve_anchor()
            # ⚠️ available 必须在 _apply_transform 之前置位：它开头有
            # `if not self.available: return`，否则「创建时应用变换」这一步等于没做
            # （面板会出现但停在默认位置）。
            self.available = True
            self._apply_transform()
            self._overlay.showOverlay(self._handle)
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ⚠️ 创建 overlay 失败，已禁用（其他输出不受影响）："
                  f"{type(exc).__name__}: {exc}")
            self.available = False
            return False
        print(f"[overlay] ✅ 已挂到 {self.cfg.anchor}（device={self._device}），"
              f"宽 {self.cfg.width_m}m，位置 {self.cfg.pos}")
        return True

    def _resolve_anchor(self):
        import openvr

        sys_ = self._vr
        a = self.cfg.anchor
        try:
            if a == "left_hand":
                return sys_.getTrackedDeviceIndexForControllerRole(openvr.TrackedControllerRole_LeftHand)
            if a == "right_hand":
                return sys_.getTrackedDeviceIndexForControllerRole(openvr.TrackedControllerRole_RightHand)
            if a == "hmd":
                return openvr.k_unTrackedDeviceIndex_Hmd
            if a == "tracker":
                idx = 0
                for i in range(openvr.k_unMaxTrackedDeviceCount):
                    if sys_.getTrackedDeviceClass(i) == openvr.TrackedDeviceClass_GenericTracker:
                        if idx == self.cfg.tracker_index:
                            print(f"[overlay] tracker #{self.cfg.tracker_index} → device {i}")
                            return i
                        idx += 1
                print(f"[overlay] ⚠️ 没找到 tracker #{self.cfg.tracker_index}（驱动注册 ≠ 已配对在线），回退右手")
                return sys_.getTrackedDeviceIndexForControllerRole(openvr.TrackedControllerRole_RightHand)
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ⚠️ 解析锚点失败：{exc}")
        return openvr.k_unTrackedDeviceIndex_Hmd

    def _apply_transform(self) -> None:
        if not self.available:
            return
        m = build_matrix(self.cfg.pos, self.cfg.rot)
        self._overlay.setOverlayTransformTrackedDeviceRelative(self._handle, self._device, m)
        self._overlay.setOverlayWidthInMeters(self._handle, self.cfg.width_m)
        self._overlay.setOverlayAlpha(self._handle, self.cfg.alpha)
        # 弯曲每次都应用：只在 >0 时设置的话，把它调回 0 就永远回不去了
        self._overlay.setOverlayCurvature(self._handle, max(0.0, min(1.0, self.cfg.curvature)))

    # ---------- 文本 ----------
    def update(self, text: str, source: str = "", force: bool = False) -> None:
        """刷新面板内容（相同内容不重复上传贴图）。"""
        text, source = text.strip(), source.strip()
        if not text:
            return
        key = (text, source if self.cfg.show_source else "")
        if key == self._last_render and not force:
            return
        self._last_render = key
        self._last_text_at = time.monotonic()
        img = render_panel(text, source, self.cfg)
        if self.dry_run:
            self.frames_updated += 1
            path = self._frames_dir / f"{self.frames_updated:03d}.png"
            img.save(path)
            print(f"[overlay][dry-run] 第 {self.frames_updated} 帧 → {path.name} | {text[:50]}")
            return
        if not self.available:
            return
        self._upload(img)

    def update_entries(self, entries, force: bool = False) -> None:
        """刷新成**对话视图**（GUI 用这个）：entries = [(who, source, translation), ...]
        或 4 元组 [(who, source, translation, label), ...]（label = 说话人昵称）。

        与 `update()` 的区别：`update()` 是"当前这一句"（CLI 单腿场景够用），
        `update_entries()` 是"最近几句对话"——手腕上只有一块屏，内容应该像 GUI 的聊天区。
        """
        key = tuple((w, (s or "").strip(), (t or "").strip(), (lab or "").strip())
                    for w, s, t, lab in (_unpack_entry(e) for e in (entries or [])))
        if key == self._last_entries and not force:
            return
        self._last_entries = key
        self._last_text_at = time.monotonic()
        img = render_split(entries, self.cfg) if self.cfg.split else render_conversation(entries, self.cfg)
        if self.dry_run:
            self.frames_updated += 1
            self._frames_dir.mkdir(parents=True, exist_ok=True)
            path = self._frames_dir / f"{self.frames_updated:03d}.png"
            img.save(path)
            print(f"[overlay][dry-run] 第 {self.frames_updated} 帧（对话视图，{len(entries or [])} 条）"
                  f" → {path.name}")
            return
        if not self.available:
            return
        self._upload(img)

    def _upload(self, img: Image.Image) -> None:
        w, h = img.size
        data = img.tobytes()               # RGBA
        buf = ctypes.create_string_buffer(data, len(data))
        try:
            self._overlay.setOverlayRaw(self._handle, buf, w, h, 4)
        except Exception as exc:  # noqa: BLE001
            self._upload_fails += 1
            f = self._upload_fails
            self._fails_in_stage += 1
            # 降噪：连失败 86 次时每帧打一行会把日志彻底淹没（用户实测就是这样），
            # 只报第一次 + 每隔 50 次汇总一条；但**第一次**要带上现场，
            # 否则事后只有一句 RequestFailed，谁也判断不出是「面板卡住」还是「面板被丢掉」。
            if f == 1:
                print(f"[overlay] ❌ setOverlayRaw 失败：{type(exc).__name__}: {exc}")
                print(f"[overlay][diag] 失败现场：{self._state_brief()}")
            if f % 50 == 0:
                print(f"[overlay] ❌ 贴图上传已连续失败 {f} 次（面板会停在最后一帧）")
                print(f"[overlay][diag] {self._state_brief()}")
            self._escalate()
            return
        self.frames_updated += 1
        self._last_ok_at = time.monotonic()
        if self._upload_fails:
            print(f"[overlay] ✅ 贴图恢复上传（连续失败 {self._upload_fails} 次；"
                  f"期间重建 {self._rebuilds_since_ok} 次、硬重启 {self._reinits_since_ok} 次）")
            self._upload_fails = 0
            self._recover_stage = "none"
            self._fails_in_stage = 0
            self._rebuilds_since_ok = 0
            self._reinits_since_ok = 0
        # 也别每帧都打：第 1 帧 + 每 50 帧一条（保留"还在刷"的证据，但不淹日志）
        if self.frames_updated == 1 or self.frames_updated % 50 == 0:
            print(f"[overlay] ← 贴图已更新（{w}x{h}, 第 {self.frames_updated} 帧）")

    def _recreate_overlay(self) -> bool:
        """销毁重建 overlay —— 贴图上传连续失败时用来恢复。

        SteamVR 的 `setOverlayRaw` 每次调用都会新建一张贴图，长时间高频更新后
        可能开始返回 `OverlayError_RequestFailed`（用户实测：连续失败 86 次，
        手腕屏停在最后一帧不动，之后又自己恢复）。重建能拿到一张干净的贴图；
        重建失败也不致命，下一次失败还会再试。
        """
        if self.dry_run or self._overlay is None:
            return False
        entries = list(self._last_entries or [])
        try:
            try:
                if self._handle is not None:
                    self._overlay.destroyOverlay(self._handle)
            except Exception:  # noqa: BLE001
                pass
            self._handle = self._overlay.createOverlay(self.cfg.overlay_key, "VLT 手腕屏")
            self._device = self._resolve_anchor()
            self.available = True
            self._apply_transform()
            self._overlay.showOverlay(self._handle)
            self._rebuilds += 1
            self._rebuilds_since_ok += 1
            self._recover_stage = "rebuild"
            self._fails_in_stage = 0
            print(f"[overlay] ♻️ 已重建 overlay 并重新贴到 {self.cfg.anchor}"
                  f"（device={self._device}，自上次成功以来第 {self._rebuilds_since_ok} 次）")
            if entries:                        # 新 overlay 是空的，立刻把内容贴回去
                self._last_entries = None
                self.update_entries(entries, force=True)
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ⚠️ 重建 overlay 失败：{type(exc).__name__}: {exc}")
            return False

    # ---------- 自愈升级 + 诊断 ----------
    def _escalate(self) -> None:
        """贴图连续失败时的自愈升级：重建 handle → 硬重启 openvr 上下文。

        用户实测（2026-09-25 20:32，家里那台机器）证明「重建 handle」这一级不够用：

            20:32:07  ← 贴图已更新（第 200 帧）              # 一切正常
            20:32:09  ❌ setOverlayRaw 失败：OverlayError_RequestFailed
                      ♻️ 连续失败 50/100/…/350 次 → 每次都重建，**仍然全程失败**
            20:35:09  会话结束

        面板一旦进入这个状态，**换 handle 救不回来**：换 handle 换不掉已经死掉的
        openvr 上下文（SteamVR 合成器重启 / 头显待机回来 / vrserver 换了一代之后，
        手里这份接口指针会永远返回失败）。所以再加一级：shutdown + init + 重建。
        """
        if self.dry_run or self._overlay is None:
            return
        if self._fails_in_stage < 3:
            return
        if self._recover_stage == "none":
            print(f"[overlay] ♻️ 贴图连续失败 {self._upload_fails} 次 → 重建 overlay")
            self._recreate_overlay()
        elif self._recover_stage == "rebuild":
            print(f"[overlay] ♻️ 重建之后仍然连续失败 {self._fails_in_stage} 次"
                  f"（换 handle 没用）→ 硬重启 openvr 连接")
            self._hard_reinit()
        elif self._upload_fails % 50 == 0:
            # 已经硬重启过还在失败：别每 3 帧就重启一次 openvr（那等于自己把自己拖死），
            # 每 50 次失败再试一次。
            print(f"[overlay] ♻️ 硬重启之后仍然连续失败 {self._upload_fails} 次 → 再硬重启一次")
            self._hard_reinit()

    def _hard_reinit(self) -> bool:
        """整条 openvr 连接重启：shutdown → init → 重建 overlay → 把内容贴回去。

        ⚠️ 进程里只有**手腕屏这一处**在用 openvr（GUI 双引擎模式下也只给 owner 那条腿挂），
        所以 shutdown 不会波及别的组件；调用点都在同一个引擎线程（init 与 shutdown 同线程）。
        """
        if self.dry_run:
            return False
        try:
            import openvr
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ⚠️ 硬重启失败（openvr 不可用）：{exc}")
            return False
        try:
            openvr.shutdown()
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay]   硬重启：shutdown 报错（忽略，继续 init）：{type(exc).__name__}: {exc}")
        try:
            self._vr = openvr.init(openvr.VRApplication_Background)
            self._overlay = openvr.IVROverlay()
            self._handle = self._overlay.createOverlay(self.cfg.overlay_key, "VLT 手腕屏")
            self._device = self._resolve_anchor()
            self.available = True
            self._apply_transform()
            self._overlay.showOverlay(self._handle)
        except Exception as exc:  # noqa: BLE001
            print(f"[overlay] ⚠️ 硬重启 openvr 失败（下轮失败时再试）："
                  f"{type(exc).__name__}: {exc}")
            self.available = False
            return False
        self._reinits += 1
        self._reinits_since_ok += 1
        self._recover_stage = "reinit"
        self._fails_in_stage = 0
        print(f"[overlay] ♻️♻️ 已硬重启 openvr 连接并重建 overlay"
              f"（累计第 {self._reinits} 次，device={self._device}）")
        entries = list(self._last_entries or [])
        if entries:                            # 新 overlay 是空的，立刻把内容贴回去
            self._last_entries = None
            self.update_entries(entries, force=True)
        return True

    def _overlay_exists(self) -> bool | None:
        """我们那把 key 还在 SteamVR 里吗？None = 判断不了。

        这行决定「面板消失」的两种可能里到底是哪种：
        - 还在   → 面板只是**卡在最后一帧**（上传被拒，对象本身没死）
        - 已不在 → 面板是**真被 SteamVR 丢掉了**（合成器重启/被清理），必须重建
        """
        if self._overlay is None or self._handle is None:
            return None
        try:
            self._overlay.findOverlay(self.cfg.overlay_key)
            return True
        except Exception as exc:  # noqa: BLE001
            name = f"{type(exc).__name__}{exc}"
            if "UnknownOverlay" in name:
                return False
            return None

    def _hmd_present(self) -> str:
        try:
            import openvr

            fn = getattr(openvr, "VR_IsHmdPresent", None)
            if fn is None:
                return "?"
            return "是" if fn() else "否"
        except Exception:  # noqa: BLE001
            return "?"

    def _visible_str(self) -> str:
        if self._overlay is None or self._handle is None:
            return "?"
        try:
            return "是" if self._overlay.isOverlayVisible(self._handle) else "否"
        except Exception:  # noqa: BLE001
            return "?"

    def _state_brief(self) -> str:
        """一行现场信息，故障时决定下一步查什么（永不抛异常）。"""
        age = time.monotonic() - self._last_ok_at if self._last_ok_at else -1.0
        exists = self._overlay_exists()
        bits = [
            f"最后成功上传 {age:.0f}s 前" if age >= 0 else "还没成功上传过",
            f"帧数={self.frames_updated}",
            {True: "overlay在", False: "overlay已被丢弃", None: "overlay状态未知"}[exists],
            f"可见={self._visible_str()}",
            f"HMD在线={self._hmd_present()}",
            f"重建={self._rebuilds}/硬重启={self._reinits}",
        ]
        return " | ".join(bits)

    def _log_heartbeat(self) -> None:
        """每 HEARTBEAT_S 秒一行状态 —— 「面板隔一阵就消失」全靠这行定位。"""
        print(f"[overlay][diag] 心跳：{self._state_brief()}")
        if self._overlay_exists() is False:
            print("[overlay] ⚠️ SteamVR 里已经没有这把 key 了 —— 面板是**真消失**"
                  "（不是卡在最后一帧）→ 重建 overlay")
            self._recreate_overlay()
        elif self._visible_str() == "否":
            # 对象还在、只是被藏起来了（SteamVR 在某些界面切换里会这么做）——
            # 重新 show 一次就好，不必重建。
            print("[overlay] ⚠️ 面板存在但**不可见**（被 SteamVR 藏起来了）→ 重新 showOverlay")
            try:
                self._overlay.showOverlay(self._handle)
            except Exception as exc:  # noqa: BLE001
                print(f"[overlay]   showOverlay 失败：{type(exc).__name__}: {exc}")

    # ---------- 周期任务：热重载 + 淡出 ----------
    def tick(self) -> None:
        # 配置热重载（位置/尺寸改完存盘即生效，无需重启）
        if self.dry_run:
            return
        if self.config_path and self.config_path.exists():
            mtime = self.config_path.stat().st_mtime
            if mtime != self._last_cfg_mtime:
                self._last_cfg_mtime = mtime
                try:
                    import yaml

                    raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
                    new_cfg = OverlayConfig.from_dict(raw.get("overlay") or {})
                    # 弯曲与锚点也要参与比对：少了它们，界面上拖动弯曲滑块、
                    # 或切换锚点（左手/右手/tracker）时热重载会「静默不生效」。
                    geo_changed = (new_cfg.pos, new_cfg.rot, new_cfg.width_m, new_cfg.alpha,
                                   new_cfg.curvature) != (self.cfg.pos, self.cfg.rot,
                                                          self.cfg.width_m, self.cfg.alpha,
                                                          self.cfg.curvature)
                    anchor_changed = (new_cfg.anchor != self.cfg.anchor
                                      or new_cfg.tracker_index != self.cfg.tracker_index)
                    # 影响**贴图内容**的参数（字号 / 面板像素尺寸 / 行数 / 是否显示原文）：
                    # 这些改了只重应用变换是不够的，必须重新渲染一帧，
                    # 否则界面上拖字号滑块会「看着生效、屏上没变」。
                    render_changed = (new_cfg.font_size != self.cfg.font_size
                                      or new_cfg.source_font_size != self.cfg.source_font_size
                                      or new_cfg.size_px != self.cfg.size_px
                                      or new_cfg.max_lines != self.cfg.max_lines
                                      or new_cfg.show_source != self.cfg.show_source)
                    if geo_changed or anchor_changed or render_changed:
                        last_entries = list(self._last_entries or [])
                        last_render = self._last_render
                        self.cfg = new_cfg
                        if self.available:
                            if anchor_changed:
                                self._device = self._resolve_anchor()
                            if geo_changed or anchor_changed:
                                self._apply_transform()
                            if render_changed:
                                if last_entries:
                                    self._last_entries = None      # 强制重渲
                                    self.update_entries(last_entries, force=True)
                                elif last_render:
                                    self._last_render = None
                                    self.update(last_render[0], last_render[1], force=True)
                        print(f"[overlay] ♻️ 配置热重载：anchor={new_cfg.anchor} pos={new_cfg.pos} "
                              f"rot={new_cfg.rot} width={new_cfg.width_m}m "
                              f"curvature={new_cfg.curvature} alpha={new_cfg.alpha} "
                              f"字号={new_cfg.font_size}/{new_cfg.source_font_size} "
                              f"面板={new_cfg.size_px[0]}x{new_cfg.size_px[1]}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[overlay] ⚠️ 热重载失败（保留旧配置）：{exc}")

        # 健康心跳：定期把「面板还活着吗」写进日志，并在 overlay 已被 SteamVR 丢掉时重建。
        # 放在热重载与淡出之前——它是最该先出结果的一条诊断。
        if self.available:
            now = time.monotonic()
            if now - self._last_heartbeat >= self.HEARTBEAT_S:
                self._last_heartbeat = now
                try:
                    self._log_heartbeat()
                except Exception as exc:  # noqa: BLE001
                    print(f"[overlay] ⚠️ 心跳失败（不影响翻译）：{type(exc).__name__}: {exc}")

        # 可选淡出
        if self.available and self.cfg.fade_after_s > 0 and self._last_text_at:
            idle = time.monotonic() - self._last_text_at
            alpha = 0.0 if idle > self.cfg.fade_after_s else self.cfg.alpha
            try:
                self._overlay.setOverlayAlpha(self._handle, alpha)
            except Exception:
                pass

    def close(self) -> None:
        if self._overlay is not None and self._handle is not None:
            try:
                self._overlay.destroyOverlay(self._handle)
            except Exception:
                pass
        if self._vr is not None:
            try:
                import openvr

                openvr.shutdown()
            except Exception:
                pass
        self.available = False


# ---------------------------------------------------------------- 离线预览
def _demo() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="离线渲染手腕面板（不碰 SteamVR）")
    ap.add_argument("--out", default=str(ROOT / "out" / "wrist_panel.png"))
    ap.add_argument("--text", default="Hello! I'm Nixi. This sentence is being translated in real time, let's see how it looks on your wrist.")
    ap.add_argument("--source", default="你好，我是逆袭。这句话正在被实时翻译，看看贴在你手腕上是什么效果。")
    ap.add_argument("--font", default=None)
    ap.add_argument("--width-m", type=float, default=0.24)
    args = ap.parse_args()

    cfg = OverlayConfig()
    if args.font:
        cfg.font = args.font
    img = render_panel(args.text, args.source, cfg)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"渲染完成：{out}  ({img.size[0]}x{img.size[1]}, 面板宽 {args.width_m}m)")
    print(f"字体：{cfg.font}")


if __name__ == "__main__":
    _demo()
