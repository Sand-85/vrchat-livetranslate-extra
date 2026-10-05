"""复核「麦克风静音」信号：真实的采集/上送链路里，「距上次上送音频的间隔」到底是不是
「用户说完话」的可靠判据？（**离线**，用真实代码喂 PCM，不需要任何硬件）

**背景（本仓库踩过的坑）**：曾有个改动拿「距上次 `send_audio` 的间隔 ≥ 0.5s」当
「麦克风已静」，以此提前封句。但麦克风腿**没有闸门**（`_SilenceGate` 默认
`silence_gate_after_s = 30s`），真人说话时静音块照样每 ~0.1s 上送一次 —— 这个间隔
在真链路里**恒为 ~0.1s**，判据几乎永不成立。作者在探针里看到「加速」是因为探针喂完
PCM 就**停止上送**，人造出了「麦克风静了」的假信号。

**作用**：用**真实代码**（`vlt.engine._SessionProxy` + `vlt.session.qwen38`）复现两个事实：
① 连续喂静音块，「距上次上送音频的间隔」最大 ~0.1s（**不能**当「用户停顿长度」）；
② 改用**电平**信号（`send_audio` 里 `peak >= SILENCE_PEAK` → `note_voice()`，
   `session.user_quiet_s()`）后，「上游已无人说话」判据才成立 —— 这正是现行快封句方案。
③ 顺带打印纯函数 `should_finalize()` 在两种判据下的输出。

**需要的环境**：无特殊硬件。任意平台、离线可跑（不联网、不碰音频设备）。

**跑法**（在仓库根目录）：
    ./.venv/Scripts/python.exe scripts/verify/check_mic_pump_silence.py

**会不会写盘**：不写盘。
"""
from __future__ import annotations

import asyncio
import math
import os
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]        # scripts/verify/ -> 仓库根
sys.path.insert(0, str(ROOT))

os.environ.pop("DASHSCOPE_API_KEY", None)

from vlt.engine import _SessionProxy                                  # noqa: E402
from vlt.session.base import SessionConfig, should_finalize           # noqa: E402
from vlt.session.qwen38 import QwenLiveTranslateSession               # noqa: E402

CHUNK_SAMPLES = 1600          # 100ms @16k 单声道
CHUNK_S = 0.1
SIL = b"\x00\x00" * CHUNK_SAMPLES
_TONE = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / 16000)))
                 for i in range(CHUNK_SAMPLES))


class _FakeCfg:
    # 与 config.example.yaml 一致（默认配置）
    session_base = {
        "silence_gate_enabled": True,
        "silence_gate_after_s": 30.0,
        "silence_gate_preroll_s": 1.0,
    }


class _FakeEngine:
    _cfg = _FakeCfg()
    _session = None
    _audio_in_chunks = 0
    _silent_chunks = 0
    _last_loud_ts = 0.0


class _FakeWS:
    """足够让真实的 `QwenLiveTranslateSession.send_audio()` 跑完（只需要一个 async send）。"""

    async def send(self, _data) -> None:
        return None


async def main() -> int:
    sess = QwenLiveTranslateSession(SessionConfig(api_key="x"))
    sess._ws = _FakeWS()                      # 假装已连接，让真实 send_audio 走到底
    eng = _FakeEngine()
    eng._session = sess
    proxy = _SessionProxy(eng)

    print("① 真实上送链路：用户**不出声**（一直喂静音块）时，「距上次上送音频的间隔」会长到多少？")
    print("   （旧判据想要它 ≥ 0.5s 才认为「麦克风静了」）")
    gaps: list[float] = []
    prev = time.perf_counter()
    for _ in range(40):                       # 4 秒静音
        await proxy.send_audio(SIL)
        now = time.perf_counter()
        gaps.append(now - prev)
        prev = now
        await asyncio.sleep(CHUNK_S)
    print(f"   4s 静音里：上送间隔最大 = {max(gaps)*1000:.0f}ms、平均 = "
          f"{sum(gaps)/len(gaps)*1000:.0f}ms")
    never = max(gaps) < 0.5
    print(f"   → 会触发「间隔 ≥ 0.5s」的旧判据吗？{'不会（间隔恒 ~100ms，判据永不成立）' if never else '会（与预期不符，请复查）'}")

    print()
    print("② 正确的信号：电平。同一个代理上，喂「说话(响) → 静音」：")
    print("   说话时 `user_quiet_s()` 应 ~0，停止说话后应随静音增长；")
    print("   若这条腿**从没说过话**，`user_quiet_s()` 是 None（走保守慢路径）。")
    sess2 = QwenLiveTranslateSession(SessionConfig(api_key="x"))
    sess2._ws = _FakeWS()
    eng2 = _FakeEngine()
    eng2._session = sess2
    proxy2 = _SessionProxy(eng2)
    print(f"   起始（还没说过话）：user_quiet_s() = {sess2.user_quiet_s()}")
    for _ in range(5):                        # 0.5s 说话
        await proxy2.send_audio(_TONE)
        await asyncio.sleep(CHUNK_S)
    q_while = sess2.user_quiet_s()
    print(f"   正在说话时：user_quiet_s() = {q_while:.3f}s（≈0，闸门'有人说话'在刷新）")
    for _ in range(5):                        # 0.5s 静音
        await proxy2.send_audio(SIL)
        await asyncio.sleep(CHUNK_S)
    q_after = sess2.user_quiet_s()
    print(f"   停止说话 0.5s 后：user_quiet_s() = {q_after:.3f}s（在增长 → 判据可用）")

    print()
    print("③ 纯判据复核（用仓库自己的 should_finalize，不为难它）：")
    rows = [
        ("旧判据（gap）：文字静默 1.5s、gap=0.1s（一直在上送静音块）",
         dict(text_quiet_s=1.5, user_quiet_s=0.1)),
        ("新判据（电平）：文字静默 1.5s、user_quiet=0.6s（上游已静 0.6s）",
         dict(text_quiet_s=1.5, user_quiet_s=0.6)),
        ("信号没接上：user_quiet=None → 保守走慢路径",
         dict(text_quiet_s=1.5, user_quiet_s=None)),
    ]
    for label, kw in rows:
        print(f"   {label} → should_finalize = {should_finalize(**kw)}")
    print()
    print("结论：真实链路上『距上次上送音频的间隔』恒 ~0.1s，**不能**当『用户说完』；"
          "必须用电平（note_voice / user_quiet_s）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
