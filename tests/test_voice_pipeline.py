#!/usr/bin/env python
"""B 模式出声流水线验收：合成并发 + 写入单写者有序 + 提前量。

## 为什么这么设计（2026-10-01 实测）

分段合成之后，**每段都要付一次 TTS 首包（~0.6s）**。原来的单飞 worker 是
「一段合成完（分片全写完）才开始下一段」→ 每段的首包都裸露在延迟里，
连续说话还会积压、撞上虚拟声卡 `max_buffer_ms` 的整句丢弃（听起来漏句）。

现在：**合成可并发**（`output.audio.tts_parallel`，实测服务端接受 3 路且首包不退化），
**写入永远一个写者、严格按排队顺序** —— 分片不会交错（当初必须串行的唯一原因就是它），
而下一段的首包被藏进上一段的播放里。实测（真 TTS，3 段）：第 3 段起播 3.25s → 2.59s。

全程离线：TTS 与音频设备都被替身接住。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import vlt.engine as engine_mod                                  # noqa: E402
from vlt.config import AppConfig, Direction                      # noqa: E402
from vlt.engine import Engine, EngineEvents                      # noqa: E402


class FakeVirtualMic:
    """记下每片被推入的时刻（用来验「段与段不交错」与「提前量」）。"""

    def __init__(self) -> None:
        self.pushed: list[tuple[float, int]] = []       # (时刻, 段号)
        self.sentences = 0

    def push(self, pcm: bytes) -> None:
        self.pushed.append((time.perf_counter(), int.from_bytes(pcm[:2], "little")))

    def end_sentence(self) -> None:
        self.sentences += 1

    def close(self) -> None:
        pass


def _mk(*, parallel: int) -> Engine:
    cfg = AppConfig(
        session_base={"api_key": "sk-test", "model": "qwen3.8-livetranslate-flash-realtime",
                      "base_url": "wss://example.invalid"},
        directions={"mine": Direction(source_lang="zh", target_lang="en", output_audio=True),
                    "theirs": Direction(source_lang="en", target_lang="zh", output_audio=False)},
        chatbox={"max_chars": 144}, merger={},
        output={"audio": {"enabled": True, "mode": "tts", "tts_parallel": parallel}},
        text_input={"model": "qwen-mt-flash",
                    "tts": {"enabled": True, "stream": True,
                            "model": "qwen3-tts-flash", "voice": "Cherry"}},
    )
    eng = Engine(cfg=cfg, direction="mine", source="mic", sinks={"chatbox"},
                 events=EngineEvents(), dry_run=True)
    eng._virtualmic = FakeVirtualMic()
    return eng


class _Harness:
    """替身 TTS：每段的合成耗时/分片数可调，并记录合成起止时刻。"""

    def __init__(self, *, per_seg_delay: float = 0.06, chunks: int = 4, fail_on: str = "",
                 truncate_on: str = "") -> None:
        self.delay = per_seg_delay
        self.chunks = chunks
        self.fail_on = fail_on
        self.truncate_on = truncate_on
        self.inflight = 0
        self.max_inflight = 0
        self.span: dict[int, tuple[float, float]] = {}      # 段号 → (合成开始, 合成结束)
        self.real = engine_mod.synthesize_stream

    def install(self) -> None:
        engine_mod.synthesize_stream = self._stream

    def restore(self) -> None:
        engine_mod.synthesize_stream = self.real

    def _stream(self, text: str, **kw):                     # noqa: ANN001
        idx = ord(text[0])
        t0 = time.perf_counter()
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)

        def gen():
            try:
                if self.fail_on and text.startswith(self.fail_on):
                    raise engine_mod.TtsError("替身：合成失败")
                if self.truncate_on and text.startswith(self.truncate_on):
                    yield idx.to_bytes(2, "little") * 1200
                    raise engine_mod.TtsStreamTruncated("替身：中途断流")
                for _ in range(self.chunks):
                    time.sleep(self.delay)
                    # 整段填同一个样本值 = 段号（升采样保首样本，故推入时能认出是谁）
                    yield idx.to_bytes(2, "little") * 1200
            finally:
                self.inflight -= 1
                self.span[idx] = (t0, time.perf_counter())

        return gen()


def _run(eng: Engine, texts: list[str], *, wait: float = 6.0) -> float:
    """把 texts 逐条排进流水线并等它跑完，返回结束时刻（perf_counter）。"""

    async def scenario() -> None:
        eng._loop = asyncio.get_running_loop()
        for t in texts:
            eng._schedule_voice_speak(t)
        deadline = time.perf_counter() + wait
        while time.perf_counter() < deadline:
            if not eng._voice_speaking and not eng._voice_slots and not eng._voice_pending:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    return time.perf_counter()


def _grouped(mic: FakeVirtualMic) -> list[int]:
    order = [seg for _, seg in mic.pushed]
    return [k for i, k in enumerate(order) if i == 0 or order[i - 1] != k]


# ---------------------------------------------------------------- 用例


def test_order_not_interleaved() -> bool:
    """第 2 段的合成比第 1 段快（延迟错开）→ 写入仍必须是 1 然后 2，绝不交错。"""
    ok = True

    class Skewed(_Harness):
        def _stream(self, text: str, **kw):                 # noqa: ANN001
            idx = ord(text[0])
            self.delay = 0.02 if idx == ord("2") else 0.12   # 第 2 段合成更快
            return super()._stream(text, **kw)

    h = Skewed(chunks=3)
    eng = _mk(parallel=2)
    h.install()
    try:
        _run(eng, ["1第一句。", "2第二句。"])
    finally:
        h.restore()
    g = _grouped(eng._virtualmic)
    cond = g == [ord("1"), ord("2")]
    print(f"  合成时长错开（2 更快）时，推入段序={[chr(k) for k in g]}（必须 1→2）  "
          f"{'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_parallel_really_happens_and_prefetch() -> bool:
    """并发确实发生（=2）且**下一段在前一段还没写完时就开始合成**（提前量）。"""
    ok = True
    h = _Harness(per_seg_delay=0.08, chunks=5)
    eng = _mk(parallel=2)
    h.install()
    try:
        _run(eng, ["1第一句。", "2第二句。"])
    finally:
        h.restore()

    cond = h.max_inflight == 2
    print(f"  合成并发上限={h.max_inflight}（tts_parallel=2 → 应为 2）  {'OK' if cond else '✗'}")
    ok &= cond

    mic = eng._virtualmic
    last_push_1 = max(t for t, seg in mic.pushed if seg == ord("1"))
    start_2 = h.span[ord("2")][0]
    cond = start_2 < last_push_1
    print(f"  第 2 段在「第 1 段最后一处分片推完」之前就已开始合成"
          f"（{start_2 - last_push_1:+.3f}s）  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_parallel_one_is_serial() -> bool:
    """tts_parallel=1 → 退回纯串行（并发上限 1），但顺序照旧正确。"""
    ok = True
    h = _Harness(chunks=3)
    eng = _mk(parallel=1)
    h.install()
    try:
        _run(eng, ["1第一句。", "2第二句。", "3第三句。"])
    finally:
        h.restore()
    cond = h.max_inflight == 1
    print(f"  tts_parallel=1：并发上限={h.max_inflight}（应为 1）  {'OK' if cond else '✗'}")
    ok &= cond
    g = _grouped(eng._virtualmic)
    cond = g == [ord("1"), ord("2"), ord("3")]
    print(f"  段序={[chr(k) for k in g]}（应为 1→2→3）  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_backpressure_keeps_order() -> bool:
    """一次塞 5 段：槽位不超过 tts_parallel+1（背压），且 5 段最终全部按序念完。"""
    ok = True
    h = _Harness(chunks=2)
    eng = _mk(parallel=2)
    h.install()
    try:
        _run(eng, ["1甲。", "2乙。", "3丙。", "4丁。", "5戊。"], wait=8.0)
    finally:
        h.restore()
    g = _grouped(eng._virtualmic)
    cond = g == [ord(c) for c in "12345"]
    print(f"  5 段全部按序念完：段序={[chr(k) for k in g]}  {'OK' if cond else '✗'}")
    ok &= cond
    cond = h.max_inflight <= eng._tts_parallel()
    print(f"  并发受限（{h.max_inflight} ≤ {eng._tts_parallel()}）  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_error_and_truncation_are_loud_and_do_not_block() -> bool:
    """某段合成失败 / 中途断流 → 必须留痕（warn）且不拖死后续段落。"""
    ok = True
    msgs: list[tuple[str, str]] = []
    h = _Harness(chunks=2, fail_on="2", truncate_on="3")
    eng = _mk(parallel=2)
    eng._events = EngineEvents(on_status=lambda lv, m: msgs.append((lv, m)))
    h.install()
    try:
        _run(eng, ["1第一句。", "2第二句。", "3第三句。"])
    finally:
        h.restore()
    warns = [m for lv, m in msgs if lv == "warn"]
    cond = any("语音译音失败" in m for m in warns)
    print(f"  合成失败留痕：{warns[:1]}  {'OK' if cond else '✗'}")
    ok &= cond
    cond = any("只念了一半" in m for m in warns)
    print(f"  中途断流留痕：{[m for m in warns if '只念了一半' in m][:1]}  {'OK' if cond else '✗'}")
    ok &= cond
    g = _grouped(eng._virtualmic)
    cond = ord("1") in g and ord("3") in g            # 第 3 段（截断那段）仍被念出来
    print(f"  出错后不拖死后续：推入段序={[chr(k) for k in g]}  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


if __name__ == "__main__":
    print("test_voice_pipeline:")
    print(" 1) 合成时长错开也不交错")
    ok = test_order_not_interleaved()
    print(" 2) 并发确实发生 + 提前量（下一段提前合成）")
    ok &= test_parallel_really_happens_and_prefetch()
    print(" 3) tts_parallel=1 退回串行")
    ok &= test_parallel_one_is_serial()
    print(" 4) 背压：5 段仍按序念完、并发受限")
    ok &= test_backpressure_keeps_order()
    print(" 5) 失败/截断留痕且不阻塞后续")
    ok &= test_error_and_truncation_are_loud_and_do_not_block()
    assert ok, "出声流水线用例失败（见上）"
    print("ALL PASSED")
