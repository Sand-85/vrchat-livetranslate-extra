"""chatbox 方向收敛回归测试。

验证 chatbox 只对 direction="mine" 生效：
  1) direction="theirs" + sinks={"chatbox"} → 不创建 chatbox、不往 chatbox 发任何东西
  2) direction="mine" + sinks={"chatbox"} → 照常创建并使用 chatbox（防修过头）
  3) mine 方向的 send_text() 仍能把译文送进 chatbox
  4) _on_text 在 theirs 方向不往 merger 推送

全程离线：不连服务端、不碰 Tk。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vlt.engine as engine_mod
from vlt.config import AppConfig, Direction
from vlt.engine import Engine, EngineEvents
from vlt.output.merger import Merger
from vlt.session.base import TextDelta


# ---------------------------------------------------------------- 测试替身


class FakeChatbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []

    def send(self, text: str, is_final: bool = False) -> bool:
        self.sent.append((text, is_final))
        return True


def _mk_engine(direction: str = "mine", sinks: set[str] | None = None) -> Engine:
    cfg = AppConfig(
        session_base={"api_key": "sk-test", "model": "qwen3.8-livetranslate-flash-realtime"},
        directions={
            "mine": Direction(source_lang="zh", target_lang="en"),
            "theirs": Direction(source_lang="en", target_lang="zh"),
        },
        chatbox={"max_chars": 144},
        merger={},
        text_input={"model": "qwen-mt-flash", "timeout_s": 5},
    )
    return Engine(
        cfg=cfg, direction=direction,
        source="mic" if direction == "mine" else "loopback",
        sinks=sinks or {"chatbox"},
        events=EngineEvents(),
        dry_run=True,
    )


# ---------------------------------------------------------------- 1) theirs 不创建 chatbox


def test_theirs_no_chatbox() -> bool:
    ok = True

    eng = _mk_engine("theirs", {"chatbox"})
    cond = eng._chatbox_wanted is False
    print(f"  theirs + chatbox in sinks → _chatbox_wanted={eng._chatbox_wanted}  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    cond = eng.chatbox is None
    print(f"  theirs → engine.chatbox is None (未启动) = {eng.chatbox is None}  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    # 模拟 _build_and_run 已跑过但没创建 chatbox 的状态：手动调 _on_text
    fake_cb = FakeChatbox()
    eng._chatbox = None
    eng._merger = Merger(sink=lambda text, is_final: fake_cb.send(text, is_final) if eng._chatbox else None,
                         interval_s=2.0)
    d = TextDelta(confirmed="Hello", is_final=True, source="Hello")
    eng._on_text(d)
    cond = fake_cb.sent == []
    print(f"  theirs _on_text → chatbox 收到 {len(fake_cb.sent)} 条  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    return ok


# ---------------------------------------------------------------- 2) mine 照常使用 chatbox


def test_mine_still_uses_chatbox() -> bool:
    ok = True

    eng = _mk_engine("mine", {"chatbox"})
    cond = eng._chatbox_wanted is True
    print(f"  mine + chatbox in sinks → _chatbox_wanted={eng._chatbox_wanted}  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    fake_cb = FakeChatbox()
    eng._chatbox = fake_cb
    eng._merger = Merger(sink=lambda text, is_final: fake_cb.send(text, is_final),
                         interval_s=2.0)
    d = TextDelta(confirmed="Hello", is_final=True, source="你好")
    eng._on_text(d)
    cond = len(fake_cb.sent) > 0
    print(f"  mine _on_text → chatbox 收到 {len(fake_cb.sent)} 条  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    return ok


# ---------------------------------------------------------------- 3) mine send_text 仍送 chatbox


def test_mine_send_text_to_chatbox() -> bool:
    ok = True
    real = engine_mod.translate_text

    eng = _mk_engine("mine")
    fake_cb = FakeChatbox()
    eng._chatbox = fake_cb
    eng._loop = asyncio.new_event_loop()
    eng._thread = type("T", (), {"is_alive": lambda self: True})()
    engine_mod.translate_text = lambda text, **kw: "Hello from typing"
    try:
        asyncio.run(eng._async_send_text("你好"))
    finally:
        engine_mod.translate_text = real
        eng._loop.close()

    cond = len(fake_cb.sent) > 0 and all(t for t, _ in fake_cb.sent)
    print(f"  mine send_text → chatbox 收到 {len(fake_cb.sent)} 条  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    return ok


# ---------------------------------------------------------------- 4) theirs send_text 被拒（方向不对）


def test_theirs_send_text_rejected() -> bool:
    eng = _mk_engine("theirs")
    eng._loop = asyncio.new_event_loop()
    eng._thread = type("T", (), {"is_alive": lambda self: True})()
    result = eng.send_text("Hello")
    eng._loop.close()
    cond = result is False
    print(f"  theirs send_text → 返回 {result}（应为 False）  "
          f"{'OK' if cond else '✗'}")
    return cond


# ---------------------------------------------------------------- 5) theirs 方向即使手动塞了 chatbox 也不走 merger


def test_theirs_merger_not_pushed() -> bool:
    eng = _mk_engine("theirs", {"chatbox"})
    fake_cb = FakeChatbox()
    eng._chatbox = fake_cb
    eng._merger = Merger(sink=lambda text, is_final: fake_cb.send(text, is_final),
                         interval_s=2.0)

    d1 = TextDelta(confirmed="Hi", is_final=False, source="Hi")
    d2 = TextDelta(confirmed="Hello world", is_final=True, source="Hello world")
    eng._on_text(d1)
    eng._on_text(d2)

    cond = fake_cb.sent == []
    print(f"  theirs 即使有 chatbox 实例，merger 也不推送 → chatbox 收到 {len(fake_cb.sent)} 条  "
          f"{'OK' if cond else '✗'}")
    return cond


# ---------------------------------------------------------------- 6) 气泡显示原文 / 译文
#
# 需求：主界面第二行 chatbox 右边那颗按钮，把**只送 chatbox** 的文本在
# 「译文（默认）」和「原文（ASR 源文）」之间互斥切换；点一下立即生效（同一次会话内）。


def _with_mode(eng: Engine, mode: str) -> Engine:
    eng._cfg.ui["chatbox_text"] = mode
    return eng


def test_mode_default_and_dirty_fallback() -> bool:
    """`ui.chatbox_text` 缺失 / 脏值 → 一律回落 translated（并留一行痕）。"""
    ok = True

    eng = _mk_engine("mine")                      # ui 段为空 = 键缺失
    cond = eng.chatbox_text_mode == "translated"
    print(f"  ui 段为空 → chatbox_text_mode={eng.chatbox_text_mode!r}  {'OK' if cond else '✗'}")
    ok &= cond

    eng = _with_mode(_mk_engine("mine"), "source")
    cond = eng.chatbox_text_mode == "source"
    print(f"  'source' → chatbox_text_mode={eng.chatbox_text_mode!r}  {'OK' if cond else '✗'}")
    ok &= cond

    engine_mod._CHATBOX_TEXT_WARNED.clear()
    eng = _with_mode(_mk_engine("mine"), "原文")   # 脏值（不是合法枚举）
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = eng.chatbox_text_mode
    cond = got == "translated" and "chatbox_text" in buf.getvalue()
    print(f"  脏值 → {got!r}，且留痕  {'OK' if cond else '✗'}")
    ok &= cond

    return ok


def test_mode_source_sends_source_not_translation() -> bool:
    """原文模式：chatbox 收到 ASR 源文，**不是**译文。"""
    ok = True

    eng = _with_mode(_mk_engine("mine"), "source")
    fake_cb = FakeChatbox()
    eng._chatbox = fake_cb
    eng._merger = Merger(sink=lambda text, is_final: fake_cb.send(text, is_final), interval_s=2.0)

    eng._on_text(TextDelta(confirmed="Hello", is_final=True, source="你好"))
    got = [t for t, _ in fake_cb.sent]
    cond = got == ["你好"]
    print(f"  原文模式 → chatbox 收到 {got!r}（应为 ['你好']，不含译文）  {'OK' if cond else '✗'}")
    ok &= cond

    return ok


def test_mode_source_empty_skips_silently_but_logs() -> bool:
    """原文为空 → **本条不发**：不进 chatbox、不入补发队列、不占位；
    只往终端/日志留一行（绝不进状态栏 —— 状态栏是 `_events.on_status`，这里不碰）。"""
    ok = True

    eng = _with_mode(_mk_engine("mine"), "source")
    fake_cb = FakeChatbox()
    eng._chatbox = fake_cb
    eng._merger = Merger(sink=lambda text, is_final: fake_cb.send(text, is_final), interval_s=2.0)
    statuses: list = []
    eng._events.on_status = lambda lvl, msg: statuses.append((lvl, msg))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        eng._on_text(TextDelta(confirmed="Hello", is_final=True, source=""))
        eng._on_text(TextDelta(confirmed="Hello again", is_final=False, source="   "))
    log = buf.getvalue()

    cond = fake_cb.sent == []
    print(f"  源文为空 → chatbox 收到 {len(fake_cb.sent)} 条（应为 0）  {'OK' if cond else '✗'}")
    ok &= cond

    cond = eng._merger.sent_count == 0 and eng._merger._last_sent == ""
    print(f"  源文为空 → 无补发队列残留（sent_count={eng._merger.sent_count}）  {'OK' if cond else '✗'}")
    ok &= cond

    cond = log.count("原文为空，本条跳过") == 2
    print(f"  源文为空 → 终端/日志留痕 {log.count('原文为空，本条跳过')} 行（每条都打，不限频，应为 2）  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    cond = not any("原文为空" in str(m) for _, m in statuses)
    print(f"  源文为空 → 状态栏 {len(statuses)} 条消息里不含该留痕（不许渗到界面）  {'OK' if cond else '✗'}")
    ok &= cond

    cond = fake_cb.sent == []
    print(f"  源文为空 → 气泡里没有任何占位文本  {'OK' if cond else '✗'}")
    ok &= cond

    return ok


def test_mode_source_typed_input_sends_raw_text() -> bool:
    """原文模式下打字输入：chatbox 收到你**敲的那句字**（翻译照做，只是不进气泡）。"""
    ok = True
    real = engine_mod.translate_text

    eng = _with_mode(_mk_engine("mine"), "source")
    fake_cb = FakeChatbox()
    eng._chatbox = fake_cb
    eng._loop = asyncio.new_event_loop()
    eng._thread = type("T", (), {"is_alive": lambda self: True})()
    engine_mod.translate_text = lambda text, **kw: "Hello from typing"
    try:
        asyncio.run(eng._async_send_text("你好"))
    finally:
        engine_mod.translate_text = real
        eng._loop.close()

    got = [t for t, _ in fake_cb.sent]
    cond = got == ["你好"]
    print(f"  原文模式 send_text('你好') → chatbox 收到 {got!r}（不含译文）  {'OK' if cond else '✗'}")
    ok &= cond

    return ok


def test_mode_switch_takes_effect_in_same_session() -> bool:
    """点击即生效：**同一个引擎实例**先译文后原文，两条各按当时模式出 —— 不重建会话。"""
    ok = True

    eng = _mk_engine("mine")                       # 默认 translated
    fake_cb = FakeChatbox()
    eng._chatbox = fake_cb
    eng._merger = Merger(sink=lambda text, is_final: fake_cb.send(text, is_final), interval_s=2.0)

    eng._on_text(TextDelta(confirmed="Hello", is_final=True, source="你好"))
    eng._cfg.ui["chatbox_text"] = "source"         # 界面点一下：只改内存 cfg
    eng._on_text(TextDelta(confirmed="Good night", is_final=True, source="晚安"))

    got = [t for t, _ in fake_cb.sent]
    cond = got == ["Hello", "晚安"]
    print(f"  切换前后各一条 → chatbox 收到 {got!r}（应为 ['Hello', '晚安']）  {'OK' if cond else '✗'}")
    ok &= cond

    return ok


# ---------------------------------------------------------------- 主入口


def main() -> int:
    print("test_chatbox_direction:")
    results = [
        ("theirs 不创建 chatbox", test_theirs_no_chatbox()),
        ("mine 照常使用 chatbox", test_mine_still_uses_chatbox()),
        ("mine send_text 送 chatbox", test_mine_send_text_to_chatbox()),
        ("theirs send_text 被拒", test_theirs_send_text_rejected()),
        ("theirs merger 不推送", test_theirs_merger_not_pushed()),
        ("气泡文本模式：默认/脏值回落", test_mode_default_and_dirty_fallback()),
        ("气泡原文模式：送源文不送译文", test_mode_source_sends_source_not_translation()),
        ("气泡原文模式：源文为空则不发声", test_mode_source_empty_skips_silently_but_logs()),
        ("气泡原文模式：打字送原字", test_mode_source_typed_input_sends_raw_text()),
        ("气泡文本模式：点击即生效（同会话）", test_mode_switch_takes_effect_in_same_session()),
    ]
    bad = [name for name, ok in results if not ok]
    print("ALL PASSED" if not bad else f"FAILED: {', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
