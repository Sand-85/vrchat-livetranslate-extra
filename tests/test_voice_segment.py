#!/usr/bin/env python
"""B 模式「分段合成」验收：不等终版，累计译文里一出现完整分句就先合成。

## 为什么要有这个（2026-10-01 真机实测）

用户真机测「说完 → VRChat 里听到译音」= **4~5 秒**。真链路探针（3 句样本）把它拆开了：

    -2.7s ~ -4.8s   首句累计译文已经**定型**（用户还在说）
    +3.5s           `response.text.done`（终版）才到 —— 判停 + 响应结束要这么久
    → 现状：终版(3.5s) + TTS 首包(0.6s) + 抖动缓冲(0.3s) ≈ 4.4s 才开口

也就是说 B 模式「等终版」白等 ~3.5s。而实测累计文本**只增不改**（前缀扩展，0 次改写），
所以可以**在分句边界处提前合成**：开口时间≈首句定型 + TTS 首包，通常落在**用户还没说完**时。

全程离线（无网络、无音频设备）：引擎的 IO 全被替身接住。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.config import AppConfig, Direction                      # noqa: E402
from vlt.engine import Engine, EngineEvents                      # noqa: E402
from vlt.engine import voice_segment_from_partial                # noqa: E402
from vlt.session.base import TextDelta                           # noqa: E402


class FakeVirtualMic:
    def __init__(self) -> None:
        self.pushed: list[bytes] = []
        self.sentences = 0

    def push(self, pcm: bytes) -> None:
        self.pushed.append(pcm)

    def end_sentence(self) -> None:
        self.sentences += 1

    def close(self) -> None:
        pass


def _mk(mode: str = "tts", *, output_audio: bool = True, audio_enabled: bool = True,
        direction: str = "mine", audio_extra: dict | None = None) -> Engine:
    audio = {"enabled": audio_enabled, "mode": mode}
    audio.update(audio_extra or {})
    cfg = AppConfig(
        session_base={"api_key": "sk-test", "model": "qwen3.8-livetranslate-flash-realtime",
                      "base_url": "wss://example.invalid"},
        directions={"mine": Direction(source_lang="zh", target_lang="en",
                                      output_audio=output_audio),
                    "theirs": Direction(source_lang="en", target_lang="zh",
                                        output_audio=False)},
        chatbox={"max_chars": 144}, merger={}, output={"audio": audio},
        text_input={"model": "qwen-mt-flash",
                    "tts": {"enabled": True, "stream": True,
                            "model": "qwen3-tts-flash", "voice": "Cherry"}},
    )
    return Engine(cfg=cfg, direction=direction, source="mic", sinks={"chatbox"},
                  events=EngineEvents(), dry_run=True)


def _feed(eng: Engine, texts: list[tuple[str, bool]]) -> list[str]:
    """按真实时间线的顺序喂 TextDelta；返回被排进 TTS 的文本序列（替身接住，不真合成）。"""
    spoken: list[str] = []
    eng._schedule_voice_speak = lambda t: spoken.append(t)       # type: ignore[assignment]
    eng._virtualmic = FakeVirtualMic()
    eng._loop = _FakeRunningLoop()
    for text, final in texts:
        eng._on_text(TextDelta(confirmed=text, is_final=final))
    return spoken


class _FakeRunningLoop:
    """只满足 `_loop.is_running()` 的判断，不真跑事件循环。"""

    def is_running(self) -> bool:
        return True


# 真链路探针里那句的真实分片序列（累计文本，只增不改）
REAL_PARTIALS = [
    "Hello.",
    "Hello. The",
    "Hello. The weather is",
    "Hello. The weather is nice today",
    "Hello. The weather is nice today.",
    "Hello. The weather is nice today. Let's go out",
    "Hello. The weather is nice today. Let's go out for",
]
REAL_FINAL = "Hello. The weather is nice today. Let's go out for a walk."


# ---------------------------------------------------------------- 1) 纯函数


def test_pure_segment() -> bool:
    ok = True
    cases = [
        ("", "", None, "空文本不念"),
        ("Hello", "", None, "没有分句符号 → 攒着"),
        ("Hey.", "", "Hey.", "有句号且够长（4 字）→ 就念它"),
        ("Hi.", "", None, "太短（3 字 < 4）先攒着"),
        ("今天天气不错，我们走", "", "今天天气不错，", "中文逗号也认（且够长）"),
        ("Hello. The weather.", "Hello.", " The weather.", "相对游标只切新增部分"),
        ("Hello. The weather", "Hello.", None, "新增部分还没到分句边界 → 攒着"),
        ("Hello, world. Next", "HelloX", None, "模型改写了已念过的部分 → 不念也不猜"),
    ]
    for text, spoken, want, note in cases:
        got = voice_segment_from_partial(text, spoken)
        cond = (got or "").strip() == (want or "").strip()
        print(f"  {note:<38} 输入={text[:28]!r:<30} → {got!r:<16} {'OK' if cond else '✗ want=' + repr(want)}")
        ok &= cond

    # 游标语义：返回的是**原文切片**（可带前导空格），按原样累加后必须仍是精确前缀
    spoken = ""
    seq = []
    for text in REAL_PARTIALS:
        seg = voice_segment_from_partial(text, spoken)
        if seg:
            spoken += seg                      # 原样推进
            seq.append(seg.strip())
    cond = REAL_FINAL.startswith(spoken)
    print(f"  游标按原文切片推进后仍是精确前缀 : {spoken!r}")
    print(f"  → {'OK' if cond else '✗ 游标失配，下一段再也切不出来（真踩过的坑）'}")
    ok &= cond
    return ok


# ---------------------------------------------------------------- 2) 引擎级驱动


def test_engine_speaks_partial_before_final() -> bool:
    ok = True
    eng = _mk("tts")
    spoken = _feed(eng, [(t, False) for t in REAL_PARTIALS] + [(REAL_FINAL, True)])

    got_first_early = bool(spoken) and spoken[0] == "Hello."
    print(f"  B 模式 + 分段开：TTS 收到 {spoken}")
    print(f"  终版之前就开口（首段 = 第一句）          {'OK' if got_first_early else '✗'}")
    ok &= got_first_early

    joined = " ".join(spoken)
    cond = "Hello." in spoken and "The weather is nice today." in spoken
    print(f"  句号处切了两段（不是整段一次念）        {'OK' if cond else '✗'}")
    ok &= cond

    # 不重念：把念过的拼起来（去掉空白）应该正好是终版的前缀，且末尾尾巴被念到
    norm = "".join(joined.split())
    cond = norm == "".join(REAL_FINAL.split()) or "".join(REAL_FINAL.split()).startswith(norm)
    print(f"  念过的内容无重复、是终版的前缀          {'OK' if cond else '✗'}")
    ok &= cond

    # 终版只补念剩下的尾巴
    cond = spoken[-1] == "Let's go out for a walk."
    print(f"  终版只念尾巴：{spoken[-1]!r}            {'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_off_and_guards() -> bool:
    ok = True
    # ① segment_tts: false → 退回旧行为（终版之前一声不吭）
    eng = _mk("tts", audio_extra={"segment_tts": False})
    spoken = _feed(eng, [(t, False) for t in REAL_PARTIALS] + [(REAL_FINAL, True)])
    cond = spoken == [REAL_FINAL]
    print(f"  segment_tts=false：TTS 序列={spoken}  {'OK' if cond else '✗'}")
    ok &= cond

    # ② A 模式（realtime）→ 分段不介入
    eng = _mk("realtime")
    spoken = _feed(eng, [(t, False) for t in REAL_PARTIALS] + [(REAL_FINAL, True)])
    cond = spoken == []
    print(f"  A 模式：TTS 序列={spoken}（应为空）     {'OK' if cond else '✗'}")
    ok &= cond

    # ③ 方向级 output_audio=false → 分段不介入
    eng = _mk("tts", output_audio=False)
    spoken = _feed(eng, [(t, False) for t in REAL_PARTIALS] + [(REAL_FINAL, True)])
    cond = spoken == []
    print(f"  方向级 output_audio=false：TTS 序列={spoken}（应为空）  {'OK' if cond else '✗'}")
    ok &= cond

    # ④ 总开关关 → 分段不介入
    eng = _mk("tts", audio_enabled=False)
    spoken = _feed(eng, [(t, False) for t in REAL_PARTIALS] + [(REAL_FINAL, True)])
    cond = spoken == []
    print(f"  译音输出总开关关：TTS 序列={spoken}（应为空）  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_min_chars_knob() -> bool:
    """segment_min_chars 生效：调大到 40 → 连「The weather is nice today.」（25 字）也不切，
    一直攒到终版才一次念完。"""
    ok = True
    eng = _mk("tts", audio_extra={"segment_min_chars": 40})
    spoken = _feed(eng, [(t, False) for t in REAL_PARTIALS] + [(REAL_FINAL, True)])
    cond = spoken == [REAL_FINAL]
    print(f"  min_chars=40：TTS 序列={spoken}         {'OK' if cond else '✗'}")
    ok &= cond
    return ok


if __name__ == "__main__":
    print("test_voice_segment:")
    print(" 1) 纯函数切段")
    ok = test_pure_segment()
    print(" 2) 引擎：终版前就开口 + 不重念 + 终版只补尾巴")
    ok &= test_engine_speaks_partial_before_final()
    print(" 3) 开关与四道守卫")
    ok &= test_off_and_guards()
    print(" 4) segment_min_chars 可调")
    ok &= test_min_chars_knob()
    assert ok, "分段合成用例失败（见上）"
    print("ALL PASSED")
