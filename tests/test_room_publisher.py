"""`SourcePublisher` 验收：纯逻辑、零 IO、注入时钟与句 id，全程离线（死代理下也必须全绿）。

## 为什么这些规则每条都要单测

发布器是「会话层文本流 → 房间协议上行帧」的唯一切口。它错了，房间里所有人
看到的字幕都会错：叠字（发了 diff）、腰斩句（静默兜底阈值太小）、刷屏（没节流）、
串句（utt 没换）。而这些错误在真机上极难归因 —— 所以这里用注入的 `now_ms`
把每一种时序都钉死来测，不 sleep、不碰网络。

对应 BRIEF 批次 2a 的规则 1~6 与 `should_publish` 的三种 source。
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.room.publisher import PublishItem, SourcePublisher, should_publish  # noqa: E402


class Utts:
    """确定性的 `new_utt` 注入源：u1、u2、u3…（测试要能断言「新句换了新 id」）。"""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"u{self.n}"


def make(**kw) -> tuple[SourcePublisher, Utts]:
    ids = Utts()
    kw.setdefault("final_gap_s", 3.0)
    kw.setdefault("min_partial_ms", 200)
    return SourcePublisher(new_utt=ids, **kw), ids


def tuples(items: list[PublishItem]) -> list[tuple]:
    return [(i.utt, i.rev, i.text, i.is_final) for i in items]


def test_item_is_frozen_snapshot() -> None:
    """契约：`PublishItem` 是不可变快照，四个字段与 `RoomClient.publish()` 一一对应。"""
    item = PublishItem(utt="u1", rev=2, text="你好", is_final=True)
    assert dataclasses.is_dataclass(item) and item.__dataclass_fields__.keys() == {
        "utt", "rev", "text", "is_final"}, f"字段变了（publish 的参数就对不上了）：{item}"
    try:
        item.text = "改一下"                          # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("PublishItem 必须是 frozen（调用方拿着不该被后续 feed 改掉）")
    # 默认句 id：8 位 hex，且两次不重复（secrets 随机源）
    a, b = SourcePublisher().feed("第一句", False), SourcePublisher().feed("第二句", False)
    assert len(a[0].utt) == 8 and a[0].utt != b[0].utt, f"默认 utt 口径不对：{a[0].utt} {b[0].utt}"
    int(a[0].utt, 16)                                  # 必须是 hex（拼错格式这里就炸）
    print(f"  PublishItem frozen + 字段对齐 publish() + 默认 utt 8 位 hex OK（{a[0].utt}）")


def test_rule1_skip_empty_and_unchanged() -> None:
    """规则 1：只发非空、且与上一次已发快照不同的文本（模型会重复吐同样的字）。"""
    pub, _ = make()
    got = pub.feed("你好", False, now_ms=0)
    assert tuples(got) == [("u1", 1, "你好", False)], got
    assert pub.feed("你好", False, now_ms=500) == [], "★ 内容没变还发（白占限速额度）"
    assert pub.feed(" 你好 ", False, now_ms=1000) == [], "★ 只差首尾空白也算没变（strip 后比对）"
    assert pub.feed("", False, now_ms=1500) == [], "★ 空文本不该上行"
    assert pub.feed("   \n ", False, now_ms=2000) == [], "★ 纯空白不该上行"
    assert pub.feed(None, False, now_ms=2100) == [], "★ None 不许抛（会话层可能给脏值）"
    got = pub.feed("你好啊", False, now_ms=2500)
    assert tuples(got) == [("u1", 2, "你好啊", False)], f"内容变了就该发，且同句 rev+1：{got}"
    # 空文本连句都不该开：句 id 是惰性分配的
    pub2, ids2 = make()
    assert pub2.feed("", False, now_ms=0) == [] and ids2.n == 0, "★ 空帧把句 id 白白烧掉了"
    print("  规则 1：空 / 纯空白 / 内容未变 → 不发；变了 → 同句 rev+1 OK")


def test_rule2_rev_increases_and_new_utt_per_sentence() -> None:
    """规则 2：同一句内 `rev` 递增；封句后必换新 `utt`、`rev` 从 1 重来。"""
    pub, ids = make(min_partial_ms=0)
    snaps = ["我", "我在", "我在测试", "我在测试房间中继"]
    seen = [pub.feed(s, False, now_ms=i * 100) for i, s in enumerate(snaps)]
    first = [(g[0].utt, g[0].rev) for g in seen]
    assert first == [("u1", 1), ("u1", 2), ("u1", 3), ("u1", 4)], f"rev 不递增：{first}"
    assert [g[0].text for g in seen] == snaps, "text 必须是全量快照（不是 diff、不叠字）"
    fin = pub.feed(snaps[-1], True, now_ms=900)
    assert tuples(fin) == [("u1", 5, snaps[-1], True)], f"final 该是 rev+1：{fin}"
    nxt = pub.feed("新一句", False, now_ms=1000)
    assert tuples(nxt) == [("u2", 1, "新一句", False)], f"★ 新句没换新 utt / rev 没归 1：{nxt}"
    assert ids.n == 2, f"应该只烧了 2 个句 id：{ids.n}"
    print("  规则 2：句内 rev 1→5、封句后新 utt + rev 归 1 OK")


def test_rule3_final_seals_even_when_text_unchanged() -> None:
    """规则 3：`is_final=True` → 发一条 final 然后封句；**文本没变也必须发**。

    对端可能只见过被节流前的旧快照，final 是「这句到此为止」的唯一信号 ——
    漏发它会让大家的手腕屏上永远挂着半句。
    """
    pub, _ = make(min_partial_ms=0)
    pub.feed("走吧", False, now_ms=0)
    got = pub.feed("走吧", True, now_ms=100)            # 内容与上次已发快照相同
    assert tuples(got) == [("u1", 2, "走吧", True)], f"★ 文本没变就把 final 吞了：{got}"
    again = pub.feed("走吧", False, now_ms=200)
    assert again == [], "封句后同内容不该立刻重发（重复垃圾不进房间）"
    nxt = pub.feed("走吧走吧", True, now_ms=300)
    assert tuples(nxt) == [("u2", 1, "走吧走吧", True)], f"封句后的下一帧必须是新句：{nxt}"
    # 从没发过任何帧的 final：也得发（rev=1），不然这句话对端永远看不到
    pub2, _ = make(min_partial_ms=0)
    got = pub2.feed("一上来就定稿", True, now_ms=0)
    assert tuples(got) == [("u1", 1, "一上来就定稿", True)], got
    print("  规则 3：final 必发（含文本未变 / 首帧即 final）+ 发完封句 OK")


def test_rule4_silence_gap_seals_previous_sentence() -> None:
    """规则 4：静默兜底分句 —— 距上次 feed 超过 `final_gap_s` 且上一句没封 →
    先补一条 final（文本取**上一次已发快照**）再开新句。

    这是「服务端有时不发 done」的既有教训：不兜底的话上一句永远开着，
    下一句会接着涨同一个 utt，对端看到的是一句越拼越长的胡话。
    ⚠️ 阈值口径与 `session.final_silence_s` 同源：必须大于服务端增量间隔
    （实测连续说话时相邻 delta 最大 2.3s），默认 3.0s；调小会把半句当定稿。
    """
    pub, _ = make(final_gap_s=3.0, min_partial_ms=0)
    assert SourcePublisher().final_gap_s == 3.0, "默认阈值必须是 3.0s（与仓库既有约定一致）"
    pub.feed("前半句", False, now_ms=0)
    # 没超阈值：同一句继续
    got = pub.feed("前半句还没说完", False, now_ms=2999)
    assert tuples(got) == [("u1", 2, "前半句还没说完", False)], f"阈值内不该分句：{got}"
    # 超过阈值（恰好等于不算，必须**大于**）：补 final + 开新句，一次 feed 出两帧
    got = pub.feed("后半句来了", False, now_ms=2999 + 3001)
    assert tuples(got) == [("u1", 3, "前半句还没说完", True),
                           ("u2", 1, "后半句来了", False)], f"★ 静默兜底不对：{tuples(got)}"
    # 补的 final 取「上一次已发快照」，不是当前文本
    assert got[0].text == "前半句还没说完" and got[0].utt == "u1"
    # 新句开着又静默 → 同样被兜底封句（u2 补 final，再开 u3）
    got = pub.feed("继续说", False, now_ms=100_000)
    assert tuples(got) == [("u2", 2, "后半句来了", True), ("u3", 1, "继续说", False)], \
        f"★ 第二次静默兜底不对：{tuples(got)}"
    # 上一帧已经是同内容的 final → 不重复补（服务端会重复发 done）
    pub2, _ = make(final_gap_s=1.0, min_partial_ms=0)
    pub2.feed("定稿了", True, now_ms=0)
    got = pub2.feed("下一句", False, now_ms=5000)
    assert tuples(got) == [("u2", 1, "下一句", False)], f"★ 对已定稿的句子又补了 final：{got}"
    print("  规则 4：静默 3s → 补 final（旧快照）+ 开新句、不重复兜底 OK")


def test_rule4_gap_final_uses_last_emitted_not_throttled_text() -> None:
    """规则 4 × 规则 5 交界：兜底 final 取**已发**快照 —— 被节流吃掉的那帧不发。

    对端没见过被节流的那帧，拿它当定稿等于发了一句没人看过的「最终版」。
    """
    pub, _ = make(final_gap_s=1.0, min_partial_ms=200)
    pub.feed("看得到这帧", False, now_ms=0)
    assert pub.feed("这帧被节流", False, now_ms=100) == [], "前提：这帧应被节流吃掉"
    got = pub.feed("很久以后", False, now_ms=5000)
    assert tuples(got) == [("u1", 2, "看得到这帧", True), ("u2", 1, "很久以后", False)], \
        f"★ 兜底 final 的文本不对：{tuples(got)}"
    print("  规则 4：兜底 final 用「上一次已发快照」而非被节流文本 OK")


def test_rule4_no_seal_when_nothing_published() -> None:
    """规则 4 边界：上一句**一个字都没发出去过** → 只开新句，不补空 final。"""
    pub, _ = make(final_gap_s=1.0)
    assert pub.feed("", False, now_ms=0) == []
    got = pub.feed("隔了很久才有的第一帧", False, now_ms=5000)
    assert tuples(got) == [("u1", 1, "隔了很久才有的第一帧", False)], f"★ 补了空 final：{got}"
    # 兜底 final 不受节流影响（此刻距上一帧很近也照发）
    pub2, _ = make(final_gap_s=0.2, min_partial_ms=10_000)
    pub2.feed("半句", False, now_ms=0)
    got = pub2.feed("新句", False, now_ms=1000)
    assert tuples(got) == [("u1", 2, "半句", True), ("u2", 1, "新句", False)], \
        f"★ 兜底 final 被节流吃掉了：{tuples(got)}"
    print("  规则 4 边界：没发过就不补空 final；兜底 final 不受节流影响 OK")


def test_rule5_partial_throttle_never_eats_final() -> None:
    """规则 5：距上一帧不足 `min_partial_ms` 的 partial 不发（内容变了也不发）；
    **final 永远发，绝不被节流吃掉**。"""
    pub, _ = make(min_partial_ms=200)
    assert SourcePublisher().min_partial_ms == 200, "默认节流必须是 200ms（方案 §11 口径）"
    assert tuples(pub.feed("第一帧", False, now_ms=0)) == [("u1", 1, "第一帧", False)]
    assert pub.feed("内容变了但太近", False, now_ms=199) == [], "★ 199ms 就放行（节流失效）"
    got = pub.feed("刚好到点", False, now_ms=200)
    assert tuples(got) == [("u1", 2, "刚好到点", False)], f"★ 200ms 该放行：{got}"
    assert pub.feed("又太近", False, now_ms=250) == []
    # final 在节流窗口内也必须发
    got = pub.feed("定稿在窗口内", True, now_ms=300)
    assert tuples(got) == [("u1", 3, "定稿在窗口内", True)], f"★ final 被节流吃掉了：{got}"
    # min_partial_ms=0 → 不限流（测试与低延迟场景用）
    pub2, _ = make(min_partial_ms=0)
    for i in range(5):
        assert len(pub2.feed(f"第{i}帧", False, now_ms=i)) == 1, "★ 0 应为不限流"
    print("  规则 5：partial 节流 200ms（199 拦 / 200 放）+ final 永不被节流 + 0=不限流 OK")


def test_rule6_reset_starts_new_sentence() -> None:
    """规则 6：`reset()` 封掉当前句并清空状态；下一次 `feed` 一定是新句。

    调用时机是切语言 / 会话重建 / 停止 —— 那条流整个作废了，
    半句旧文本不该再被兜底补进房间。
    """
    pub, ids = make(final_gap_s=1.0, min_partial_ms=200)
    pub.feed("说了一半", False, now_ms=0)
    pub.reset()
    got = pub.feed("说了一半", False, now_ms=10)         # 同文本、且在节流窗口内
    assert tuples(got) == [("u2", 1, "说了一半", False)], \
        f"★ reset 后没开新句（utt/rev 都没归位）：{got}"
    # reset 之后不许再给旧句补兜底 final
    pub.reset()
    got = pub.feed("很久以后", False, now_ms=1_000_000)
    assert len(got) == 1 and got[0].utt == "u3" and got[0].rev == 1, \
        f"★ reset 后还给旧句补 final：{tuples(got)}"
    assert ids.n == 3, f"句 id 消耗不对：{ids.n}"
    print("  规则 6：reset → 新句 + 状态清空 + 不补旧句 final OK")


def test_real_clock_path_does_not_crash() -> None:
    """生产路径：不注入 `now_ms` 时取墙钟，也必须正常工作（这里只冒烟，不 sleep）。"""
    pub = SourcePublisher(min_partial_ms=0)
    got = pub.feed("用真实时钟说一句", True)
    assert len(got) == 1 and got[0].is_final and len(got[0].utt) == 8, got
    assert pub.feed("再来一句", False) and pub.feed("再来一句", False) == [], "真实时钟下去重仍生效"
    print("  真实时钟路径冒烟 OK")


def test_should_publish_three_sources() -> None:
    """`should_publish`：只有「自己麦克风」那条腿该上行；loopback 必须排除。

    loopback 采的是**游戏里别人说话的声音**，发进房间 = 把别人的话二次广播，
    还会引发回环（A 转 B 的话 → B 的客户端又转回来），三个人就能刷爆房间。
    """
    assert should_publish("mic", True, True) is True
    assert should_publish("pcm:D:/rec/me.wav", True, True) is True, "录音文件当输入仍是「我说」"
    assert should_publish("loopback", True, True) is False, "★ loopback 被放进房间了（回环炸弹）"
    assert should_publish("MIC", True, True) is True, "大小写不该影响判定"
    assert should_publish("Loopback", True, True) is False
    # 开关口径：任一开关关掉都不发
    assert should_publish("mic", False, True) is False, "room.enabled=false 时不该发"
    assert should_publish("mic", True, False) is False, "broadcast_source=false 时不该发"
    assert should_publish("mic", False, False) is False
    # 脏 source：不认识的一律不发（宁可少发，不可把不明来源灌进房间）
    for bad in ("", "  ", None, "chatbox", "pcm", "mic2", "loopback:default"):
        assert should_publish(bad, True, True) is False, f"★ 脏 source {bad!r} 被放行了"
    print("  should_publish：mic/pcm 发、loopback 与脏值不发、两开关任一关即不发 OK")


def test_real_speaker_repeating_himself_is_not_swallowed() -> None:
    """★ 真人重复说同一句短话，第二遍必须照发（2026-09-28 主控复验时补）。

    去重的初衷是挡「服务端重复发 done」（毫秒级），**不能按内容一刀切**：
    「对」「好」「谢谢」「哈哈」这类短应答在对话里天然会重复，而 `engine` 那边的
    repeat 抑制**特意**只对 ≥4 字的长句生效（见 `engine._REPEAT_MIN_LEN` 的注释：
    短应答天然会连撞，当证据会误杀正常对话）—— publisher 不能把它放行的东西又吃掉。
    """
    p, _ = make(min_partial_ms=0)
    first = p.feed("对", True, now_ms=0)
    assert len(first) == 1 and first[0].is_final, first
    second = p.feed("对", True, now_ms=2000)          # 2 秒后又说了一遍
    assert len(second) == 1, f"★ 第二遍被吞了 —— 对端永远看不到这一句：{second}"
    assert second[0].text == "对" and second[0].is_final
    assert second[0].utt != first[0].utt, "重复的两句必须是两个不同的句 id"

    # 但服务端毫秒级的重复 done 仍必须挡住（同一内容、极短间隔）
    p2, _ = make(min_partial_ms=0)
    assert len(p2.feed("好的", True, now_ms=0)) == 1
    assert p2.feed("好的", True, now_ms=30) == [], "★ 机械重复 done 没被挡住"

    # 窗口可关：<=0 表示不做这层去重
    p3, _ = make(min_partial_ms=0, duplicate_final_window_ms=0)
    assert len(p3.feed("行", True, now_ms=0)) == 1
    assert len(p3.feed("行", True, now_ms=10)) == 1, "窗口为 0 时不该去重"
    print("  真人重复照发 / 机械重复 done 仍挡 / 窗口可关 OK")


def test_silence_fallback_final_is_not_duplicated() -> None:
    """★ 静默兜底补的 final 与紧接着到达的真 final，只能上屏一条。

    兜底分支调 `_make()` 时若 `_last_feed_ms` 还是**旧值**，`_last_final_ms` 会被盖成
    上一次 feed 的时间戳 → 紧接着的真 final 认不出「同一句刚刚发过」→ 房间收到两条
    一模一样的 final，对端重复上屏。
    （2026-09-28 真机演示复现：**一次** `feed(final)` 调用就吐了两条 final；
     接收端日志里同一句连续出现两条 FINAL。）
    """
    p, _ = make(final_gap_s=1.0, min_partial_ms=0)
    p.feed("半句话", False, now_ms=0)
    out = p.feed("半句话", True, now_ms=5000)     # 间隔超过 final_gap_s，兜底分支会先补一条
    finals = [i for i in out if i.is_final]
    assert len(finals) == 1, \
        f"★ 同一句发了 {len(finals)} 条 final：{[(i.utt, i.text) for i in out]}"
    assert finals[0].text == "半句话"
    print("  静默兜底 + 真 final 只上屏一条 OK")


if __name__ == "__main__":
    print("test_room_publisher:")
    test_item_is_frozen_snapshot()
    test_rule1_skip_empty_and_unchanged()
    test_rule2_rev_increases_and_new_utt_per_sentence()
    test_rule3_final_seals_even_when_text_unchanged()
    test_rule4_silence_gap_seals_previous_sentence()
    test_rule4_gap_final_uses_last_emitted_not_throttled_text()
    test_rule4_no_seal_when_nothing_published()
    test_rule5_partial_throttle_never_eats_final()
    test_rule6_reset_starts_new_sentence()
    test_real_clock_path_does_not_crash()
    test_should_publish_three_sources()
    test_real_speaker_repeating_himself_is_not_swallowed()
    test_silence_fallback_final_is_not_duplicated()
    print("ALL PASSED")
