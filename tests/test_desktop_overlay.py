#!/usr/bin/env python
"""桌面字幕（issue #11：VRChat 桌面模式叠加窗）验收。

盯的是**共享层**：锚点/夹取/透明度/配置继承这些纯函数，dry-run 出图，Tk 冒烟
（建窗 → 出图 → 拖动 → 透明度 → 关闭幂等），多显示器下按「目标窗口所在那块屏」取工作区，
以及界面那侧的透明度滑块（初值继承 + 只有真拖过才落盘）。真正的「贴着 VRChat 窗口跟随」
只能在跑了游戏的机器上验 —— 那由维护者本机的复验脚本人工做（不进仓库、不进 CI）。

写法照 `tests/test_overlay_conversation.py`：单文件脚本，直接跑，全绿打印 ALL PASSED。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

import yaml

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
for p in (str(ROOT), str(TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from PIL import Image  # noqa: E402

from vlt import platform  # noqa: E402
from vlt.output.desktop_overlay import (  # noqa: E402
    ANCHORS, KEY_RGB, TRANSPARENT_KEY, DesktopOverlay, DesktopOverlayConfig,
    clamp_alpha, compute_position, resolve_position,
)

# 固定的几何常量：断言里写死具体数字，锚点算错一格就红
RECT = (100, 50, 900, 650)          # 目标窗口客户区（屏幕坐标）
SIZE = (400, 200)                   # 面板像素尺寸
AREA = (0, 0, 1920, 1080)           # 屏幕工作区

# 副屏工作区：主屏**左侧**那块（坐标全是负的，真实多屏里最常见的一种排布）
AREA_LEFT = (-1920, 0, 0, 1080)
# 副屏工作区：主屏**上方**那块（y 为负）
AREA_ABOVE = (0, -1080, 1920, 0)

# 假「游戏窗口」的标题：独一无二，免得测试去贴用户真在跑的 VRChat
FAKE_TITLE = "VLT_FAKE_VRCHAT_WINDOW"
# 一定不存在的标题：界面那条用例靠它退化成屏幕绝对定位，不依赖本机是否真开着 VRChat
FAKE_MISSING_TITLE = "___vlt_不存在的窗口___"

# 界面透明度滑块用例的三个值：
#   GUI_OVERLAY_ALPHA = 手腕屏（`overlay:` 段）的透明度 —— 桌面字幕**绝不该**读它；
#   GUI_SANDBOX_ALPHA = 桌面段（`desktop_overlay:`）自己的透明度；
#   GUI_SLIDER_ALPHA  = 用户拖动滑块后落盘的值。
# 三个值互不相同，「读错了哪一段」一断言就现形。
GUI_SANDBOX_DIR = ROOT / "out" / "desktop_overlay_cfg"
GUI_SANDBOX = GUI_SANDBOX_DIR / "config.yaml"
GUI_OVERLAY_ALPHA = 0.5
GUI_SANDBOX_ALPHA = 0.65
GUI_SLIDER_ALPHA = 0.35

SAMPLE_ENTRIES = [
    ("theirs", "Hello there, can you hear me?", "你好，能听到我说话吗？"),
    ("mine", "听得非常清楚，谢谢。", "Loud and clear, thanks."),
    ("theirs", "さっきの話、ほんとう？", "刚才那件事，是真的吗？"),
]


# ---------------------------------------------------------------- 1. 九个锚点 + offset
def test_compute_position_nine_anchors() -> None:
    want = {
        "top_left": (100, 50), "top_center": (300, 50), "top_right": (500, 50),
        "middle_left": (100, 250), "center": (300, 250), "middle_right": (500, 250),
        "bottom_left": (100, 450), "bottom_center": (300, 450), "bottom_right": (500, 450),
    }
    for anchor in ANCHORS:
        if anchor == "free":
            continue
        got = compute_position(anchor, (0, 0), RECT, SIZE, AREA)
        assert got == want[anchor], f"{anchor}: 期望 {want[anchor]}，实际 {got}"
    assert len(ANCHORS) == 10 and "free" in ANCHORS, ANCHORS

    # offset 必须原样叠加（歌词式字幕默认往上抬 48px，贴边时就是靠它抬进画面里）
    assert compute_position("bottom_center", (0, -48), RECT, SIZE, AREA) == (300, 402)
    assert compute_position("top_left", (25, 13), RECT, SIZE, AREA) == (125, 63)
    print("  九个锚点 + offset OK")


# ---------------------------------------------------------------- 2. 屏幕夹取
def test_position_clamped_into_area() -> None:
    # a) 面板比可用区域还大 → 贴 area 左上角（至少看得见，不许夹成负坐标）
    assert compute_position("center", (0, 0), RECT, (2000, 1200), AREA) == (0, 0)
    # b) 贴边越界 → 夹回 area 内（字幕不许被甩到屏幕外）
    got = compute_position("bottom_right", (300, 300), (1600, 900, 1920, 1080), SIZE, AREA)
    assert got == (1520, 880), got
    # c) 负偏移越界 → 夹到 0
    assert compute_position("top_left", (-400, -300), RECT, SIZE, AREA) == (0, 0)
    # d) 副屏在左侧（工作区原点是负的）→ 以 area 左上角为界，不是以 (0,0)
    got = compute_position("top_left", (-400, -300), RECT, SIZE, (-1920, 0, 0, 1080))
    assert got == (-400, 0), got
    print("  屏幕夹取（超大面板 / 越界 / 负坐标 / 副屏）OK")


# ---------------------------------------------------------------- 2b. 多显示器：副屏不被拽回主屏
def test_clamp_stays_on_secondary_monitor() -> None:
    """纯函数：工作区是**副屏**（负坐标）时，夹取结果必须留在副屏里。

    真 bug（审查实测确认）：夹取用的工作区来自 `SPI_GETWORKAREA`，那**只有主屏**一份 ——
    副屏上的字幕被 `_clamp_into_area` 拽回主屏，用户看到的是字幕"自己跑到另一块屏上"。
    左侧副屏坐标全是负的，被拽得尤其明显（直接飞到主屏左边界）。
    """
    rect = (-1800, 100, -1100, 700)            # 左侧副屏上的目标窗口客户区
    got = compute_position("bottom_center", (0, -40), rect, SIZE, AREA_LEFT)
    assert got == (-1650, 460), got
    assert AREA_LEFT[0] <= got[0] and got[0] + SIZE[0] <= AREA_LEFT[2], \
        f"横向被拽出副屏：{got}"
    assert AREA_LEFT[1] <= got[1] and got[1] + SIZE[1] <= AREA_LEFT[3], \
        f"纵向被拽出副屏：{got}"
    # 对照：同一个落点用**主屏**工作区去夹 → 被拽到主屏左边界（这就是 bug 的样子）
    yanked = compute_position("bottom_center", (0, -40), rect, SIZE, AREA)
    assert yanked == (0, 460), yanked
    assert yanked != got, (yanked, got)

    # 上方副屏（y 为负）同样成立
    got2 = compute_position("top_center", (0, 20), (200, -1000, 1600, -200),
                            SIZE, AREA_ABOVE)
    assert got2 == (700, -980), got2
    assert AREA_ABOVE[1] <= got2[1] and got2[1] + SIZE[1] <= AREA_ABOVE[3], got2

    # 面板比副屏还大 → 贴**副屏**左上角（不是主屏的 (0,0)）
    assert compute_position("center", (0, 0), rect, (3000, 2000),
                            AREA_LEFT) == (-1920, 0)
    assert resolve_position(
        DesktopOverlayConfig(enabled=True, anchor="free", pos=(-1700, 200), size_px=SIZE),
        None, SIZE, AREA_LEFT) == (-1700, 200)
    # 自由模式的绝对坐标越出副屏 → 夹到副屏边界，不是夹到 0
    assert resolve_position(
        DesktopOverlayConfig(enabled=True, anchor="free", pos=(-9999, 9999), size_px=SIZE),
        None, SIZE, AREA_LEFT) == (-1920, 880)
    print("  副屏工作区（负坐标）夹取留在副屏、不被拽回主屏 OK")


# ---------------------------------------------------------------- 3. resolve_position
def test_resolve_position_falls_back_to_cfg_pos() -> None:
    cfg = DesktopOverlayConfig(enabled=True, anchor="bottom_center",
                               pos=(80, 80), size_px=SIZE)
    # a) 没有目标窗口（rect=None）→ 用绝对坐标
    assert resolve_position(cfg, None, SIZE, AREA) == (80, 80)
    # b) anchor=free → 即使有 rect 也用绝对坐标
    free = DesktopOverlayConfig(enabled=True, anchor="free", pos=(222, 333), size_px=SIZE)
    assert resolve_position(free, RECT, SIZE, AREA) == (222, 333)
    # c) attach_to_game=False → 绝对坐标
    loose = DesktopOverlayConfig(enabled=True, attach_to_game=False,
                                 pos=(10, 20), size_px=SIZE)
    assert resolve_position(loose, RECT, SIZE, AREA) == (10, 20)
    # d) 正常贴窗 = compute_position
    assert resolve_position(cfg, RECT, SIZE, AREA) == \
        compute_position("bottom_center", cfg.offset, RECT, SIZE, AREA)
    # e) 绝对坐标也要夹进屏幕（用户手填 -9999 不许把字幕甩没）
    wild = DesktopOverlayConfig(enabled=True, anchor="free", pos=(-9999, 99999), size_px=SIZE)
    assert resolve_position(wild, None, SIZE, AREA) == (0, 880)
    print("  resolve_position 三条兜底 + 夹取 OK")


# ---------------------------------------------------------------- 4. clamp_alpha
def test_clamp_alpha_bounds() -> None:
    assert clamp_alpha(0.0) == 0.2        # 低于下限夹到 0.2：全透明等于"功能坏了"
    assert clamp_alpha(1.5) == 1.0
    assert clamp_alpha("0.5") == 0.5      # 配置读出来是字符串也要认
    assert clamp_alpha(None) == 0.9       # 非法值回落默认
    assert clamp_alpha("abc") == 0.9
    assert clamp_alpha(0.9) == 0.9
    assert clamp_alpha(1) == 1.0
    assert isinstance(clamp_alpha(0.5), float)
    print("  clamp_alpha 边界 OK")


# ---------------------------------------------------------------- 5. from_dict
def test_alpha_mask_bits() -> None:
    """形状蒙版字节口径：LSB-first、行末补零、阈值语义、只看面板自身 alpha。"""
    from PIL import Image

    from vlt.platform.overlay_pixels import alpha_mask_bits
    # 9 像素宽（跨字节边界）2 行：第一行前 4 个实心，第二行全空
    img = Image.new("RGBA", (9, 2), (0, 0, 0, 0))
    for x in range(4):
        img.putpixel((x, 0), (255, 0, 0, 255))
    bits = alpha_mask_bits(img)
    assert bits == b"\x0f\x00\x00\x00", bits     # LSB-first：0b0000_1111 → 0x0f
    # 默认阈值 32：31 不算、32 算（只裁"几乎全透明"的外圈——
    # 128 会误裁 border_alpha=120 的边框与低透明度底板，见函数文档）
    img2 = Image.new("RGBA", (8, 1), (0, 0, 0, 0))
    img2.putpixel((0, 0), (0, 0, 0, 31))
    img2.putpixel((7, 0), (0, 0, 0, 32))
    assert alpha_mask_bits(img2) == b"\x80", alpha_mask_bits(img2)
    # 显式阈值仍然可用：127 不算、128 算
    img2.putpixel((0, 0), (0, 0, 0, 127))
    img2.putpixel((7, 0), (0, 0, 0, 128))
    assert alpha_mask_bits(img2, threshold=128) == b"\x80", \
        alpha_mask_bits(img2, threshold=128)
    # 宽度不是 8 的倍数也照打包（行末补零）
    img3 = Image.new("RGBA", (3, 1), (0, 0, 0, 255))
    assert alpha_mask_bits(img3) == b"\x07", alpha_mask_bits(img3)
    print("  alpha_mask_bits（LSB-first / 行补零 / 默认阈值 32 / 显式阈值）OK")


def test_from_dict_defaults_and_own_keys() -> None:
    """桌面字幕的配置**完全独立**：所有键只认 `desktop_overlay:` 段，缺省回落代码默认值。

    （2026-10-04 维护者口径：桌面面板与手腕屏区别太大，不许共享配置。）
    """
    # a) 缺段 → 全默认
    c = DesktopOverlayConfig.from_dict({})
    assert (c.enabled, c.mode, c.anchor, c.attach_to_game) == \
        (False, "conversation", "bottom_center", True), c
    assert c.offset == (0, -48) and c.pos == (80, 80) and c.size_px == (1024, 360), c
    assert abs(c.alpha - 0.9) < 1e-9 and c.click_through and c.follow
    assert c.game_title == "VRChat"
    v = c.visual_config()
    assert v.size_px == (1024, 360)
    assert v.font_size == 36 and v.source_font_size == 29 and v.max_lines == 3
    assert v.color_bg == (12, 14, 20)     # 视觉默认取 OverlayConfig 的**代码默认值**
    assert v.show_source is True

    # b) 本段显式键全部生效（含配色 —— 桌面段自己就能配一套观感）
    c = DesktopOverlayConfig.from_dict(
        {"enabled": True, "mode": "latest", "anchor": "top_right", "offset": [10, -20],
         "pos": [5, 6], "size_px": [800, 300], "alpha": 0.75, "click_through": False,
         "follow": False, "game_title": "VRChat Desktop", "font_size": 28,
         "source_font_size": 20, "max_lines": 2, "show_source": True, "font": "X.ttf",
         "color_bg": [1, 2, 3], "bg_alpha": 128})
    assert c.enabled and c.mode == "latest" and c.anchor == "top_right", c
    assert c.offset == (10, -20) and c.pos == (5, 6), c
    assert c.size_px == (800, 300) and abs(c.alpha - 0.75) < 1e-9, c
    assert c.click_through is False and c.follow is False
    assert c.game_title == "VRChat Desktop"
    assert c.font_size == 28 and c.source_font_size == 20 and c.max_lines == 2
    assert c.show_source is True and c.font == "X.ttf"
    v = c.visual_config()
    assert v.size_px == (800, 300) and v.font_size == 28 and v.max_lines == 2
    assert v.color_bg == (1, 2, 3) and v.bg_alpha == 128, (v.color_bg, v.bg_alpha)

    # c) 垃圾值不许把整段炸掉（配置是用户手改的）
    c = DesktopOverlayConfig.from_dict({"offset": "nope", "size_px": [1, 2, 3],
                                        "alpha": None, "font_size": "big"})
    assert c.offset == (0, -48), c.offset          # 回落本段默认
    assert c.size_px == (1024, 360), c.size_px
    assert abs(c.alpha - 0.9) < 1e-9, c.alpha      # None 视作「没写」
    assert c.font_size == 36, c.font_size
    print("  from_dict 默认 / 本段显式键 / 垃圾值回落 OK")


def test_desktop_config_is_decoupled_from_overlay_section() -> None:
    """★ 桌面字幕与手腕屏的配置**完全独立**（2026-10-04 返工的核心，别再合回去）。

    三条判据：
      ① `from_dict()` 只接一个参数 —— 想传 `overlay:` 段都传不进去；
      ② 源码里不许再出现「读 overlay 段」的痕迹（`raw.get("overlay")` / `visual=`）；
      ③ 视觉默认值来自 OverlayConfig 的**代码默认值**，且桌面自己的默认尺寸/字号
         与手腕屏的默认值**本来就不同**（1024x360/36 vs 1024x440/36）——
         谁把继承接回来，这里立刻红。
    """
    import inspect

    from vlt.output.desktop_overlay import desktop_visual_defaults
    from vlt.output.overlay import OverlayConfig

    sig = inspect.signature(DesktopOverlayConfig.from_dict)
    assert list(sig.parameters) == ["d"], f"from_dict 又多出了参数：{list(sig.parameters)}"

    src = (ROOT / "vlt" / "output" / "desktop_overlay.py").read_text(encoding="utf-8")
    assert 'raw.get("overlay")' not in src, "热重载又把 overlay: 段读回来了（两段必须独立）"
    assert "visual=" not in src, "又长出「继承 overlay 段」的参数了"

    wrist = OverlayConfig()
    desk = desktop_visual_defaults()
    assert desk["color_bg"] == wrist.color_bg, (desk["color_bg"], wrist.color_bg)
    assert desk["bg_alpha"] == wrist.bg_alpha
    # 默认尺寸本来就不同 → 「独立」不是嘴上说说
    assert DesktopOverlayConfig.from_dict({}).size_px == (1024, 360)
    assert wrist.size_px == (1024, 440), wrist.size_px
    print("  配置独立（签名 / 源码 / 默认值三分）OK")


def test_backend_config() -> None:
    """`desktop_overlay.backend`：默认 auto、五个合法值都认、垃圾值回落，且**不从 overlay 段继承**。

    `overlay.backend`（auto/null）是手腕屏那套，语义完全不同 —— 继承过来会让桌面字幕
    跟着手腕屏的开关走，属于配置语义漂移。
    """
    from vlt.output.desktop_overlay import BACKENDS
    assert BACKENDS == ("auto", "native", "tk", "wayland", "x11"), BACKENDS
    assert DesktopOverlayConfig.from_dict({}).backend == "auto"
    for want in ("native", "tk", "wayland", "x11"):
        assert DesktopOverlayConfig.from_dict({"backend": want}).backend == want, want
    assert DesktopOverlayConfig.from_dict({"backend": "glx"}).backend == "auto"    # 垃圾值
    # 手腕屏的 `overlay.backend`（auto/null）与桌面无关：from_dict 只接本段（见独立配置用例）
    print("  backend 配置（默认 / 五个合法值 / 垃圾值回落 / 不继承）OK")


# ---------------------------------------------------------------- 原生窗接线


class _FakeNative:
    """假原生窗：只记录收到的调用（真协议由 tests/test_wayland_window.py 验）。"""

    available = True

    def __init__(self) -> None:
        self.panels: list[tuple] = []
        self.moves: list[tuple[int, int]] = []
        self.sizes: list[tuple[int, int]] = []
        self.alphas: list[float] = []
        self.drag: list[bool] = []
        self.ticks = 0
        self.closed = 0
        self.position = (11, 22)

    def set_panel(self, image, alpha=1.0):  # noqa: ANN001
        self.panels.append((tuple(image.size), float(alpha)))

    def move(self, x, y):  # noqa: ANN001
        self.moves.append((int(x), int(y)))
        self.position = (int(x), int(y))

    def set_size(self, size):  # noqa: ANN001
        self.sizes.append(tuple(int(v) for v in size))

    def set_alpha(self, alpha):  # noqa: ANN001
        self.alphas.append(float(alpha))

    def set_click_through(self, on):  # noqa: ANN001
        pass

    def set_draggable(self, on):  # noqa: ANN001
        self.drag.append(bool(on))

    def tick(self) -> None:
        self.ticks += 1

    def close(self) -> None:
        self.closed += 1


def test_native_window_wiring() -> None:
    """backend=native 时共享模块走原生窗：贴图/移动/穿透/透明度/尺寸/关闭全部透传。"""
    import vlt.platform as platform

    fake = _FakeNative()
    made: list[dict] = []

    def _fake_create(**kw):  # noqa: ANN003
        made.append(kw)
        return fake

    real = platform.create_desktop_window
    platform.create_desktop_window = _fake_create
    tmp = Path(tempfile.mkdtemp(prefix="vlt-native-cfg-"))
    cfgp = tmp / "config.yaml"
    cfgp.write_text("desktop_overlay:\n  size_px: [200, 100]\n", encoding="utf-8")
    try:
        cfg = DesktopOverlayConfig(enabled=True, backend="native", attach_to_game=False,
                                   anchor="free", pos=(33, 44), size_px=(200, 100),
                                   alpha=0.8, click_through=True)
        ov = DesktopOverlay(cfg, config_path=cfgp)
        assert ov.start() is True
        assert made and made[0]["backend"] == "native", made
        assert callable(made[0]["on_drag_end"]), "拖动回调没接线"
        assert fake.moves and fake.moves[-1] == (33, 44), fake.moves
        assert fake.panels and fake.panels[0][1] == 0.8, fake.panels
        assert ov.position == (33, 44)

        ov.tick()
        assert fake.ticks >= 1, "原生窗的 tick 没被泵"
        ov.set_draggable(True)
        assert fake.drag == [True], fake.drag
        ov.set_alpha(0.5)
        assert fake.alphas and abs(fake.alphas[-1] - 0.5) < 1e-9, fake.alphas

        # 热重载改尺寸 → 原生窗要跟着 set_size（不是只重渲染一张更大的图）
        cfgp.write_text("desktop_overlay:\n  size_px: [220, 110]\n", encoding="utf-8")
        ov._reload_config()
        assert fake.sizes == [(220, 110)], fake.sizes

        # 拖动落点 → 折算成配置（与 Tk 那条腿同一套逻辑）
        ov._native_drag_end(120, 130)
        assert ov.user_pos == (120, 130)

        ov.close()
        assert fake.closed == 1, fake.closed
        ov.close()                      # 幂等
        assert fake.closed == 1
    finally:
        platform.create_desktop_window = real
    print("  原生窗接线：建窗/移动/贴图/拖动/透明度/热重载尺寸/关闭 OK")


def test_native_backend_falls_back_to_tk() -> None:
    """backend=native 但门面给不出原生窗 → 回落 Tk（仍能起来，后端自己留了原因）。"""
    import vlt.platform as platform

    root, _tk, why = _try_tk()
    if root is None:
        print(f"  跳过（无 Tk）：{why}")
        return
    real = platform.create_desktop_window
    platform.create_desktop_window = lambda **kw: None       # noqa: ARG005
    try:
        cfg = DesktopOverlayConfig(enabled=True, backend="native", attach_to_game=False,
                                   anchor="free", pos=(10, 10), size_px=(200, 100))
        ov = DesktopOverlay(cfg, root=root)
        assert ov.start() is True
        assert ov._native is None and ov.available
        ov.close()
    finally:
        platform.create_desktop_window = real
    print("  backend=native 回落 Tk OK")


# ---------------------------------------------------------------- 6. dry-run 出图
def test_dry_run_renders_frames() -> None:
    cfg = DesktopOverlayConfig(enabled=True, mode="conversation", size_px=(640, 240),
                               font_size=30, source_font_size=22, max_lines=3)
    ov = DesktopOverlay(cfg, dry_run=True)
    # 契约钉死的默认落盘目录
    assert ov._frames_dir == ROOT / "out" / "desktop_overlay_frames", ov._frames_dir
    with tempfile.TemporaryDirectory() as tmp:
        ov._frames_dir = Path(tmp)      # 指到临时目录，别把仓库 out/ 弄脏
        assert ov.start() is True
        n0 = ov.frame_count
        ov.update_entries(SAMPLE_ENTRIES)
        ov.update_entries(SAMPLE_ENTRIES)          # 同样内容不重复出图（省 CPU）
        assert ov.frame_count == n0 + 1, (n0, ov.frame_count)
        ov.update_entries(SAMPLE_ENTRIES, force=True)
        assert ov.frame_count == n0 + 2, (n0, ov.frame_count)
        # 空内容不清屏：不许出「空白帧」把上一句盖掉
        ov.update_entries([])
        ov.update("", "")
        assert ov.frame_count == n0 + 2, ov.frame_count

        files = sorted(Path(tmp).glob("frame_*.png"))
        assert files, "dry-run 没有落盘任何 PNG"
        assert files[-1].name == f"frame_{n0 + 2:03d}.png", files[-1].name
        img = Image.open(files[-1])
        img.load()
        assert img.size == cfg.size_px, img.size
        colors = img.getcolors(maxcolors=1_000_000)
        assert colors and len(colors) > 3, f"图里只有底色（{len(colors or [])} 种颜色）"
        # 圆角外那一圈必须是色键色（Tk 靠它抠透明）—— 贴错底色 = 一整块实心矩形
        assert img.convert("RGB").getpixel((0, 0)) == KEY_RGB, img.getpixel((0, 0))
        ov.close()
        ov.close()                      # 幂等
    print("  dry-run 出图（帧数 / 落盘 / 尺寸 / 非底色像素 / 色键角）OK")


def test_mode_dispatch_renders_differently() -> None:
    """mode 必须真的换渲染器：conversation=对话视图，latest=单句面板。"""
    imgs = {}
    for mode in ("conversation", "latest"):
        cfg = DesktopOverlayConfig(enabled=True, mode=mode, size_px=(640, 240))
        ov = DesktopOverlay(cfg, dry_run=True)
        with tempfile.TemporaryDirectory() as tmp:
            ov._frames_dir = Path(tmp)
            assert ov.start() is True
            ov.update_entries(SAMPLE_ENTRIES)
            files = sorted(Path(tmp).glob("frame_*.png"))
            assert files, f"mode={mode} 没出图"
            imgs[mode] = Image.open(files[-1]).convert("RGB")
            ov.close()
    a, b = imgs["conversation"], imgs["latest"]
    assert a.size == b.size == (640, 240)
    assert a.tobytes() != b.tobytes(), "两种 mode 出了一样的图（mode 没生效）"
    print("  mode 分发（conversation / latest）OK")


# ---------------------------------------------------------------- 7/8. Tk 冒烟 + 无目标窗口
def _try_tk():
    """建一个 withdraw 的 Tk root；建不了（Linux 无显示器等）返回 (None, None, 原因)。"""
    try:
        import tkinter as tk
    except Exception as exc:  # noqa: BLE001
        return None, None, f"没有 tkinter：{type(exc).__name__}: {exc}"
    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001 — TclError：无显示器 / 无 X
        return None, tk, f"{type(exc).__name__}: {exc}"
    root.withdraw()
    return root, tk, None


def test_start_without_game_window() -> None:
    """CI 上没有 VRChat：不许抛异常，start() 仍要成功（退化成绝对定位）。"""
    cfg = DesktopOverlayConfig(enabled=True, attach_to_game=True, backend="tk",
                               game_title="___vlt_不存在的窗口___",
                               size_px=(320, 120), pos=(40, 40))
    with tempfile.TemporaryDirectory() as tmp:
        ov = DesktopOverlay(cfg, dry_run=True)
        ov._frames_dir = Path(tmp)
        assert ov.start() is True, "dry-run 起不来"
        assert ov.game_rect is None
        assert ov.position == (40, 40), ov.position
        ov.tick()                       # 没起窗/没内容也不许炸
        ov.close()

    root, _tk, why = _try_tk()
    if root is None:
        print(f"  跳过（无 Tk）：{why}")
        return
    try:
        ov = DesktopOverlay(cfg, root=root)
        assert ov.start() is True, "有 Tk 却起不来"
        root.update()
        assert ov.game_rect is None, ov.game_rect
        assert ov.position == (40, 40), ov.position
        ov.update_entries(SAMPLE_ENTRIES)
        ov.tick()
        root.update()
        assert ov.frame_count >= 2, ov.frame_count
        ov.close()
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    print("  找不到目标窗口：start() 仍成功 + 退化绝对定位 OK")


def test_tk_smoke_full_cycle() -> None:
    """建窗 → 出图 → tick → 拖动开关 → 透明度 → 关闭幂等，全程不抛。"""
    root, _tk, why = _try_tk()
    if root is None:
        print(f"  跳过（无 Tk）：{why}")
        return
    try:
        cfg = DesktopOverlayConfig(enabled=True, mode="conversation", backend="tk",
                                   attach_to_game=False, anchor="free",
                                   pos=(30, 30), size_px=(420, 180), alpha=0.9)
        ov = DesktopOverlay(cfg, root=root)
        assert ov.start() is True
        assert ov.available is True
        root.update()
        ov.update_entries(SAMPLE_ENTRIES, force=True)
        ov.tick()
        root.update()
        assert ov.frame_count >= 2, ov.frame_count     # start 空面板 1 帧 + 内容 1 帧
        assert ov.position == (30, 30), ov.position
        # 贴图真的挂到 Label 上了（PhotoImage 被 GC 的话这里会是空串 → 窗口一片空白）
        assert str(ov._label.cget("image")), "Label 上没有 image（贴图被 GC 了？）"
        ov.set_draggable(True)
        ov.set_draggable(False)
        ov.set_alpha(0.6)
        root.update()
        assert abs(ov.cfg.alpha - 0.6) < 1e-9, ov.cfg.alpha
        assert abs(float(ov._win.attributes("-alpha")) - 0.6) < 0.02, \
            ov._win.attributes("-alpha")
        ov.close()
        ov.close()                      # 幂等
        ov.tick()                       # 关窗之后 tick / update 都不许炸
        ov.update_entries(SAMPLE_ENTRIES)
        ov.tick()
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    print("  Tk 冒烟：建窗 / 出图 / 拖动 / 透明度 / 关闭幂等 OK")


def test_attach_and_follow_fake_game_window() -> None:
    """贴到（假）游戏窗口上并跟随移动。真 VRChat 只能在真机上验。"""
    root, tk, why = _try_tk()
    if root is None:
        print(f"  跳过（无 Tk）：{why}")
        return
    fake = None
    try:
        fake = tk.Toplevel(root)
        fake.title(FAKE_TITLE)
        fake.geometry("500x300+40+40")
        fake.configure(bg="#204060")
        fake.update()                    # 真的映射出来，EnumWindows 才看得见
        cfg = DesktopOverlayConfig(enabled=True, attach_to_game=True, backend="tk",
                                   follow=True,
                                   anchor="bottom_center", offset=(0, -40),
                                   size_px=(320, 120), game_title=FAKE_TITLE)
        ov = DesktopOverlay(cfg, root=root)
        assert ov.start() is True
        root.update()
        rect = ov.game_rect
        if rect is None:
            # 平台门面没有找窗口的能力（裁剪过的构建 / 后端不可用）→ 跟随不在这里验
            print("  跳过跟随断言（本平台没有桌面窗口后端）")
            ov.close()
            return
        want = compute_position("bottom_center", (0, -40), rect, (320, 120),
                                platform.screen_work_area())
        got = ov.position
        assert abs(got[0] - want[0]) <= 4 and abs(got[1] - want[1]) <= 4, \
            f"位置={got} 期望={want}"
        # 移动游戏窗口 → tick() 必须跟着走，位移量等于窗口位移量
        before = ov.position
        fake.geometry("500x300+180+100")
        fake.update()
        root.update()
        ov.tick()
        root.update()
        after = ov.position
        assert after != before, f"窗口移动后字幕没跟：{before} -> {after}"
        assert abs((after[0] - before[0]) - 140) <= 4, (before, after)
        assert abs((after[1] - before[1]) - 60) <= 4, (before, after)
        # 关掉跟随 → 窗口再动，字幕不许动
        ov.cfg.follow = False
        fake.geometry("500x300+40+40")
        fake.update()
        root.update()
        ov.tick()
        root.update()
        assert ov.position == after, (ov.position, after)
        ov.close()
    finally:
        if fake is not None:
            try:
                fake.destroy()
            except Exception:  # noqa: BLE001
                pass
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    print("  贴窗 + 跟随移动 + follow=False 不动 OK")


def test_work_area_follows_attached_monitor() -> None:
    """`_work_area()` 的选型：贴窗 → **目标窗口那块屏**；自由模式 → 主屏。

    本机不一定接了副屏，所以用「把门面的 `monitor_work_area` 临时换成一块假的左侧副屏」
    来钉住选型与夹取（真 Win32 那一条由 `test_win32_helpers_on_real_windows` 覆盖）。
    换错选型的表现非常具体：副屏上的字幕被主屏工作区夹回主屏，用户看着它"自己跳屏"。
    """
    root, tk, why = _try_tk()
    if root is None:
        print(f"  跳过（无 Tk）：{why}")
        return
    real_monitor = platform.monitor_work_area
    seen: list[int] = []

    def fake_monitor(hwnd):                      # 假装目标窗口在左侧副屏上
        seen.append(hwnd)
        return AREA_LEFT

    fake = None
    try:
        fake = tk.Toplevel(root)
        fake.title(FAKE_TITLE)
        fake.geometry("500x300+40+40")
        fake.update()
        cfg = DesktopOverlayConfig(enabled=True, attach_to_game=True, follow=False,
                                   backend="tk",
                                   anchor="bottom_center", offset=(0, -40),
                                   size_px=(320, 120), game_title=FAKE_TITLE)
        ov = DesktopOverlay(cfg, root=root)
        assert ov.start() is True
        root.update()
        if ov.game_rect is None:
            print("  跳过（本平台没有桌面窗口后端）")
            ov.close()
            return

        platform.monitor_work_area = fake_monitor
        try:
            # ① 贴窗 → 按目标窗口那块屏取工作区（句柄要传对）
            got = ov._work_area()
            assert got == AREA_LEFT, f"贴窗时应按目标窗口那块屏取工作区：{got}"
            assert seen == [ov._game_hwnd], f"问工作区时该带上目标窗口句柄：{seen}"

            # ② 目标窗口在副屏上 → 落点留在副屏（x 为负），没被主屏工作区拽回来。
            #    这里只验「_apply_position 用的那条解析链」，不真把窗口挪到屏幕外 ——
            #    屏外坐标能不能落下去取决于 OS/显示器排布，拿它做断言会假红。
            ov._game_rect = (-1800, 100, -1100, 700)
            want = resolve_position(ov.cfg, ov._game_rect, (320, 120), ov._work_area())
            assert want == compute_position("bottom_center", ov.cfg.offset, ov._game_rect,
                                            (320, 120), AREA_LEFT), want
            assert want == (-1610, 540), want
            assert want[0] < 0, f"字幕被拽回主屏了：{want}"
            yanked = resolve_position(ov.cfg, ov._game_rect, (320, 120), AREA)
            assert yanked[0] >= 0 and yanked != want, (yanked, want)

            # ③ 没有目标窗口句柄（自由模式 / 找不到窗口）→ 沿用主屏工作区，不去问 monitor
            seen.clear()
            ov._game_hwnd, ov._game_rect = None, None
            ov.cfg.attach_to_game, ov.cfg.anchor, ov.cfg.pos = False, "free", (10, 20)
            got = ov._work_area()
            assert seen == [], f"没有句柄就不该去问 monitor_work_area：{seen}"
            assert got == platform.screen_work_area(), f"自由模式应沿用主屏工作区：{got}"
            ov._apply_position()
            root.update()
            assert ov.position == (10, 20), ov.position
        finally:
            platform.monitor_work_area = real_monitor
        ov.close()
    finally:
        platform.monitor_work_area = real_monitor
        if fake is not None:
            try:
                fake.destroy()
            except Exception:  # noqa: BLE001
                pass
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    print("  工作区选型：贴窗按目标窗口那块屏 / 自由模式沿用主屏 OK")


# ---------------------------------------------------------------- 平台门面 / 隔离
def test_facade_safe_defaults_without_backend() -> None:
    """桌面窗口后端的「有 / 无」两态：缺后端时门面必须返回安全默认值，一个都不许抛。

    Linux 侧从 X11 后端（`vlt/platform/linux.py` 的桌面窗口一节）落地后，探针
    应该能认出它；Windows 侧认 `win.py`。「缺后端」的那一半用猴子补丁假装出来，
    两条路径都要验。
    """
    if platform.IS_WINDOWS:
        from vlt.platform import win as w
        assert platform.desktop_window_backend() is w
    elif platform.IS_LINUX:
        from vlt.platform import linux as lx
        assert platform.desktop_window_backend() is lx
    else:
        assert platform.desktop_window_backend() is None

    real = platform.desktop_window_backend
    platform.desktop_window_backend = lambda: None     # 假装本平台没有桌面窗口能力
    try:
        assert platform.find_game_window("VRChat") is None
        assert platform.window_client_rect(1234) is None
        assert platform.is_window(1234) is False
        assert platform.set_click_through(1234, True) is False
        assert platform.set_tool_window(1234) is False
        assert platform.set_window_shape(1234, b"\x00" * 25, 200, 100) is False   # 蒙版同样兜底
        assert platform.top_level_hwnd(4321) == 4321
        assert platform.screen_work_area() == (0, 0, 1920, 1080)
        assert platform.monitor_work_area(1234) == (0, 0, 1920, 1080)
        assert platform.create_desktop_window((320, 120)) is None    # 没有原生窗 → 回落 Tk
    finally:
        platform.desktop_window_backend = real
    print("  门面兜底（没有桌面窗口后端时的安全默认值）OK")


def test_create_desktop_window_refuses_in_test_process() -> None:
    """测试进程里**不许**真的去连用户的合成器建原生窗（与虚拟声卡同一道防呆）。

    实测踩过：旧的 GUI 用例在 Wayland 机器上会直接连用户正在用的 niri 弹出一块
    layer 面。真协议验收（tests/test_wayland_window.py）直连**私有嵌套合成器**、
    不走这个入口，所以这里可以一刀切拒绝。
    """
    if not platform.IS_LINUX:
        print("  跳过（非 Linux：Windows 走 Tk，没有原生桌面窗）")
        return
    from vlt.platform import linux as lx
    assert lx.create_desktop_window((64, 64)) is None, "测试进程居然建出了原生窗！"
    print("  测试进程拒绝建原生窗（回落 Tk）OK")


def test_monitor_work_area_falls_back_to_primary() -> None:
    """多屏那条能力的兜底：没有句柄 / 句柄失效 / 后端没这项能力 → 回落主屏工作区。

    `_work_area()` 在 50ms 一跳的 tick 里，任何异常冒出去都会打断整条翻译腿，
    所以这里只要求「永远给一个能用的矩形」，绝不要求它一定拿得到副屏那一份。
    """
    primary = platform.screen_work_area()
    assert len(primary) == 4 and primary[2] > primary[0] and primary[3] > primary[1], primary
    # 没有句柄（自由模式）→ 主屏工作区
    assert platform.monitor_work_area(0) == primary, platform.monitor_work_area(0)
    assert platform.monitor_work_area(None) == primary
    # 失效句柄 → 回落，不抛
    assert platform.monitor_work_area(0x7FFFFFFF) == primary

    # ⚠️ 门面**不许**对坐标做 `max(0, ...)` 之类的钳制：副屏在主屏左侧/上方时坐标本来就是
    # 负的，钳一下就把副屏工作区改成错的（字幕被夹回主屏）。用打桩后端喂一份负坐标，要求
    # **原样**返回 —— 这条是审查时补的：此前把钳制写进门面 `monitor_work_area()`，
    # 全量用例照样全绿（故障注入实测 M4），等于没人守这条不变量。
    class _FakeNegativeBackend:
        @staticmethod
        def monitor_work_area(hwnd: int) -> tuple[int, int, int, int]:  # noqa: ARG004
            return (-1920, 0, 0, 1080)

    real = platform.desktop_window_backend
    platform.desktop_window_backend = lambda: _FakeNegativeBackend()
    try:
        got = platform.monitor_work_area(1234)
        assert got == (-1920, 0, 0, 1080), f"门面把副屏的负坐标钳掉了：{got}"
    finally:
        platform.desktop_window_backend = real

    real = platform.desktop_window_backend
    platform.desktop_window_backend = lambda: None     # 假装本平台没有这项能力（Linux）
    try:
        assert platform.monitor_work_area(0) == (0, 0, 1920, 1080)
        assert platform.monitor_work_area(1234) == (0, 0, 1920, 1080)
    finally:
        platform.desktop_window_backend = real
    print(f"  monitor_work_area 无句柄/失效句柄/无后端都回落主屏 {primary} OK")


def test_shared_module_stays_platform_clean() -> None:
    """共享模块里不许出现 Win32 调用 / 平台独占 import（隔离门禁的源码级断言）。"""
    src = (ROOT / "vlt" / "output" / "desktop_overlay.py").read_text(encoding="utf-8")
    for bad in ("import ctypes", "ctypes.", "windll", "user32",
                "platform.win", "platform import win",
                "EnumWindows", "GetWindowLong", "SetWindowLong",
                "libwayland", "libX11", "zwlr_"):     # 原生后端只能走门面
        assert bad not in src, f"desktop_overlay.py 里出现了平台独占字样：{bad!r}"
    for name in ("desktop_window_backend", "find_game_window", "window_client_rect",
                 "is_window", "set_click_through", "set_tool_window", "set_window_shape",
                 "top_level_hwnd", "screen_work_area", "monitor_work_area",
                 "create_desktop_window"):
        assert callable(getattr(platform, name, None)), f"platform 门面缺 {name}"
    if platform.IS_WINDOWS:
        from vlt.platform import win as w
        for name in ("top_level_hwnd", "find_window_by_title", "window_client_rect",
                     "is_window", "set_click_through", "set_tool_window",
                     "screen_work_area", "monitor_work_area"):
            assert callable(getattr(w, name, None)), f"win.py 缺 {name}"
    print("  平台隔离 + 门面 / win 实现齐备 OK")


def test_win32_helpers_on_real_windows() -> None:
    """Windows 专属：找窗口 / 客户区 / 扩展样式真的生效（别的平台跳过）。"""
    if not platform.IS_WINDOWS:
        print("  跳过（非 Windows）")
        return
    import ctypes

    from vlt.platform import win as w

    area = platform.screen_work_area()
    assert len(area) == 4 and area[2] > area[0] and area[3] > area[1], area
    assert platform.is_window(0) is False
    assert platform.find_game_window("") is None
    assert platform.find_game_window("___没有这个窗口___") is None

    root, tk, why = _try_tk()
    if root is None:
        print(f"  跳过（无 Tk）：{why}")
        return

    def exstyle(hwnd: int) -> int:
        u = ctypes.windll.user32
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        return int(u.GetWindowLongPtrW(ctypes.c_void_p(hwnd), -20)) & 0xFFFFFFFF

    fake = None
    single_monitor = True
    mon_area: tuple[int, int, int, int] | None = None
    try:
        fake = tk.Toplevel(root)
        fake.title(FAKE_TITLE)
        fake.geometry("300x200+60+60")
        fake.update()
        kid = int(fake.winfo_id())
        hwnd = platform.top_level_hwnd(kid)
        assert hwnd != kid, "顶层句柄应该是 winfo_id() 的父窗口"
        assert platform.is_window(hwnd) is True
        rect = platform.window_client_rect(hwnd)
        assert rect is not None and rect[2] - rect[0] > 200, rect
        # 多屏：按**窗口所在显示器**取工作区（`screen_work_area()` 只有主屏那一份）
        mon_area = platform.monitor_work_area(hwnd)
        assert len(mon_area) == 4 and mon_area[2] > mon_area[0] \
            and mon_area[3] > mon_area[1], mon_area
        assert mon_area[0] < rect[2] and rect[0] < mon_area[2] \
            and mon_area[1] < rect[3] and rect[1] < mon_area[3], \
            f"工作区 {mon_area} 与窗口客户区 {rect} 不相交（拿错屏了）"
        single_monitor = int(ctypes.windll.user32.GetSystemMetrics(80)) <= 1  # SM_CMONITORS
        if single_monitor:
            assert mon_area == area, f"单屏时应等于主屏工作区：{mon_area} vs {area}"
        assert w.monitor_work_area(0) == w.screen_work_area()
        # 大小写不敏感
        assert platform.find_game_window(FAKE_TITLE.lower()) == hwnd
        # 精确同名优先于「标题里也含这个子串」的窗口（本程序主窗标题就叫「VRChat 实时同传」）
        decoy = tk.Toplevel(root)
        decoy.title(FAKE_TITLE + " decoy")
        decoy.geometry("200x150+400+300")
        decoy.update()
        assert platform.find_game_window(FAKE_TITLE) == hwnd, \
            "精确同名没优先（会贴到标题只是含子串的窗口上）"
        decoy.destroy()

        # 鼠标穿透：读改写之后扩展样式真的变了，且 WS_EX_LAYERED 不被冲掉
        assert platform.set_click_through(hwnd, True) is True
        ex = exstyle(hwnd)
        assert ex & w._WS_EX_TRANSPARENT, f"穿透没打开：0x{ex:08X}"
        assert ex & w._WS_EX_LAYERED, f"WS_EX_LAYERED 被冲掉了：0x{ex:08X}"
        assert platform.set_click_through(hwnd, False) is True
        ex = exstyle(hwnd)
        assert not (ex & w._WS_EX_TRANSPARENT), f"穿透没关掉：0x{ex:08X}"
        assert ex & w._WS_EX_LAYERED, f"关穿透时把 WS_EX_LAYERED 也摘了：0x{ex:08X}"
        assert platform.set_tool_window(hwnd) is True
        ex = exstyle(hwnd)
        assert ex & w._WS_EX_NOACTIVATE and ex & w._WS_EX_TOOLWINDOW, f"0x{ex:08X}"
        # 失效句柄：不许抛，返回安全值
        assert platform.window_client_rect(0x7FFFFFFF) is None
        fake.destroy()
        assert platform.is_window(hwnd) is False, "销毁后 is_window 应返回 False"
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    note = ("本机只有一块屏 → 副屏（负坐标）那一份只能人工验"
            if single_monitor else f"本机多屏，按窗口取到 {mon_area}")
    print(f"  Win32：找窗口 / 客户区 / 穿透 / 工具窗 / 按显示器取工作区 OK（{note}）")


def test_click_through_toggles_with_drag() -> None:
    """字幕窗自己的穿透开关：set_draggable(True) 关穿透、(False) 恢复。"""
    if not platform.IS_WINDOWS:
        print("  跳过（非 Windows）")
        return
    import ctypes

    from vlt.platform import win as w

    def exstyle(hwnd: int) -> int:
        u = ctypes.windll.user32
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        return int(u.GetWindowLongPtrW(ctypes.c_void_p(hwnd), -20)) & 0xFFFFFFFF

    root, _tk, why = _try_tk()
    if root is None:
        print(f"  跳过（无 Tk）：{why}")
        return
    try:
        cfg = DesktopOverlayConfig(enabled=True, attach_to_game=False, backend="tk",
                                   size_px=(320, 120), pos=(20, 20))
        ov = DesktopOverlay(cfg, root=root)
        assert ov.start() is True
        root.update()
        hwnd = ov._hwnd                 # 内部属性：本用例就是要验 Win32 扩展样式
        assert hwnd and platform.is_window(hwnd), hwnd
        ex = exstyle(hwnd)
        assert ex & w._WS_EX_TRANSPARENT, f"鼠标穿透默认应打开：0x{ex:08X}"
        assert ex & w._WS_EX_NOACTIVATE and ex & w._WS_EX_TOOLWINDOW, \
            f"应不抢焦点、不进 alt-tab：0x{ex:08X}"
        ov.set_draggable(True)
        root.update()
        assert not (exstyle(hwnd) & w._WS_EX_TRANSPARENT), "解锁拖动后鼠标穿透应关闭"
        assert exstyle(hwnd) & w._WS_EX_LAYERED, "关穿透时不许把 WS_EX_LAYERED 冲掉"
        ov.set_draggable(False)         # 锁定后穿透应立刻恢复
        root.update()
        assert exstyle(hwnd) & w._WS_EX_TRANSPARENT, "锁定后鼠标穿透应恢复"
        ov.set_alpha(0.6)               # 改透明度后穿透要补回来（Tk 会重写扩展样式）
        ov.tick()                       # 补回发生在下一跳 tick（GUI 每 50ms 一跳）
        root.update()
        assert exstyle(hwnd) & w._WS_EX_TRANSPARENT, \
            f"改透明度后鼠标穿透丢了：0x{exstyle(hwnd):08X}"
        assert abs(float(root.tk.call("wm", "attributes", ov._win._w, "-alpha")) - 0.6) < 0.01, \
            "透明度没有真的改到 0.6"
        ov.close()
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    print("  字幕窗鼠标穿透 ↔ 拖动开关 OK")


def test_transparent_key_matches_key_rgb() -> None:
    """色键字符串与 RGB 底色必须是同一个颜色（贴错底 = 一整块实心矩形）。"""
    assert tuple(int(TRANSPARENT_KEY[i:i + 2], 16) for i in (1, 3, 5)) == KEY_RGB
    print("  色键常量自洽 OK")


def test_drag_moves_window_and_snaps_to_anchor() -> None:
    """拖动：解锁后才跟鼠标走，落点必须折算成 `(锚点, 偏移)` 而不是死坐标。

    这里直接调事件处理函数（鸭子类型的假事件）：Tk 的 `event_generate` 在 CI 上
    不可靠；真鼠标那一路由主控的复验脚本在真窗口上验。
    """
    root, tk, why = _try_tk()
    if root is None:
        print(f"  跳过（无 Tk）：{why}")
        return
    fake = None
    try:
        fake = tk.Toplevel(root)
        fake.title(FAKE_TITLE)
        fake.geometry("500x300+40+40")
        fake.update()
        cfg = DesktopOverlayConfig(enabled=True, attach_to_game=True, backend="tk",
                                   follow=True,
                                   anchor="bottom_center", offset=(0, -40),
                                   size_px=(320, 120), game_title=FAKE_TITLE)
        ov = DesktopOverlay(cfg, root=root)
        assert ov.start() is True
        root.update()
        rect = ov.game_rect
        if rect is None:
            print("  跳过拖动断言（本平台没有桌面窗口后端）")
            ov.close()
            return

        class _Ev:                                  # 够用的假事件
            def __init__(self, x: int, y: int) -> None:
                self.x_root, self.y_root = x, y

        start = ov.position
        # ① 锁定时按下去不该动（穿透开着，真实鼠标事件本来也到不了窗口）
        ov._on_drag_start(_Ev(start[0] + 5, start[1] + 5))
        ov._on_drag_move(_Ev(start[0] + 105, start[1] + 65))
        assert ov.position == start, f"锁定状态下不该跟鼠标走：{ov.position} != {start}"
        assert ov._drag_from is None, "锁定状态不该记录拖动起点"

        # ② 解锁后：按下 → 移动 → 抬起，窗口位移 == 鼠标位移
        ov.set_draggable(True)
        ov._on_drag_start(_Ev(start[0] + 5, start[1] + 5))
        assert ov._drag_from == (5, 5), ov._drag_from
        ov._on_drag_move(_Ev(start[0] + 5 + 120, start[1] + 5 + 60))
        root.update()                               # 让 Tk 真的把窗口挪过去
        moved = ov.position
        assert moved == (start[0] + 120, start[1] + 60), f"{start} → {moved}"
        ov._on_drag_end(_Ev(0, 0))
        assert ov._drag_from is None and ov.user_pos == moved, ov.user_pos

        # ③ 落点折算成配置，且折算回去必须还原同一个位置（否则「锁定」后窗口会跳）
        changed = ov.snap_to_config()
        assert changed, "贴窗时拖动落点必须折算成 (锚点, 偏移)"
        assert changed["anchor"] in ANCHORS and changed["anchor"] != "free", changed
        assert list(changed["offset"]) == list(ov.cfg.offset), changed
        back = compute_position(ov.cfg.anchor, ov.cfg.offset, rect, (320, 120),
                                platform.screen_work_area())
        assert back == moved, f"折算后回到 {back}，实际落点 {moved}"

        # ④ 拖动结束后目标窗口再动，字幕仍按"贴着窗口"的方式跟着走（不是弹回旧锚点）
        ov.set_draggable(False)
        fake.geometry("500x300+120+90")
        fake.update()
        ov.tick()
        # 期望值走生产公式，且**不能**假设"字幕位移量 == 窗口位移量"：贴边/近屏边缘时
        # `compute_position` 会把落点夹回可用区（通用行为，与屏幕大小无关 ——
        # run_tests.py 已把 xvfb 虚拟屏显式钉成 1920x1080，别依赖发行版默认）。
        want = compute_position(ov.cfg.anchor, ov.cfg.offset, ov.game_rect, (320, 120),
                                platform.screen_work_area())
        assert abs(ov.position[0] - want[0]) <= 4 and abs(ov.position[1] - want[1]) <= 4, \
            f"跟随位置不对：{ov.position} 期望≈{want}"
        ov.close()
    finally:
        if fake is not None:
            try:
                fake.destroy()
            except Exception:  # noqa: BLE001
                pass
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    print("  拖动：解锁才跟手 / 落点折算成锚点+偏移 / 跟随不弹回 OK")


def test_best_anchor_picks_nearest_grid_point() -> None:
    """落点 → 最近锚点的纯函数：四个角 + 中心，以及"同一个位置能还原"。"""
    from vlt.output.desktop_overlay import best_anchor
    assert best_anchor((100, 50), RECT, SIZE) == ("top_left", (0, 0))
    assert best_anchor((500, 50), RECT, SIZE) == ("top_right", (0, 0))
    assert best_anchor((100, 450), RECT, SIZE) == ("bottom_left", (0, 0))
    assert best_anchor((500, 450), RECT, SIZE) == ("bottom_right", (0, 0))
    assert best_anchor((300, 250), RECT, SIZE) == ("center", (0, 0))
    anchor, offset = best_anchor((330, 430), RECT, SIZE)
    assert (anchor, offset) == ("bottom_center", (30, -20)), (anchor, offset)
    # 任意落点：折算回去必须落在原坐标（±1px，整数除法的取整误差）
    # ⚠️ 只列**屏幕内**的落点：屏幕外的点会被 `_clamp_into_area` 拉回来（那是另一条
    #    用例在管的行为），拿它做往返断言会假红。
    for pos in ((120, 60), (860, 640), (300, 400), (0, 0), (900, 50)):
        anchor, offset = best_anchor(pos, RECT, SIZE)
        got = compute_position(anchor, offset, RECT, SIZE, (0, 0, 4096, 2160))
        assert abs(got[0] - pos[0]) <= 1 and abs(got[1] - pos[1]) <= 1, \
            f"{pos} → {anchor}{offset} → {got}"
    print("  落点折算最近锚点 OK")


def test_own_window_excluded_from_game_search() -> None:
    """本程序主窗标题也含 "VRChat"：找游戏窗口时必须能把**自己**排除掉。

    实测：不排除时，VRChat 没起来的情况下字幕会贴到本程序自己的窗口下沿
    （日志里明明白白写着「已贴到 'VRChat'」，其实贴的是我们自己）。
    """
    root, tk, why = _try_tk()
    if root is None:
        print(f"  跳过（无 Tk）：{why}")
        return
    if platform.desktop_window_backend() is None:
        print("  跳过（本平台没有桌面窗口后端）")
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
        return
    mine = None
    try:
        mine = tk.Toplevel(root)
        mine.title("VRChat 实时同传")          # 与真实主窗标题一致
        mine.geometry("300x200+30+30")
        mine.update()
        own = platform.top_level_hwnd(mine.winfo_id())
        hit = platform.find_game_window("VRChat")
        if hit != own:
            # ⚠️ 前提不成立就**跳过**，不许断言失败：本机真开着 VRChat（标题精确匹配）时，
            #    枚举顺序上它可能排在我们前面 → 这条前提恒假，但那是环境不是代码 bug。
            #    真正要钉的是下面那条（排除自己之后不许再命中自己），它不依赖这个前提。
            print(f"  跳过（本机另有窗口精确匹配 \"VRChat\"，hwnd={hit}）")
            return
        hit2 = platform.find_game_window("VRChat", (own,))
        assert hit2 != own, "排除自己之后仍然命中了自己 → VRChat 没开时会贴错窗"
    finally:
        if mine is not None:
            try:
                mine.destroy()
            except Exception:  # noqa: BLE001
                pass
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    print("  找游戏窗口时排除本程序自己的窗口 OK")


# ---------------------------------------------------------------- 界面：透明度滑块
def _make_gui_sandbox() -> None:
    """把模板改成**沙箱**配置：手腕屏 `overlay.alpha=0.5`、桌面段自己写 `alpha: 0.65`、
    目标窗口标题指向一个一定不存在的窗口。

    用途：证明**两个面板的配置互不影响** —— 桌面字幕的滑块与窗口只认
    `desktop_overlay:` 段（0.65），绝不是手腕屏那段的 0.5（返工前正是「视觉参数继承
    `overlay:` 段」，这里就是那条口径的回归守卫）。
    用行级替换而不是整文件重写：沙箱要跟用户手写配置一样保留注释，
    后面「落盘不丢东西」那几条验的才是真实场景。

    ⚠️ 全程只读写 `out/` 下的沙箱：仓库根的 `config.yaml` 是被 gitignore 的用户真实
    个人配置，本用例绝不碰它（也不备份/还原它）。
    """
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")

    # ① overlay 段补一行**顶层** alpha（手腕屏的）—— 桌面字幕绝不该读它
    text, n = re.subn(r"(?m)^(  size_px: \[1024, 440\].*)$",
                      f"  alpha: {GUI_OVERLAY_ALPHA}\n\\g<1>", text)
    assert n == 1, f"模板里应有且只有 1 行 overlay 段的 `  size_px: [1024, 440]`，命中 {n} 处"

    # ② 桌面段的 alpha 改成自己的值（模板里是 0.90）
    text, n = re.subn(r"(?m)^  alpha: 0\.90.*$", f"  alpha: {GUI_SANDBOX_ALPHA}", text)
    assert n == 1, f"模板里 desktop_overlay 段应有且只有 1 行 `  alpha: 0.90`，命中 {n} 处"

    # ③ 目标窗口标题指向一定不存在的窗口：结论不依赖本机是否真开着 VRChat
    text, n = re.subn(r"(?m)^(  game_title:\s*)\S+", rf"\g<1>{FAKE_MISSING_TITLE}", text)
    assert n == 1, f"模板里应有且只有 1 行 `  game_title:`，命中 {n} 处"

    GUI_SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    # .gitattributes 规定源码 LF：显式传 newline，别让 Windows 把整份配置写成 CRLF
    GUI_SANDBOX.write_text(text, encoding="utf-8", newline="\n")

    data = _read_gui_sandbox()
    assert data["overlay"]["alpha"] == GUI_OVERLAY_ALPHA, data["overlay"].get("alpha")
    assert float(data["desktop_overlay"]["alpha"]) == GUI_SANDBOX_ALPHA, \
        data["desktop_overlay"].get("alpha")
    assert data["desktop_overlay"]["game_title"] == FAKE_MISSING_TITLE


def _read_gui_sandbox() -> dict:
    return yaml.safe_load(GUI_SANDBOX.read_text(encoding="utf-8"))


def test_gui_alpha_slider_reads_its_own_section_only() -> None:
    """★ 桌面字幕的滑块与窗口只认 `desktop_overlay:` 段，**不读**手腕屏的 `overlay:` 段。

    这条用例守的是 2026-10-04 那次返工：桌面面板与手腕屏的配置**完全独立**
    （返工前是「视觉参数继承 `overlay:` 段」，被维护者打回）。
    沙箱里三个值互不相同：手腕屏 0.5 / 桌面段 0.65 / 用户拖到 0.35 —— 读错一段就现形。

    顺带钉住：只有用户真拖过滑块才写盘（`_save_desktop_cfg()` 也被拖动落盘调到）；
    `_stop_desktop()` 要把拖动按钮文案复位成「解锁拖动」；字幕窗没起来时点解锁，
    文案不许谎称"已自动取消勾选"。
    """
    # 起界面会走 `load_api_key()`：干净环境（CI）上没有 key 直接 SystemExit → 假红。
    # 给一个**拼接出来的假 key**（不触发仓库的凭据扫描钩子）；本用例跟 key 真假无关。
    os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "deskalpha0123456789abcd")

    import vlt.config as _cfg_mod
    import vlt.i18n as _i18n
    import vlt.gui as _gui_mod

    _make_gui_sandbox()
    # 界面语言跟随系统语言（CI 与外国机器是英文系统）→ 钉死 zh，控件树排布才稳定。
    # ⚠️ 必须在构造窗口**之前**打桩。产品代码不依赖这个补丁。
    saved = (_cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG, _i18n.detect_system_language)
    _cfg_mod.DEFAULT_CONFIG = GUI_SANDBOX
    _gui_mod.DEFAULT_CONFIG = GUI_SANDBOX            # `_save_desktop_cfg` 用的是这个常量
    _i18n.detect_system_language = lambda: "zh"

    from vlt.gui import TranslationGUI
    from vlt.i18n import t

    gui = None
    try:
        gui = TranslationGUI()
        if gui._update_check_job is not None:        # 启动 3 秒后自动查更新：绝不真连 GitHub
            gui._root.after_cancel(gui._update_check_job)
            gui._update_check_job = None
        gui._root.update()

        # ① 滑块初值 == 桌面段自己的 0.65 —— **不是**手腕屏段的 0.5，也不是默认 0.90
        got = float(gui._desktop_alpha_var.get())
        assert abs(got - GUI_SANDBOX_ALPHA) < 1e-9, \
            f"滑块初值应取 desktop_overlay.alpha={GUI_SANDBOX_ALPHA}，实际 {got}"
        assert abs(got - GUI_OVERLAY_ALPHA) > 1e-9, \
            f"滑块初值读到了手腕屏的 overlay.alpha={GUI_OVERLAY_ALPHA}（两段必须独立）"
        assert abs(got - 0.9) > 1e-9, "滑块初值是兜底 0.90 —— 没跟本段配置同源"

        # ② 起字幕窗：窗口那侧解析出来的 alpha 必须与滑块显示的是同一个值
        gui._desktop_var.set(True)
        gui._on_desktop_toggle()
        gui._root.update()
        if gui._desktop_out is None:
            print("  跳过界面落盘断言（本平台没有桌面窗口后端）")
            return
        assert abs(gui._desktop_out.cfg.alpha - got) < 1e-9, \
            f"窗口透明度 {gui._desktop_out.cfg.alpha} 与滑块 {got} 不同源"
        assert gui._desktop_alpha_touched is False, "没碰过滑块就不该算'动过了'"

        # ③ 只解锁/锁定拖动（模拟拖到某处放手），绝不碰滑块 → 配置里的 alpha 不许被改写。
        #    落点故意**不等于**模板里的 pos:[80, 80]：否则「位置写进去了」那条断言即使
        #    落盘整条路失效也照样绿（空过）。拖动放手时 `_on_drag_end` 做的正是这个赋值
        #    （真鼠标那一路已由 test_drag_moves_window_and_snaps_to_anchor 覆盖）。
        pos = tuple(gui._desktop_out.position)
        landed = (pos[0] + 37, pos[1] + 23)
        assert list(landed) != list(_read_gui_sandbox()["desktop_overlay"]["pos"]), \
            "落点得与配置里现成的 pos 不同，否则下面那条断言是空过"
        gui._desktop_out.user_pos = landed
        gui._toggle_desktop_drag()                   # 解锁
        assert gui._desktop_dragging is True
        assert gui._desktop_drag_btn.cget("text") == t("锁定位置"), \
            gui._desktop_drag_btn.cget("text")
        gui._toggle_desktop_drag()                   # 锁定 → 落盘
        data = _read_gui_sandbox()
        assert abs(float(data["desktop_overlay"]["alpha"]) - GUI_SANDBOX_ALPHA) < 1e-9, \
            f"只拖了位置就把 alpha 改写了：{data['desktop_overlay'].get('alpha')}"
        # 落盘这条路本身得是通的（否则上一条断言是空过）：位置写进去了，值就是落点
        assert list(data["desktop_overlay"]["pos"]) == list(landed), \
            f"位置没写进去（落盘路径失效？）：{data['desktop_overlay'].get('pos')} vs {landed}"
        assert gui._desktop_drag_btn.cget("text") == t("解锁拖动")

        # ④ 真的拖了滑块 → 窗口立刻跟着变，且 alpha 这才被写进配置
        gui._desktop_alpha_var.set(GUI_SLIDER_ALPHA)
        gui._on_desktop_alpha()
        assert gui._desktop_alpha_touched is True
        assert abs(gui._desktop_out.cfg.alpha - GUI_SLIDER_ALPHA) < 1e-9, \
            gui._desktop_out.cfg.alpha
        gui._save_desktop_cfg()                      # 防抖那 300ms 直接跑同一个函数
        data = _read_gui_sandbox()
        assert abs(float(data["desktop_overlay"]["alpha"]) - GUI_SLIDER_ALPHA) < 1e-9, \
            f"拖过滑块后 alpha 该写进配置：{data['desktop_overlay'].get('alpha')}"
        # 位置那条不能因为多了 alpha 就被冲掉
        assert list(data["desktop_overlay"]["pos"]) == list(landed), data["desktop_overlay"]

        # ⑤ 卫生项：关掉字幕窗后按钮文案必须复位（原来会一直写着「锁定位置」）
        gui._toggle_desktop_drag()                   # 再解锁一次
        assert gui._desktop_drag_btn.cget("text") == t("锁定位置")
        gui._stop_desktop()
        assert gui._desktop_out is None and gui._desktop_dragging is False
        assert gui._desktop_drag_btn.cget("text") == t("解锁拖动"), \
            f"字幕窗都关了按钮还写着：{gui._desktop_drag_btn.cget('text')!r}"

        # ⑥ 卫生项：字幕窗没在跑时点解锁 → 文案如实（这条分支没取消任何勾选）
        gui._toggle_desktop_drag()
        assert gui._desktop_dragging is False
        status = str(gui._status_label.cget("text"))
        assert t("桌面字幕还没开启，先勾上「桌面字幕」再解锁拖动") in status, status
        assert gui._desktop_var.get() is True, "这条分支不该动用户的勾选状态"
    finally:
        if gui is not None:
            try:
                gui._root.destroy()
            except Exception:  # noqa: BLE001
                pass
        (_cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG,
         _i18n.detect_system_language) = saved
        # 沙箱文件留在 out/ 下即可（已 gitignore）；用户的 config.yaml 全程没被碰过
    print(f"  桌面字幕透明度滑块：初值取本段的 {GUI_SANDBOX_ALPHA}（不是手腕屏的 "
          f"{GUI_OVERLAY_ALPHA}）/ 只拖位置不改写 alpha / 拖过滑块才写 {GUI_SLIDER_ALPHA} / "
          "按钮文案与提示如实 OK")


def test_gui_poll_ticks_out_created_after_build() -> None:
    """★ 回归 6083052：桌面字幕/手腕屏**晚于 poll 起跑**才创建，也必须被每跳 tick。

    用户实测症状：字幕窗能正常显示，但**收不到原文/译文、拖不动、改配置不生效** ——
    这三件事恰好都在 `DesktopOverlay.tick()` 里做（出图 / 跟随 / 热重载）。
    根因：gui 拆分时把实例塞进单元素 list 容器交给 poll，却**从没写回** → poll 永远
    读到构建期的 None。这条用例只盯「建窗晚于 _poll」这一条：先把假实例挂上，再让
    构建期起的那条 poll 链自己跑一跳 —— **绝不能再调 gui._poll()**（重调会重建容器，
    把 bug 掩盖过去）。
    """
    import time as _time

    os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "deskalpha0123456789abcd")

    import vlt.config as _cfg_mod
    import vlt.gui as _gui_mod
    import vlt.i18n as _i18n
    from vlt import gui_chat

    _make_gui_sandbox()
    saved = (_cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG, _i18n.detect_system_language)
    _cfg_mod.DEFAULT_CONFIG = GUI_SANDBOX
    _gui_mod.DEFAULT_CONFIG = GUI_SANDBOX
    _i18n.detect_system_language = lambda: "zh"

    from vlt.gui import TranslationGUI

    class _FakeOut:
        def __init__(self) -> None:
            self.ticks = 0

        def tick(self) -> None:
            self.ticks += 1

    gui = None
    try:
        gui = TranslationGUI()          # 构建期已跑过一次 _poll（那时两个实例都还是 None）
        if gui._update_check_job is not None:
            gui._root.after_cancel(gui._update_check_job)
            gui._update_check_job = None
        gui._root.update()

        # 「开始翻译」之后才建窗：只把实例挂上去，**不碰** poll 链
        fake_ov, fake_dov = _FakeOut(), _FakeOut()
        gui._overlay_out = fake_ov
        gui._desktop_out = fake_dov

        # 推 Tk 事件循环 0.3s（poll 50ms 一跳）：让构建期那条链自己跑到 tick
        end = _time.monotonic() + 0.3
        while _time.monotonic() < end:
            gui._root.update()
            _time.sleep(0.01)

        assert fake_dov.ticks > 0, (
            "桌面字幕晚于 _poll 创建后一次都没被 tick —— 容器没写回？"
            "（症状：字幕收不到内容 / 拖不动 / 热重载失效）")
        assert fake_ov.ticks > 0, "手腕屏晚于 _poll 创建后没被 tick"
    finally:
        if gui is not None:
            try:
                gui_chat.cancel_poll(gui._chat_ctx, gui._root)
            except Exception:  # noqa: BLE001
                pass
            try:
                gui._root.destroy()
            except Exception:  # noqa: BLE001
                pass
        (_cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG,
         _i18n.detect_system_language) = saved
    print("  poll→tick 活引用：建窗晚于 _poll 也每跳被 tick（桌面字幕 + 手腕屏）OK")


def test_update_entries_keeps_room_label() -> None:
    """★ 4 元组条目（房间成员的昵称）不许被丢 —— 桌面字幕与手腕屏共用一套渲染。

    真实缺口（main 的桌面字幕 × 房间分支的 4 元组，合并时才暴露）：`update_entries()` 原本
    写的是 `try: who, source, text = item / except: continue`，**只解 3 个** ——
    房间链路传进来的 4 元组会整条被 continue 掉，症状是「房间里别人说的话在桌面字幕上
    凭空消失」，而手腕屏那条腿好好的（它一直收 4 元组）。两条腿共用
    `overlay.render_conversation`，形状口径也必须共用。
    """
    import tempfile

    from PIL import Image

    from vlt.output.desktop_overlay import DesktopOverlay, DesktopOverlayConfig

    cfg = DesktopOverlayConfig(enabled=True, mode="conversation", size_px=(640, 240),
                               font_size=30, source_font_size=22, max_lines=3)
    ov = DesktopOverlay(cfg, dry_run=True)
    with tempfile.TemporaryDirectory() as tmp:
        ov._frames_dir = Path(tmp)      # 指到临时目录，别把仓库 out/ 弄脏
        assert ov.start() is True

        n0 = ov.frame_count
        # 4 元组：房间成员的条目（who="peer:<id>"，第 4 项是昵称）
        room_entries = [("peer:p_abc123", "", "我这边能听到你", "小明")]
        ov.update_entries(room_entries, force=True)
        assert ov.frame_count == n0 + 1, (
            f"4 元组条目被整条丢掉了：帧数没有增加（{n0} → {ov.frame_count}）—— "
            "症状就是「房间里别人的话在桌面字幕上不显示」")
        assert len(ov._entries) == 1, f"条目没进渲染队列：{ov._entries!r}"
        assert ov._entries[0][3] == "小明", f"昵称被丢掉了：{ov._entries[0]!r}"

        # 昵称必须**真的画进图里**：同一句话，带 label 与不带 label 出的图必须不同
        img_with = Image.open(sorted(Path(tmp).glob("frame_*.png"))[-1]).convert("RGB")
        ov._entries = []
        ov._last_sig = None
        ov.update_entries([("peer:p_abc123", "", "我这边能听到你")], force=True)
        img_without = Image.open(sorted(Path(tmp).glob("frame_*.png"))[-1]).convert("RGB")
        assert img_with.tobytes() != img_without.tobytes(), \
            "带昵称与不带昵称渲染出的图一模一样 —— 昵称根本没被画出来"
        ov.close()
    print("  4 元组条目（房间昵称）不再被丢，且昵称真的画进了桌面字幕 OK")


if __name__ == "__main__":
    print("test_desktop_overlay:")
    test_compute_position_nine_anchors()
    test_position_clamped_into_area()
    test_clamp_stays_on_secondary_monitor()
    test_resolve_position_falls_back_to_cfg_pos()
    test_clamp_alpha_bounds()
    test_alpha_mask_bits()
    test_from_dict_defaults_and_own_keys()
    test_desktop_config_is_decoupled_from_overlay_section()
    test_backend_config()
    test_native_window_wiring()
    test_native_backend_falls_back_to_tk()
    test_dry_run_renders_frames()
    test_mode_dispatch_renders_differently()
    test_start_without_game_window()
    test_tk_smoke_full_cycle()
    test_attach_and_follow_fake_game_window()
    test_work_area_follows_attached_monitor()
    test_facade_safe_defaults_without_backend()
    test_create_desktop_window_refuses_in_test_process()
    test_monitor_work_area_falls_back_to_primary()
    test_shared_module_stays_platform_clean()
    test_win32_helpers_on_real_windows()
    test_click_through_toggles_with_drag()
    test_transparent_key_matches_key_rgb()
    test_best_anchor_picks_nearest_grid_point()
    test_drag_moves_window_and_snaps_to_anchor()
    test_own_window_excluded_from_game_search()
    test_gui_alpha_slider_reads_its_own_section_only()
    test_gui_poll_ticks_out_created_after_build()
    test_update_entries_keeps_room_label()
    print("ALL PASSED")
