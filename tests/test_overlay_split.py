"""手腕屏「分栏」（方案 A）验收测试：**一块面板、左右两栏，每栏宽度可自定义**。

只测纯渲染（离线，不碰 SteamVR、不需要头显）：
  1) 宽度可自定义 —— 比例写法与像素写法都能吃，且分隔线位置跟着配置走
  2) 内容按栏筛选 —— 「别人说」不会串到「我说」那一栏（反之亦然）
  3) 栏标题、栏间分隔线、脏配置回落
  4) 关掉分栏时**走的还是老渲染路径**（像素级回归见注释里的基线哈希）

面板几何（1024×440 / pad=12）：可绘制区内宽 = 1024-48 = 976，取整误差由最后一栏吸收。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.output.overlay import (  # noqa: E402
    OverlayConfig,
    WristOverlay,
    _pane_boxes,
    peer_color,
    render_conversation,
    render_split,
)

PAD = 12
W, H = 1024, 440
LEFT_X = PAD * 2                      # 可绘制区左边界（= theirs 竖条的 x）
RIGHT_X = W - PAD * 2 - 1             # 可绘制区右边界（= mine 竖条的 x）


# ---------------------------------------------------------------- 像素探针
def _is_color(px, col, tol: int = 12) -> bool:
    return (abs(px[0] - col[0]) <= tol and abs(px[1] - col[1]) <= tol
            and abs(px[2] - col[2]) <= tol and px[3] > 60)


def _col_ys(img, x: int, col, tol: int = 12) -> list[int]:
    """扫某一列上匹配该颜色的 y 列表（沿用 test_room_overlay 的做法）。"""
    px = img.load()
    out = []
    for y in range(img.size[1]):
        if _is_color(px[x, y], col, tol):
            out.append(y)
    return out


def _divider_x(img, cfg: OverlayConfig) -> int | None:
    """找栏间竖分隔线：颜色 = 边框色、透明度减半，跨大部分面板高度。

    ⚠️ 分隔线的 alpha 就是 border_alpha//2（默认 60），**不能**用 `_is_color`（它要求
    alpha > 60）—— 第一版探针就栽在这儿：线明明画了却扫不到。
    """
    col = cfg.color_border
    want_a = max(40, cfg.border_alpha // 2)
    px = img.load()
    hits: list[int] = []
    for x in range(PAD * 2, W - PAD * 2):
        n = 0
        for y in range(PAD * 2 + 6, H - PAD * 2 - 6):
            p = px[x, y]
            if (abs(p[0] - col[0]) <= 8 and abs(p[1] - col[1]) <= 8
                    and abs(p[2] - col[2]) <= 8 and abs(p[3] - want_a) <= 6):
                n += 1
        if n > (H - 4 * PAD) * 0.9:
            hits.append(x)
    return (hits[0] + hits[-1]) // 2 if hits else None


def _ratio_of_first_pane(div_x: int, gap: int) -> float:
    """由分隔线位置反推第一栏占内宽的比例（空档两侧各半）。"""
    pane1 = (div_x - gap // 2) - LEFT_X
    return pane1 / (RIGHT_X + 1 - LEFT_X)


def _make(pairs, **kw) -> OverlayConfig:
    d = {"split": True, "size_px": [W, H]}
    d.update(kw)
    return OverlayConfig.from_dict(d)


# ---------------------------------------------------------------- 用例
def test_widths_customizable() -> bool:
    """★ 宽度可自定义：比例写法下，分隔线位置必须跟着配置走。"""
    ok = True
    cases = [((0.5, 0.5), 0.50), ((0.3, 0.7), 0.30), ((0.7, 0.3), 0.70), ((0.25, 0.75), 0.25)]
    for panes, expect in cases:
        cfg = _make(panes, split_panes=list(panes))
        img = render_split([], cfg)
        div = _divider_x(img, cfg)
        if div is None:
            print(f"  宽度 {panes}：没找到分隔线 ✗")
            ok = False
            continue
        got = _ratio_of_first_pane(div, cfg.split_gap_px)
        good = abs(got - expect) <= 0.02
        print(f"  宽度 {panes} → 分隔线 x={div}，反推第一栏占比 {got:.3f}"
              f"（期望 {expect:.2f}）  {'OK' if good else '✗'}")
        ok &= good
    return ok


def test_pixel_weights_equivalent() -> bool:
    """★ 像素/份数写法与比例写法等价（[300,700] == [0.3,0.7]）。"""
    a = _make(None, split_panes=[0.3, 0.7])
    b = _make(None, split_panes=[300, 700])
    c = _make(None, split_panes=[3, 7])
    ia, ib, ic = render_split([], a), render_split([], b), render_split([], c)
    ok = ia.tobytes() == ib.tobytes() == ic.tobytes()
    da, db = _divider_x(ia, a), _divider_x(ib, b)
    print(f"  [0.3,0.7] vs [300,700] vs [3,7]：分隔线 {da} / {db} / {_divider_x(ic, c)}，"
          f"像素{'一致 ✅' if ok else '不一致 ✗'}")
    return ok


def test_content_filtering() -> bool:
    """★ 内容不串栏：「别人说」只进第 1 栏、「我说」只进第 2 栏。"""
    ok = True
    cfg = _make(None, split_panes=[0.5, 0.5])
    pid = "p_roommate"
    mine_e = ("mine", "我说的中文", "What I said")
    theirs_e = ("theirs", "相手の日本語", "别人说的译文")

    img = render_split([theirs_e, mine_e], cfg)
    boxes = _pane_boxes(LEFT_X, RIGHT_X + 1, cfg.split_panes, cfg.split_gap_px)
    (p1x0, p1x1), (p2x0, p2x1) = boxes
    p1_left = _col_ys(img, p1x0, cfg.color_theirs)          # 第 1 栏左缘应有 theirs 竖条
    p1_right = _col_ys(img, p1x1 - 1, cfg.color_mine)       # 第 1 栏右缘**不该**有 mine 竖条
    p2_right = _col_ys(img, p2x1 - 1, cfg.color_mine)       # 第 2 栏右缘应有 mine 竖条
    p2_left = _col_ys(img, p2x0, cfg.color_theirs)          # 第 2 栏左缘**不该**有 theirs 竖条
    cond = bool(p1_left) and not p1_right and bool(p2_right) and not p2_left
    print(f"  混排：第1栏 theirs 竖条 {len(p1_left)}px / 误入 mine {len(p1_right)}px；"
          f"第2栏 mine 竖条 {len(p2_right)}px / 误入 theirs {len(p2_left)}px  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    # 只有「别人说」时，第 2 栏应该是空的（没有 mine 竖条）
    img2 = render_split([theirs_e], cfg)
    cond2 = not _col_ys(img2, p2x1 - 1, cfg.color_mine)
    print(f"  仅「别人说」：第2栏 mine 竖条 {len(_col_ys(img2, p2x1 - 1, cfg.color_mine))}px"
          f"（应为 0）  {'OK' if cond2 else '✗'}")
    ok &= cond2

    # 内容选择器可换：第 1 栏放 peer（房间里别人），mine 的条目也不该出现在第 1 栏
    cfg2 = _make(None, split_content=["peer", "mine"])
    img3 = render_split([("peer:" + pid, "", "房间里的一个人", "甲"), mine_e], cfg2)
    cond3 = (bool(_col_ys(img3, p1x0, peer_color(pid)))
             and not _col_ys(img3, p1x1 - 1, cfg2.color_mine)
             and bool(_col_ys(img3, p2x1 - 1, cfg2.color_mine)))
    print(f"  选 peer 进第1栏：peer 竖条 {len(_col_ys(img3, p1x0, peer_color(pid)))}px、"
          f"mine 未串入 {not _col_ys(img3, p1x1 - 1, cfg2.color_mine)}  {'OK' if cond3 else '✗'}")
    ok &= cond3
    return ok


def test_labels_and_divider() -> bool:
    """栏标题与分隔线：配了才画，且关得掉。"""
    ok = True
    plain = _make(None)
    labeled = _make(None, split_labels=["别人", "我"])
    i_plain, i_label = render_split([], plain), render_split([], labeled)
    labeled_ink = i_label.tobytes() != i_plain.tobytes()
    div_on = _divider_x(i_plain, plain) is not None
    nodiv = _make(None, split_divider=False)
    div_off = _divider_x(render_split([], nodiv), nodiv) is None
    print(f"  栏标题：配了有小字标题={'OK' if labeled_ink else '✗'}；"
          f"分隔线：默认画={'OK' if div_on else '✗'}、关得掉={'OK' if div_off else '✗'}")
    ok &= labeled_ink and div_on and div_off
    return ok


def test_dirty_config_falls_back() -> bool:
    """脏配置绝不让渲染崩：一律回落默认。"""
    ok = True
    cases = [
        ({"split_panes": "abc"}, (0.5, 0.5)),
        ({"split_panes": [0, -3]}, (0.5, 0.5)),
        ({"split_panes": []}, (0.5, 0.5)),
        ({"split_panes": [2, 2, 2]}, (2.0, 2.0, 2.0)),
        ({"split_content": "mine"}, ("mine",)),
        ({"split_content": None}, ("theirs", "mine")),
    ]
    for raw, expect in cases:
        cfg = OverlayConfig.from_dict(raw)
        got = cfg.split_panes if "split_panes" in raw else cfg.split_content
        good = got == expect
        print(f"  {raw} → {got}（期望 {expect}）  {'OK' if good else '✗'}")
        ok &= good
    # 面板极窄时退化成不切分，且不能算出负宽度
    tiny = OverlayConfig.from_dict({"size_px": [40, 440], "split": True})
    boxes = _pane_boxes(PAD * 2, 40 - PAD * 2, tiny.split_panes, tiny.split_gap_px)
    good = all(b[1] > b[0] for b in boxes)
    print(f"  极窄面板（40px）→ {boxes} 不出现负宽度  {'OK' if good else '✗'}")
    ok &= good
    return ok


def test_no_split_keeps_old_path() -> bool:
    """★ 关掉分栏时 `update_entries` 必须走老渲染（render_conversation），开时才走 render_split。

    老路径的像素回归：本仓库 `out/overlay_baseline/*.png` 是重构前的基线，
    重构前后 sha256 逐字节一致（普通对话 e04674ac… / 长句 686cad0f…）。
    """
    import vlt.output.overlay as om

    ok = True
    entries = [("theirs", "别人说的", "translated"), ("mine", "我说的", "what I said")]
    real_c, real_s = om.render_conversation, om.render_split
    calls: list[str] = []
    om.render_conversation = lambda e, c=None: (calls.append("conv"), real_c(e, c))[1]
    om.render_split = lambda e, c=None: (calls.append("split"), real_s(e, c))[1]
    try:
        with tempfile.TemporaryDirectory() as td:
            for split, expect in ((False, "conv"), (True, "split")):
                calls.clear()
                cfg = om.OverlayConfig.from_dict({"split": split, "size_px": [W, H]})
                ov = om.WristOverlay(cfg, dry_run=True)
                ov._frames_dir = Path(td)          # dry-run 只写图，不碰 SteamVR
                ov.update_entries(entries, force=True)
                good = calls == [expect]
                print(f"  split={split} → 调用 {calls}（期望 ['{expect}']）  {'OK' if good else '✗'}")
                ok &= good
    finally:
        om.render_conversation, om.render_split = real_c, real_s
    return ok


def main() -> int:
    print("手腕屏分栏（方案 A）测试：")
    results = [
        ("宽度可自定义", test_widths_customizable()),
        ("像素写法等价", test_pixel_weights_equivalent()),
        ("内容不串栏", test_content_filtering()),
        ("标题与分隔线", test_labels_and_divider()),
        ("脏配置回落", test_dirty_config_falls_back()),
        ("分派正确", test_no_split_keeps_old_path()),
    ]
    bad = [name for name, ok in results if not ok]
    print("\nALL PASSED" if not bad else f"FAILED: {', '.join(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
