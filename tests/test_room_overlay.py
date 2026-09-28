"""房间多人手腕屏验收（BRIEF 批次 2b · R4）。

只测 `vlt/output/overlay.py` 的**纯渲染**（`render_conversation` / `peer_color`）——
零 Tk、零 openvr、零网络，任何机器上都能离线跑。

覆盖 BRIEF 明列的五点：
    ① 同一个人多次出现颜色一致（换顺序、换时间都一样）；
    ② 不同人颜色不同（≥4 人两两不同）；
    ③ 昵称确实画上去了（作为一行小字，块会变高）；
    ④ 塞不下时丢的是最旧的、最新的在最下面；
    ⑤ 3 元组（旧形态）仍工作，且 mine/theirs 的行/色一点没变。

断言一律用**整列扫描**：条目是垂直堆叠的，取单点采样会正好落进块间留白里误判
（`test_overlay_conversation.py` 的注释里就记着这个坑）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vlt.output.overlay import (  # noqa: E402
    PEER_PALETTE,
    OverlayConfig,
    peer_color,
    render_conversation,
)

# 左 / 右外缘竖条的采样列（pad=12，竖条 x∈[24,29]，取靠内的 26 避开圆角抗锯齿）。
_LEFT_X = 24 + 2


def _col_ys(img, x: int, color) -> list[int]:  # noqa: ANN001
    """整列扫描：返回第 x 列上 RGB 恰好等于 `color` 的所有 y（竖条覆盖的行）。"""
    px = img.load()
    _, h = img.size
    want = tuple(color)
    return [y for y in range(h) if px[x, y][:3] == want]


def _right_x(img) -> int:  # noqa: ANN001
    w, _ = img.size
    return w - 24 - 2


# 六个 peer_id，各自哈希到调色板里**互不相同**的槽位（离线挑好的，见文件尾注释）。
# 颜色两两不同 → 扫描某种颜色就能唯一锁定是哪个人，用来验「换顺序颜色不变」「丢最旧」。
_PEERS = ["p_000000", "p_000001", "p_000002", "p_000004", "p_000005", "p_000007"]


# ---------------------------------------------------------------- 调色板自检
def test_palette_distinct_from_base_colors() -> None:
    """调色板每种颜色都必须区别于底板/mine 蓝/theirs 灰，否则整列扫描会张冠李戴。"""
    cfg = OverlayConfig()
    base = {"bg": cfg.color_bg, "mine": cfg.color_mine, "theirs": cfg.color_theirs}
    for col in PEER_PALETTE:
        for name, b in base.items():
            assert tuple(col) != tuple(b), f"调色板颜色 {col} 与 {name} {b} 撞色，扫描会误判"
    # 调色板内部也不该有重复（重复 = 少一种可区分颜色）
    assert len({tuple(c) for c in PEER_PALETTE}) == len(PEER_PALETTE), "调色板里有重复颜色"
    print(f"  调色板 {len(PEER_PALETTE)} 色互不相同，且都区别于 bg/mine/theirs OK")


# ---------------------------------------------------------------- ① 同一人同色
def test_same_peer_same_color_across_order_and_time() -> None:
    """① 同一个人：换出现顺序、换调用时间，颜色都必须一致（稳定哈希，不按顺序/不随机）。"""
    a, b = _PEERS[0], _PEERS[1]
    col_a = peer_color(a)

    # 换时间：反复调、间隔调，结果恒定（纯函数，且用的是 crc32 而非进程随机化的 hash()）
    for _ in range(3):
        assert peer_color(a) == col_a, "同一个人多次取色不一致（疑似用了随机/顺序）"
    assert peer_color(b) != col_a or True  # b 的颜色下面单独验

    cfg = OverlayConfig()
    text = "这句话用来占位，确保块够高能画出竖条"
    # 顺序一：a 在前（更旧，画在上面）；顺序二：a 在后（更新，画在下面）
    img1 = render_conversation([("peer:" + a, "", text, "甲"),
                                ("peer:" + b, "", text, "乙")], cfg)
    img2 = render_conversation([("peer:" + b, "", text, "乙"),
                                ("peer:" + a, "", text, "甲")], cfg)

    ys1 = _col_ys(img1, _LEFT_X, col_a)
    ys2 = _col_ys(img2, _LEFT_X, col_a)
    assert ys1, f"顺序一里没扫到 {a} 的竖条颜色 {col_a}"
    assert ys2, f"顺序二里没扫到 {a} 的竖条颜色 {col_a}（换顺序就丢色/变色了）"
    # 换顺序后 a 的位置应当移动（说明确实换了排位），但**颜色不变**
    assert (min(ys1), max(ys1)) != (min(ys2), max(ys2)), \
        "两种顺序下 a 的竖条位置竟完全一样（顺序没生效，测试没验到点子上）"
    print(f"  ① {a} 换顺序/换时间颜色恒为 {col_a}（位置 {min(ys1)}~{max(ys1)} → "
          f"{min(ys2)}~{max(ys2)}）OK")


# ---------------------------------------------------------------- ② 不同人不同色
def test_different_peers_different_colors() -> None:
    """② 至少 4 个人两两不同色（这里用 6 个，全部互异）。"""
    colors = {p: peer_color(p) for p in _PEERS}
    distinct = set(colors.values())
    assert len(distinct) == len(_PEERS), \
        f"不同人撞色了：{len(_PEERS)} 个人只得到 {len(distinct)} 种颜色 → {colors}"
    assert len(distinct) >= 4, f"要求 ≥4 人两两不同，实际只有 {len(distinct)} 种"
    # 每种颜色都必须真的来自固定调色板（不是凭空生成的随机色）
    for p, c in colors.items():
        assert tuple(c) in {tuple(x) for x in PEER_PALETTE}, f"{p} 的颜色 {c} 不在调色板里"
    print(f"  ② {len(_PEERS)} 个人两两不同色，且都取自固定调色板 OK")
    for p in _PEERS:
        print(f"     {p} → {colors[p]}")


# ---------------------------------------------------------------- ③ 昵称画上去了
def test_nickname_is_drawn_as_small_line() -> None:
    """③ 昵称确实被画上去：作为**一行小字**，会让该条目的块（连同外缘竖条）变高。

    昵称与译文都是白色（`color_source == color_translation`），没法靠颜色区分，
    所以改用「有昵称 vs 无昵称」的**竖条高度差**来证明多画了一行小字：
    关掉 show_source 且 source 为空时，能多出这一行的只可能是昵称。
    """
    cfg = OverlayConfig()
    cfg.show_source = False          # 排除原文小字的干扰：多出来的小字行只能是昵称
    pid = _PEERS[0]
    col = peer_color(pid)
    text = "只有一行译文"

    img_no = render_conversation([("peer:" + pid, "", text, "")], cfg)
    img_yes = render_conversation([("peer:" + pid, "", text, "甲乙丙")], cfg)

    assert img_no.size == cfg.size_px and img_yes.size == cfg.size_px, "面板物理尺寸被改了"
    # 画面确实变了（昵称落笔了）
    assert img_no.tobytes() != img_yes.tobytes(), "加昵称后画面没有任何变化（没画上去）"

    h_no = len(_col_ys(img_no, _LEFT_X, col))
    h_yes = len(_col_ys(img_yes, _LEFT_X, col))
    assert h_no > 0 and h_yes > 0, "没扫到 peer 竖条，无法比对高度"
    # 多出的高度 ≈ 一行小字（source_font_size+6）+ 小字与大字间的 4px 间距
    expect = (cfg.source_font_size + 6) + 4
    delta = h_yes - h_no
    assert delta > 0, f"加昵称后竖条没变高（{h_no} → {h_yes}）：昵称没占位"
    assert abs(delta - expect) <= 2, \
        f"竖条增高 {delta}px，与一行小字的高度 {expect}px 对不上（昵称可能没按小字画）"
    print(f"  ③ 昵称作为一行小字画上去：竖条 {h_no}px → {h_yes}px（+{delta}≈{expect}）OK")


# ---------------------------------------------------------------- ④ 丢最旧、最新在下
def test_overflow_drops_oldest_keeps_newest_at_bottom() -> None:
    """④ 塞不下时：丢的是**最旧**的（最早那条的颜色整个消失），**最新**的在最下面。"""
    cfg = OverlayConfig()
    # 每条都带昵称 + 一段会折成两行的长文，6 条必然超出面板预算 → 触发丢弃
    long_text = "这是一句故意写得比较长的话，用来让每一条都折成两行从而更快塞满面板"
    entries = [("peer:" + p, "", f"{i}:{long_text}", f"成员{i}")
               for i, p in enumerate(_PEERS)]
    img = render_conversation(entries, cfg)
    _, h = img.size

    oldest_col = peer_color(_PEERS[0])
    newest_col = peer_color(_PEERS[-1])
    ys_oldest = _col_ys(img, _LEFT_X, oldest_col)
    ys_newest = _col_ys(img, _LEFT_X, newest_col)

    assert not ys_oldest, \
        f"最旧那条（{_PEERS[0]} 色 {oldest_col}）竟然还在（应被更新的挤掉）：y={ys_oldest[:5]}"
    assert ys_newest, f"最新那条（{_PEERS[-1]} 色 {newest_col}）不见了（不该丢最新的）"
    # 最新一条必须落在面板**下半部**（底对齐堆叠，最后画的在最下面）
    assert max(ys_newest) > h // 2, \
        f"最新那条不在下半部（max y={max(ys_newest)}, 半高={h // 2}）： newest 没在最下面"
    # 仍然画出来的其它条目，颜色都必须是「较新」的那几个（旧的被丢）
    present = [p for p in _PEERS if _col_ys(img, _LEFT_X, peer_color(p))]
    assert present == _PEERS[len(_PEERS) - len(present):], \
        f"保留下来的不是「最新的连续若干条」：{present}"
    print(f"  ④ 塞不下时丢最旧、最新在最下面 OK（保留 {len(present)}/{len(_PEERS)} 条："
          f"{present}，最新条底 y={max(ys_newest)}/{h}）")


# ---------------------------------------------------------------- ⑤ 3 元组 + mine/theirs 不变
def test_three_tuple_and_mine_theirs_unchanged() -> None:
    """⑤ 旧的 3 元组仍工作；mine/theirs 的分侧与颜色一点没变；空 label 的 4 元组与 3 元组逐像素相同。"""
    cfg = OverlayConfig()

    # 3 元组：theirs 靠左灰条、mine 靠右蓝条（与改版前完全一致的口径）
    img3 = render_conversation([("theirs", "你好，你好吗？", "Hello, how are you?"),
                                ("mine", "我很好，谢谢你。", "I'm fine, thank you.")], cfg)
    left_theirs = _col_ys(img3, _LEFT_X, cfg.color_theirs)
    right_mine = _col_ys(img3, _right_x(img3), cfg.color_mine)
    assert left_theirs, "3 元组：theirs 没在左缘画灰条"
    assert right_mine, "3 元组：mine 没在右缘画蓝条"
    # 分侧不能串：theirs 不该出现在右缘、mine 不该出现在左缘
    assert not _col_ys(img3, _right_x(img3), cfg.color_theirs), "theirs 串到了右缘"
    assert not _col_ys(img3, _LEFT_X, cfg.color_mine), "mine 串到了左缘"

    # 空 label 的 4 元组必须与 3 元组**逐像素相同**（新形态没改变既有渲染）
    img4 = render_conversation([("theirs", "你好，你好吗？", "Hello, how are you?", ""),
                                ("mine", "我很好，谢谢你。", "I'm fine, thank you.", "")], cfg)
    assert img3.tobytes() == img4.tobytes(), "空 label 的 4 元组与 3 元组渲染不一致（mine/theirs 被改动了）"

    # peer 与 mine/theirs 同屏：mine 仍靠右蓝条、peer 靠左但用调色板色（不是 theirs 灰）
    pid = _PEERS[2]
    img_mix = render_conversation([("peer:" + pid, "", "来自房间的一句话", "远端甲"),
                                   ("mine", "我说的话", "My own words")], cfg)
    assert _col_ys(img_mix, _right_x(img_mix), cfg.color_mine), "混排时 mine 的右侧蓝条丢了"
    assert _col_ys(img_mix, _LEFT_X, peer_color(pid)), "混排时 peer 的左侧调色板竖条丢了"
    assert not _col_ys(img_mix, _LEFT_X, cfg.color_theirs), \
        "peer 用了 theirs 灰（应使用按 peer_id 稳定哈希的调色板色）"
    print("  ⑤ 3 元组照常工作；mine/theirs 分侧与颜色不变；空 label 4 元组逐像素等同 3 元组 OK")


# 说明：_PEERS 这六个 id 是离线用 `zlib.crc32(id) % len(PEER_PALETTE)` 挑出来的，
# 分别落在 6 个互不相同的槽位（6/8/2/7/3/5），因此「扫某种颜色」能唯一锁定某个人。
# 换调色板长度时这组 id 需要重挑；测试开头的 test_palette_* 会先把撞色问题暴露出来。


def main() -> int:
    print("test_room_overlay:")
    tests = [
        test_palette_distinct_from_base_colors,
        test_same_peer_same_color_across_order_and_time,
        test_different_peers_different_colors,
        test_nickname_is_drawn_as_small_line,
        test_overflow_drops_oldest_keeps_newest_at_bottom,
        test_three_tuple_and_mine_theirs_unchanged,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
