"""手腕 Overlay 的**共享**部分：配置结构 + 把译文渲染成贴图。

平台后端各自独占一个模块，本文件**不含任何后端实现**，所以两个平台的产物都能带它：

    * Windows → `vlt/output/openvr_overlay.py`（只进 Windows 产物）
    * Linux   → `vlt/output/openxr_overlay.py`（只进 Linux 产物）

这条边界是**被门禁强制**的，不是习惯：`scripts/check_platform_purity.py` 会断言
两边产物里都不出现对方后端的模块名与字样，详见 `docs/平台约束记录.md` 第三节。

设计要点（见 notes/VRChat实时同传-架构设计-端到端路线.md §4.4）：
- 纯 Python（Pillow 画图），不需要 Unity / 第三方 overlay 管理器
- **中文/日文用 Pillow 画成图**，绕开 chatbox 与 avatar 头顶字的字符集限制
- 锚点 / 位置 / 旋转 / 尺寸 / 透明度可配 + 热重载（两端共用同一份配置语义）
- 离线可验证：`--demo` 只渲染 PNG，不碰 VR 运行时
"""
from __future__ import annotations

import zlib                     # 只有 peer_color 用：房间成员的稳定配色（crc32，确定性哈希）
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 调试帧是"跑完要看"的产物 → 可写目录（exe 旁），不是临时解包目录
from ..paths import APP_DIR as ROOT

# 配置里没写字体时用哪份：按平台探测（Windows → 雅黑；Linux → fontconfig）。
# 解析结果缓存，避免每次渲染都去 spawn 一个 fc-match。
_font_cache: dict[str, str | None] = {}


def resolve_font_path(configured: str) -> str | None:
    """决定这次渲染用哪个字体文件。

    优先级：配置里指定的（且文件真的存在）→ 平台自动探测 → None（调用方回落位图字体）。

    **为什么不能把 `C:/Windows/Fonts/msyh.ttc` 写死**：那是 Windows 专有路径。
    换到别的机器上 `ImageFont.truetype` 会抛
    `OSError: cannot open resource` —— 实测 Linux 上 `tests/test_wrap.py` 就是这么挂的，
    而手腕屏会退化成 `load_default()` 的位图字体（画不了中日韩，等于白屏）。
    """
    key = configured or ""
    if key not in _font_cache:
        resolved: str | None = None
        if configured and Path(configured).exists():
            resolved = configured
        else:
            from ..platform import find_cjk_font
            resolved = find_cjk_font()
        _font_cache[key] = resolved
    return _font_cache[key]


# ---------------------------------------------------------------- 配置


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
    font: str = ""                           # 空 = 按平台自动探测（见 resolve_font_path）
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
    # 手腕屏后端：auto（按平台自动选）/ null（彻底禁用）。
    # 刻意只支持这两个 —— 强行指定具体后端会让平台隔离破功
    # （两端产物的依赖集会在共享代码里被打通），见 vlt/platform/__init__.py 的说明。
    backend: str = "auto"
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
            font=d.get("font") or "",
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
            backend=str(d.get("backend", "auto") or "auto"),
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
        path = resolve_font_path(cfg.font)
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001 — 字体文件坏了也别让整条腿挂掉
                pass
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

    整块面板（`render_conversation`）走这一段 ——
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

def render_conversation(entries, cfg: OverlayConfig | None = None) -> Image.Image:
    """把「最近的对话」渲染成**一块**面板 —— 镜像 GUI 的聊天区（别人在左、我在右）。

    entries: 每条是 3 元组 `(who, source, translation)` 或 4 元组 `(who, source, translation, label)`，
    `label` = 说话人昵称（小字）。`who ∈ {"mine","theirs"}` 或 `"peer:<peer_id>"`（房间里 N 个不同的人）。
    **最后一条最新。**

    `peer:<id>` 按 theirs 那样靠左，但外缘竖条的颜色取自固定调色板、按 peer_id **稳定哈希**
    （同一个人任何时候都同色，重连也不变）；昵称画成小字。

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
        path = resolve_font_path(cfg.font)
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                pass
        return ImageFont.load_default()

    tf, sf = _f(cfg.font_size), _f(cfg.source_font_size)
    _draw_entries(d, entries, cfg, x0=pad * 2, x1=w - pad * 2,
                  y_bottom=h - pad * 2, budget=h - 3 * pad, tf=tf, sf=sf)
    return img


def _demo() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="离线渲染手腕面板（不碰 VR 运行时）")
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
