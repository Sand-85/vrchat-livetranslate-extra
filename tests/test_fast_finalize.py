#!/usr/bin/env python
"""「快封句」验收：上游也没人说话了 → 不白等那 3s（但绝不在人还在说时抢跑）。

## 为什么要有它（以及它跟被回滚的那版差在哪）

服务端在人停止说话后 **8s 内一条事件都不发**（既无 `response.text.done` 也无
`response.done`）→ 没有语义完成信号可用，只能靠定时器；而累计译文在「说完前 0.74~0.89s」
就不再增长 → 那 3s 全是白等。所以给静默兜底加一条**快路径**：文字静默 ≥1.1s 且
**上游也没有人在说话** ≥0.5s → 立刻封句。

⚠️ 「上游没有人在说话」这个信号**必须用「电平」**（`_SessionProxy` 已经在算的
`peak >= SILENCE_PEAK` → `session.note_voice()`），**不能**用「距上次上送音频的间隔」：
麦克风腿没有闸门（`_SilenceGate` 只挂在环回腿上、阈值默认 30s），人说话时静音块照样
每 ~0.1s 上送一次 → 那个间隔恒为 ~0.1s，快路径永远不触发（实测：
`out/check_pr49_mic_signal.py`：连续喂 4s 静音，间隔最大 0.113s）。
本文件第 3 条用例就是钉这个：**用真实 `_SessionProxy` 喂静音块，`user_quiet_s()` 不许长大**。
"""
from __future__ import annotations

import asyncio
import math
import struct
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.session.base import (DEFAULT_FAST_FINAL_SILENCE_S,      # noqa: E402
                              DEFAULT_FAST_FINAL_USER_QUIET_S,
                              DEFAULT_FINAL_SILENCE_S, SessionConfig, should_finalize)
from vlt.session.qwen38 import QwenLiveTranslateSession          # noqa: E402

CHUNK_SAMPLES = 1600                       # 100ms @16k 单声道
SPEECH_CHUNKS = 10                         # 1.0s 说话
SILENCE_CHUNKS = 30                        # 之后 3.0s 不说话
_TONE = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / 16000)))
                 for i in range(CHUNK_SAMPLES))
_SIL = b"\x00\x00" * CHUNK_SAMPLES


def _fmt(sec: float | None) -> str:
    return "None（没收到过信号）" if sec is None else f"{sec:.2f}s"


def test_pure_table() -> bool:
    ok = True
    cases = [
        # (文字静默, 上游静默, 期望, 说明)
        (3.10, 0.30, True,  "慢路径：人还在说，但文字静默过了 3s → 照旧封"),
        (2.30, 0.20, False, "**不抢跑**：说话中间那个 2.3s 大间隔（上游还在说）"),
        (1.20, 1.00, True,  "快路径：上游静了 1s + 文字静默 1.2s → 封（省 ~1.8s）"),
        (1.20, 0.20, False, "**不抢跑**：文字静默够了但人还在说"),
        (0.90, 5.00, False, "上游静很久了，但文字才静 0.9s（服务端可能还在追）"),
        (1.10, 0.50, True,  "正好卡在阈值上（≥ 即封）"),
        (1.09, 0.50, False, "差一点就不封"),
    ]
    for text_q, user_q, want, why in cases:
        got = should_finalize(text_quiet_s=text_q, user_quiet_s=user_q)
        cond = got is want
        print(f"  文字静默={text_q:.2f}s 上游静默={user_q:.2f}s → {got}"
              f"（期望 {want}）{'OK' if cond else '✗'}  {why}")
        ok &= cond
    return ok


def test_pure_edges() -> bool:
    ok = True
    # 从没收到过「有人说话」的信号（没声音 / 信号没接上）→ **保守走慢路径**，绝不早封：
    # 抢跑会把半句当最终版；而「从没说过话」时本来也没文本可封。
    got = should_finalize(text_quiet_s=1.2, user_quiet_s=None)
    cond = got is False
    print(f"  从没收到「有人说话」信号 + 文字静默 1.2s → {got}（期望 False：保守走慢路径）"
          f"  {'OK' if cond else '✗'}")
    ok &= cond
    # 关掉快路径（fast_final_silence_s=None）→ 退回纯 3.0s 行为
    got = should_finalize(text_quiet_s=1.2, user_quiet_s=9.9, fast_silence_s=None)
    cond = got is False
    print(f"  关掉快路径 + 文字静默 1.2s → {got}（期望 False）  {'OK' if cond else '✗'}")
    ok &= cond
    got = should_finalize(text_quiet_s=3.1, user_quiet_s=9.9, fast_silence_s=None)
    cond = got is True
    print(f"  关掉快路径 + 文字静默 3.1s → {got}（期望 True，慢路径还在）  {'OK' if cond else '✗'}")
    ok &= cond
    # 阈值可调
    got = should_finalize(text_quiet_s=0.6, user_quiet_s=1.0, fast_silence_s=0.55)
    cond = got is True
    print(f"  自定义快阈值 0.55s + 文字静默 0.6s → {got}（期望 True）  {'OK' if cond else '✗'}")
    ok &= cond
    # 默认值一致性（config 默认 1.1 / 0.5，别被悄悄改大）
    cond = (DEFAULT_FAST_FINAL_SILENCE_S == 1.1 and DEFAULT_FAST_FINAL_USER_QUIET_S == 0.5
            and DEFAULT_FINAL_SILENCE_S == 3.0)
    print(f"  默认阈值：快={DEFAULT_FAST_FINAL_SILENCE_S}s/上游={DEFAULT_FAST_FINAL_USER_QUIET_S}s"
          f"，慢={DEFAULT_FINAL_SILENCE_S}s  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


class _FakeWS:
    async def send(self, _data) -> None:
        return None


class _FakeCfg:
    session_base = {"silence_gate_enabled": True, "silence_gate_after_s": 30.0,
                    "silence_gate_preroll_s": 1.0}


class _FakeEngine:
    """喂给真实 `_SessionProxy`：只提供它要读的字段。"""

    _cfg = _FakeCfg()
    _session = None
    _audio_in_chunks = 0
    _silent_chunks = 0
    _last_loud_ts = 0.0
    _silence_gate_cfg = None


def test_proxy_reports_voice_by_level() -> bool:
    """★ 核心回归：信号必须来自**电平**，不是「距上次上送音频的间隔」。

    真实 `_SessionProxy` 喂「一块响 + 连着一串静音」：
      - 响的那块之后 `user_quiet_s()` 很小（证明电平信号到了会话）；
      - 之后**静音块照样在往上送**时，`user_quiet_s()` 必须**继续变大** ——
        若哪天有人把它改回「上送间隔」，这里就会变成恒 ~0.0s（把静音当「还在说话」），
        或者反过来永远不触发快路径。
    """
    from vlt.engine import _SessionProxy

    async def run() -> bool:
        ok = True
        sess = QwenLiveTranslateSession(SessionConfig(api_key="x"))
        sess._ws = _FakeWS()
        eng = _FakeEngine()
        eng._session = sess
        proxy = _SessionProxy(eng)

        # ① 从未说过话 → None（按「已静」看待）
        cond = sess.user_quiet_s() is None
        print(f"  没说过话时 user_quiet_s() = {sess.user_quiet_s()}（期望 None）  {'OK' if cond else '✗'}")
        ok &= cond

        # ② 一块「响」→ 会话收到「有人在说」
        await proxy.send_audio(_TONE)
        q = sess.user_quiet_s()
        cond = q is not None and q < 0.2
        print(f"  喂一块响的 → user_quiet_s()={_fmt(q)}（期望 <0.2s）  {'OK' if cond else '✗'}")
        ok &= cond

        # ③ 接着连喂静音块（真实麦克风腿就是这样：不说话也每 100ms 送一块）
        for _ in range(5):
            await proxy.send_audio(_SIL)
            await asyncio.sleep(0.1)
        q = sess.user_quiet_s()
        cond = q is not None and q >= 0.4
        print(f"  之后 0.5s 全是静音块（仍在往上送）→ user_quiet_s()={_fmt(q)}"
              f"（期望 ≥0.4s：静音就该长）  {'OK' if cond else '✗'}")
        ok &= cond

        # ④ 又响了一块 → 重新计时
        await proxy.send_audio(_TONE)
        q = sess.user_quiet_s()
        cond = q is not None and q < 0.2
        print(f"  再喂一块响的 → user_quiet_s()={_fmt(q)}（期望 <0.2s）  {'OK' if cond else '✗'}")
        ok &= cond
        return ok

    return asyncio.run(run())


class _FakeSource:
    """按实时节奏吐块（100ms 一块，先响后静）；静音期间**照样**有块。"""

    rate = 16000
    channels = 1

    def __init__(self) -> None:
        self.seq = [_TONE] * SPEECH_CHUNKS + [_SIL] * SILENCE_CHUNKS
        self.i = 0
        self.speech_end_at: float | None = None

    async def read(self, timeout: float = 1.0):
        await asyncio.sleep(0.1)
        if self.i >= len(self.seq):
            return _SIL                     # 采集流不会停：一直有静音块（真实麦克风就是这样）
        chunk = self.seq[self.i]
        self.i += 1
        if self.i == SPEECH_CHUNKS:         # 刚送完最后一块「说话」
            self.speech_end_at = time.perf_counter()
        return chunk

    def close(self) -> None:
        return None


def _run_e2e(fast_silence_s: float | None) -> tuple[float, float, float | None] | None:
    """真链路复刻：`_pump_capture` 喂「1s 说话 + 3s 静音」+ 真实 `tick()`。

    返回 (说完 → 封句的秒数, 封句瞬间文字静默, 封句瞬间上游静默)。
    """
    from vlt.engine import _SessionProxy, _pump_capture

    async def run() -> tuple[float, float, float | None] | None:
        sess = QwenLiveTranslateSession(
            SessionConfig(api_key="x", fast_final_silence_s=fast_silence_s))
        sess._ws = _FakeWS()
        eng = _FakeEngine()
        eng._session = sess
        proxy = _SessionProxy(eng)

        finals: list[tuple[float, float, float | None]] = []

        def on_text(d) -> None:
            if d.is_final:
                now = time.perf_counter()
                finals.append((now, now - sess._last_text_at, sess.user_quiet_s(now)))

        sess.on_text = on_text
        # 模拟「服务端已经把译文吐完了」：最后一条增量就在起跑线上
        sess._buf = ["你好，世界。"]
        sess._last_text_at = time.perf_counter()

        source = _FakeSource()
        stop = threading.Event()

        async def ticker() -> None:
            stamped = False
            while not stop.is_set():
                if source.speech_end_at is not None and not stamped:
                    # 真机日志：最后一片译文在说完**前 0.74~0.89s** 就到齐 → 这里按 0.8s 复刻
                    sess._last_text_at = source.speech_end_at - 0.8
                    stamped = True
                sess.tick()
                await asyncio.sleep(0.1)

        async def watchdog() -> None:
            await asyncio.sleep(5.0)
            stop.set()

        tk = asyncio.create_task(ticker())
        wd = asyncio.create_task(watchdog())
        await _pump_capture(proxy, None, 0.0, stop, "mic", lambda: source, None)
        stop.set()
        wd.cancel()
        await tk
        if source.speech_end_at is None or not finals:
            return None
        at, tq, uq = finals[0]
        return (at - source.speech_end_at, tq, uq)

    return asyncio.run(run())


def test_end_to_end_fast_path() -> bool:
    """★ 端到端：走真实采集泵（静音块持续上送）时，快路径**真的**要生效。"""
    ok = True
    got = _run_e2e(DEFAULT_FAST_FINAL_SILENCE_S)
    if got is None:
        print("  ✗ 没拿到完整时间线（无法判定）")
        return False
    fast, tq, uq = got
    cond = fast < 2.0
    print(f"  默认开快路径：说完 → 封句 +{fast:.2f}s（封句瞬间：文字静默 {tq:.2f}s、"
          f"上游静默 {_fmt(uq)}）  期望 <2.0s  {'OK' if cond else '✗'}")
    ok &= cond

    got = _run_e2e(None)
    if got is None:
        print("  ✗ 关掉快路径那次没拿到时间线")
        return False
    slow, tq2, _ = got
    # 慢路径 = 文字静默满 3.0s；而最后一片译文在说完前 0.8s 就到了 → 实测 ~+2.2s
    cond = slow >= 2.0
    print(f"  关掉快路径：说完 → 封句 +{slow:.2f}s（文字静默 {tq2:.2f}s）  期望 ≥2.0s  "
          f"{'OK' if cond else '✗'}")
    ok &= cond
    cond = slow - fast >= 1.0
    print(f"  快路径的收益 = {slow - fast:.2f}s（期望 ≥1.0s；真机日志里省 ~1.8s）  "
          f"{'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_config_parsing() -> bool:
    """配置口径：非法值**留痕 + 回落默认值**（不许静默关掉）；`null` 才是明确关掉。"""
    ok = True
    import io
    import contextlib

    from vlt.config import _float_or_default, _opt_float

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = _opt_float("1,1", DEFAULT_FAST_FINAL_SILENCE_S, key="session.fast_final_silence_s")
    log = buf.getvalue()
    cond = got == DEFAULT_FAST_FINAL_SILENCE_S and "⚠️" in log and "fast_final_silence_s" in log
    print(f"  fast_final_silence_s='1,1' → {got}（期望回落 {DEFAULT_FAST_FINAL_SILENCE_S}）+ 留痕  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    cond = _opt_float(None, DEFAULT_FAST_FINAL_SILENCE_S, key="k") is None
    cond &= _opt_float("", DEFAULT_FAST_FINAL_SILENCE_S, key="k") is None
    print(f"  null / 空串 → None（明确关掉快路径）  {'OK' if cond else '✗'}")
    ok &= cond

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = _float_or_default("abc", DEFAULT_FAST_FINAL_USER_QUIET_S,
                                key="session.fast_final_user_quiet_s")
    cond = got == DEFAULT_FAST_FINAL_USER_QUIET_S and "⚠️" in buf.getvalue()
    print(f"  fast_final_user_quiet_s='abc' → {got}（期望回落默认值）+ 留痕  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


if __name__ == "__main__":
    print("test_fast_finalize:")
    results = [
        ("纯函数判据表", test_pure_table()),
        ("纯函数边界", test_pure_edges()),
        ("代理按电平上报（真实 _SessionProxy）", test_proxy_reports_voice_by_level()),
        ("端到端（真实采集泵 + tick）", test_end_to_end_fast_path()),
        ("配置解析", test_config_parsing()),
    ]
    bad = [name for name, ok in results if not ok]
    for name, ok in results:
        print(f"  {'✓' if ok else '✗'} {name}")
    if bad:
        print(f"快封句用例失败（见上）：{', '.join(bad)}")
        sys.exit(1)
    print("ALL PASSED")
