#!/usr/bin/env python
"""麦克风采集的**声道数**回归：双声道 / 5.1 / 7.1 输入必须**全声道接进来**再降为单声道。

## 治什么（2026-10 真机）

调用点原来一律 `open_mic(..., channels=1)`。测量发现：Linux 上设备名解析到 PortAudio
的 **JACK** 后端时，开 1 声道只连 **1 个 JACK 端口**（`pw-link` 里只有
`|-> PortAudio:in_0`，来源是 `capture_FL`）—— 其余声道**整条丢掉**。Windows 侧同理，
麦克风没跟上 loopback 那条腿早就落地的「按端点原生声道数打开」口径。

## 这个测试怎么钉住它

假 `sounddevice` 记录**实际请求的声道数**，钉住三条不变式：

  ① 按设备原生声道数打开（6/8 声道都接），开成功后 `source.channels` 回填成实际声道数
     —— 下游 `to_16k_mono` / `resample_to_48k_stereo` 据此取均值降为单声道；
  ② 原生声道数被拒 → 按 `channel_fallbacks` 逐级回落到 2，且**失败行与降级行都留痕**；
  ③ 候选全失败 → 逐个都试过（不静默）。

另加：`win.open_mic` 的端点声道数、`to_16k_mono` 对 6/8 声道的降混。

（Linux 麦克风已改走原生 `pw-record`，不再有「按原生声道数开 sounddevice」这条 —— 多声道由
PipeWire 服务端降混，见 `tests/test_mic_rate.py`。）

⚠️ 日志走 **print**（`[mic]` 那族在引擎里就是 print 进日志文件的），按 stdout 捕获断言。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_SKIPPED: list[str] = []


def _skip(name: str, why: str) -> None:
    _SKIPPED.append(name)
    print(f"  ⏭ 跳过 {name}：{why}")


# ---------------------------------------------------------------- 假 sounddevice

def _install_fake_sd(plan: dict[int, Exception | None], *, max_in: int = 2):
    """假 sounddevice：`plan[声道数]` = 打开该声道数时抛的异常（None = 成功）。"""
    created = {"attempts": [], "closed": 0}

    class _Stream:
        def __init__(self, kw) -> None:      # noqa: ANN001
            self.kw = kw

        def __enter__(self):                 # noqa: ANN204
            return self

        def __exit__(self, *_a) -> bool:
            created["closed"] += 1
            return False

    def _raw_in(**kw):                       # noqa: ANN202
        created["attempts"].append(int(kw["channels"]))
        err = plan.get(int(kw["channels"]))
        if err is not None:
            raise err
        return _Stream(kw)

    mod = types.ModuleType("sounddevice")
    mod.RawInputStream = _raw_in                                     # type: ignore[attr-defined]
    mod.query_devices = lambda *a, **k: {                            # type: ignore[attr-defined]
        "max_input_channels": max_in, "default_samplerate": 48000}
    old = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = mod
    return created, old


def _restore_sd(old: object) -> None:
    if old is None:
        sys.modules.pop("sounddevice", None)
    else:
        sys.modules["sounddevice"] = old          # type: ignore[assignment]


def _make_mic(*, channels: int, channel_fallbacks: tuple[int, ...] = ()):  # noqa: ANN202
    from vlt.platform.win import SoundDeviceMicSource
    loop = asyncio.new_event_loop()
    src = SoundDeviceMicSource(loop, "Fake Mic", rate=48000, channels=channels,
                               blocksize=1600, channel_fallbacks=channel_fallbacks)
    src.start()
    return src, loop


def _wait(pred, timeout: float = 3.0) -> bool:    # noqa: ANN001
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------- 假模块用例

def test_mic_opens_with_native_channels() -> None:
    """6 声道设备 → 必须按 6 声道打开，并把 `source.channels` 回填成 6。"""
    created, old = _install_fake_sd({6: None})
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            src, loop = _make_mic(channels=6, channel_fallbacks=(2, 1))
            try:
                assert _wait(lambda: created["attempts"] == [6]), \
                    f"没有按原生声道数打开：{created['attempts']}"
                assert src.channels == 6, f"source.channels 应为 6（下游按它降混），实际 {src.channels}"
            finally:
                src.close()
                loop.close()
        out = buf.getvalue()
        assert "按 6 声道打开" in out, f"按原生声道数打开那一行没留痕：{out!r}"
        assert "回落" not in out, f"一次就开成功，不该有回落：{out!r}"
        print(f"  6ch 设备按原生声道数打开 OK（attempts={created['attempts']}）")
    finally:
        _restore_sd(old)


def test_mic_falls_back_when_native_rejected() -> None:
    """原生声道数被拒 → 逐级回落到 2ch；失败行与降级行都留痕（禁静默降级）。"""
    err = OSError(-9998, "Invalid number of channels")
    created, old = _install_fake_sd({6: err, 2: None})
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            src, loop = _make_mic(channels=6, channel_fallbacks=(2, 1))
            try:
                assert _wait(lambda: created["attempts"] == [6, 2]), \
                    f"回落顺序应为 6 → 2，实际 {created['attempts']}"
                assert src.channels == 2, f"回落成功后 source.channels 应为 2，实际 {src.channels}"
            finally:
                src.close()
                loop.close()
        out = buf.getvalue()
        assert "按 6 声道打不开" in out and "-9998" in out, f"原生失败那行没留痕：{out!r}"
        assert "已回落到 2ch" in out, f"回落那行没留痕：{out!r}"
        print(f"  原生被拒 → 回落 2ch OK（attempts={created['attempts']}，两条日志都在）")
    finally:
        _restore_sd(old)


def test_mic_all_channel_candidates_tried() -> None:
    """全部声道候选都打不开 → 6 → 2 → 1 逐个都试过（不静默）。"""
    err = OSError(-9998, "Invalid number of channels")
    created, old = _install_fake_sd({6: err, 2: err, 1: err})
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            src, loop = _make_mic(channels=6, channel_fallbacks=(2, 1))
            try:
                assert _wait(lambda: created["attempts"] == [6, 2, 1]), \
                    f"应把 6/2/1 都试过，实际 {created['attempts']}"
            finally:
                src.close()
                loop.close()
        print(f"  全失败 → 6/2/1 逐个都试过 OK（attempts={created['attempts']}）")
    finally:
        _restore_sd(old)


# ---------------------------------------------------------------- 声道数解析

def test_windows_endpoint_channels() -> None:
    """Windows：取端点 `max_input_channels`；取不到回落 2。"""
    created, old = _install_fake_sd({}, max_in=8)
    try:
        import vlt.platform.win as W
        assert W._endpoint_input_channels(None) == 8, "应取端点原生 8 声道"
        assert W._endpoint_input_channels(3) == 8, "给了索引也走同一取法"
    finally:
        _restore_sd(old)

    # 查询失败 → 回落 2（不让整条腿挂掉）
    mod = types.ModuleType("sounddevice")

    def _boom(*_a, **_k):
        raise RuntimeError("no device")

    mod.query_devices = _boom                        # type: ignore[attr-defined]
    old = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = mod
    try:
        import vlt.platform.win as W
        assert W._endpoint_input_channels(None) == 2, "查询失败应回落 2"
    finally:
        _restore_sd(old)
    print("  win 端点声道数：取原生 8 / 查询失败回落 2 OK")


def test_to_16k_mono_downmixes_multichannel() -> None:
    """`to_16k_mono`：6/8 声道按帧取均值降为单声道（16k 时不做重采样）。"""
    import numpy as np
    from vlt.engine import to_16k_mono

    mono6 = np.frombuffer(to_16k_mono(
        np.array([[100, 200, 300, 400, 500, 600],
                  [0, 0, 0, 0, 0, 600]], dtype=np.int16).tobytes(), 16000, 6),
        dtype=np.int16)
    assert list(mono6) == [350, 100], f"6 声道降混不对：{list(mono6)}"

    mono8 = np.frombuffer(to_16k_mono(
        np.array([[800] * 8], dtype=np.int16).tobytes(), 16000, 8), dtype=np.int16)
    assert list(mono8) == [800], f"8 声道降混不对：{list(mono8)}"
    print("  to_16k_mono：6/8 声道按帧取均值降为单声道 OK")


def test_mic_channel_fallbacks_helper() -> None:
    """`mic_channel_fallbacks`：只保留比 primary 小的 2/1（与 loopback 口径一致）。"""
    from vlt.platform.win import mic_channel_fallbacks
    assert mic_channel_fallbacks(6) == (2, 1)
    assert mic_channel_fallbacks(2) == (1,)
    assert mic_channel_fallbacks(1) == ()
    assert mic_channel_fallbacks(8) == (2, 1)
    print("  mic_channel_fallbacks：6→(2,1) / 2→(1,) / 1→() OK")


if __name__ == "__main__":
    print("test_mic_channels:")
    test_mic_opens_with_native_channels()
    test_mic_falls_back_when_native_rejected()
    test_mic_all_channel_candidates_tried()
    test_windows_endpoint_channels()
    test_to_16k_mono_downmixes_multichannel()
    test_mic_channel_fallbacks_helper()
    if _SKIPPED:
        print(f"  （跳过 {len(_SKIPPED)} 条：{'、'.join(_SKIPPED)}）")
    print("ALL PASSED")
