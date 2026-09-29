#!/usr/bin/env python
"""回归：Linux 麦克风必须按**设备原生采样率**打开。

真机实测（本机，PipeWire）：PortAudio 的 ALSA 后端**不做采样率转换** ——
用 16000 打开直接 `PortAudioError: Invalid sample rate [PaErrorCode -9997]`，
而设备原生率是 48000。于是「自己说话」这条腿在 Linux 上**完全打不开**
（Windows 的 WASAPI 共享模式会自己重采样，所以那边 16k 一直没事）。

修法：用设备原生率打开，`source.rate` 即原生率，引擎侧的 `to_16k_mono`
按它重采样到 16k（非整数倍分支已修好）。

跑法：.venv/bin/python tests/test_mic_rate.py
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeStream:
    def __init__(self, **kw) -> None:
        _OPENED.update(kw)

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False


_OPENED: dict = {}


def _install_fake_sounddevice(native_rate: float) -> None:
    fake = types.ModuleType("sounddevice")
    fake.query_devices = lambda *a, **k: {          # type: ignore[attr-defined]
        "name": "Fake Mic", "default_samplerate": native_rate, "max_input_channels": 1,
    }
    fake.RawInputStream = lambda **kw: _FakeStream(**kw)   # type: ignore[attr-defined]
    sys.modules["sounddevice"] = fake


def test_mic_uses_native_rate() -> None:
    import vlt.platform.linux as L

    _OPENED.clear()
    _install_fake_sounddevice(48000.0)

    async def go():
        src = L.open_mic("Fake Mic", rate=16000, channels=1, blocksize=1600)
        await asyncio.sleep(0.05)
        src.close()
        return src

    src = asyncio.run(go())
    rate = _OPENED.get("samplerate")
    assert rate == 48000, f"应按设备原生 48000 打开，实际 {rate}（16000 会 Invalid sample rate）"
    assert src.rate == 48000, f"source.rate 应为 48000 供引擎重采样，实际 {src.rate}"
    print("  Linux 麦克风按原生采样率打开 OK")


if __name__ == "__main__":
    print("test_mic_rate:")
    test_mic_uses_native_rate()
    print("ALL PASSED")
