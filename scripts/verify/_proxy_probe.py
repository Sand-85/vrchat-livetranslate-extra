"""麦克风代理真机回环验收的**共用件**（`scripts/verify/` 内部依赖，不是给人单独跑的入口）。

**作用**：两份平台各自的回环探针（`verify_proxy_loopback.py` = Windows、
`verify_proxy_loopback_linux.py` = Linux）要证明的是**同一件事** ——
「假麦克风的已知正弦真的从虚拟声卡的录音端出来了，且档位切换真的改了它」。
差别的只有**设备从哪来**（Windows 要用户自备 VB-CABLE/VoiceMeeter 并按 host API 挑端点；
Linux 的虚拟麦是程序运行时自己声明的）。所以「喂什么信号、怎么判主频」这一层必须共用，
否则两边判据会悄悄漂移 —— 而判据漂移正是这类探针最要命的失败模式（看着绿、其实没测到）。

**会不会写盘**：不会。这里只产生/分析内存里的 PCM。

为什么放在这里（而不是各脚本各抄一份）：与 `_shot_window.py` 同一个理由 ——
抄两份必然漂移。本文件被 `verify_proxy_loopback.py` 与
`verify_proxy_loopback_linux.py` 共用。
"""
from __future__ import annotations

import math
import struct

#: 假麦克风喂进去的正弦（原声档应当原样从录音端出来）
TONE_MIC_HZ = 1000.0
#: 译音档推进去的正弦（与上面不同频率，才能分辨「此刻放的是哪一档」）
TONE_TRANSLATED_HZ = 500.0
#: 主频判据容差。不做逐字节比对：中间过了环形缓冲 / 重采样 / 分块，
#: 但「1kHz 还是 500Hz」是确定性的。
TOLERANCE_HZ = 30.0


def tone_16k_mono(freq: float, ms: int, amp: float = 0.3) -> bytes:
    """16kHz 单声道 s16le 的已知正弦（假麦克风的输入块）。"""
    n = int(16000 * ms / 1000)
    return b"".join(
        struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * freq * i / 16000)))
        for i in range(n)
    )


def tone_48k_stereo(freq: float, ms: int, amp: float = 0.3) -> bytes:
    """48kHz 立体声 s16le 的已知正弦（译音档往 `translated_sink` 推的块）。"""
    n = int(48000 * ms / 1000)
    return b"".join(
        struct.pack("<hh", v, v)
        for v in (int(amp * 32767 * math.sin(2 * math.pi * freq * i / 48000)) for i in range(n))
    )


class FakeMicSource:
    """假麦克风源：按实时节奏吐 100ms 一块的已知正弦（16k 单声道）。

    接口与 `platform.capture_backend().open_mic()` 的返回值一致：
    `rate` / `channels` / `async read(timeout)` / `close()`。
    """

    def __init__(self, freq: float = TONE_MIC_HZ) -> None:
        self.rate, self.channels = 16000, 1
        self.closed = False
        self._chunk = tone_16k_mono(freq, 100)

    async def read(self, timeout: float = 0.5):        # noqa: ARG002
        import asyncio

        await asyncio.sleep(0.1)
        return self._chunk

    def close(self) -> None:
        self.closed = True


class FakeCaptureBackend:
    """替掉 `vlt.platform.capture_backend()` —— 只为了不碰真实麦克风。"""

    def __init__(self, src: FakeMicSource) -> None:
        self._src = src

    def open_mic(self, name, *, rate, channels, blocksize):   # noqa: ARG002, ANN001
        return self._src


def analyse(samples, label: str, samplerate: int = 48000) -> tuple[float, float]:
    """按 FFT 主频判「此刻录音端放的是哪一档」；返回 (主频 Hz, RMS dBFS) 并打印一行。"""
    import numpy as np

    mono = samples.astype(np.float64).mean(axis=1)
    rms = float(np.sqrt((mono ** 2).mean())) if len(mono) else 0.0
    db = 20 * math.log10(rms / 32768) if rms > 0 else -120.0
    spec = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
    freqs = np.fft.rfftfreq(len(mono), 1 / samplerate)
    peak = float(freqs[int(np.argmax(spec))]) if len(spec) else 0.0
    print(f"    {label}: 主频 {peak:7.1f} Hz | RMS {rms:8.1f} ({db:6.1f} dBFS)")
    return peak, db


def report(checks: list[tuple[str, float, float]]) -> bool:
    """打印判决表；返回是否全过。`checks` = [(说明, 实测 Hz, 期望 Hz)]。"""
    print(f"\n=== 判定（主频容差 ±{TOLERANCE_HZ:.0f}Hz）===")
    all_ok = True
    for label, got, want in checks:
        good = abs(got - want) <= TOLERANCE_HZ
        all_ok &= good
        print(f"  {'✅' if good else '❌'} {label}：实测 {got:.1f} Hz")
    return all_ok
