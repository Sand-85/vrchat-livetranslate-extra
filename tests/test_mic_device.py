#!/usr/bin/env python
"""回归：麦克风「设备句柄」的传递口径（两个平台的坑各一条）。

PR #4 审查指出两件事，两者都不会报错、只会静默选错设备：

1. `SoundDeviceMicSource` 曾用 `device or None` 归一化设备 —— PortAudio 的索引
   **0 是合法设备**，被 `or` 判成假值后会悄悄回落到默认设备。
2. Windows 侧曾把设备**名字**直接交给 sounddevice，而 v0.3.x 是先用
   `resolve_device_name` 解析成 **PortAudio 索引**再打开；同名端点（两块同型号声卡 /
   多个虚拟声卡）并存时，打开的物理端点可能与旧版不同。

本文件钉住：设备句柄原样传递（含 0），且 Windows 的 `open_mic` 交出去的是索引。

跑法：.venv/bin/python tests/test_mic_device.py
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_OPENED: dict = {}


class _FakeStream:
    def __init__(self, **kw) -> None:
        _OPENED.clear()
        _OPENED.update(kw)

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False


def _install_fake_sounddevice(native_rate: float = 48000.0) -> None:
    fake = types.ModuleType("sounddevice")
    fake.query_devices = lambda *a, **k: {          # type: ignore[attr-defined]
        "name": "Fake Mic", "default_samplerate": native_rate,
        "max_input_channels": 1, "max_output_channels": 0,
    }
    fake.RawInputStream = lambda **kw: _FakeStream(**kw)   # type: ignore[attr-defined]
    sys.modules["sounddevice"] = fake


def _open_and_close(make_source):
    """在事件循环里建源 → start() → 稍等 → close()（保证采集线程真跑过一次）。"""
    async def go():
        src = make_source()
        src.start()
        await asyncio.sleep(0.05)
        src.close()
        return src
    return asyncio.run(go())


def test_device_passed_through_unchanged() -> None:
    """`str | int | None` 必须原样传给 sd.RawInputStream（尤其索引 0 不能变 None）。"""
    import vlt.platform.audio as A

    _install_fake_sounddevice()
    for device in ("Fake Mic", 0, 3, None):
        _open_and_close(lambda d=device: A.SoundDeviceMicSource(
            asyncio.new_event_loop(), d, rate=48000, channels=1, blocksize=1600))
        assert _OPENED["device"] == device, (
            f"设备句柄被改写：{device!r} → {_OPENED['device']!r}")
    print("  SoundDeviceMicSource 原样传递 str / 0 / 3 / None OK")


def test_windows_open_mic_resolves_name_to_index() -> None:
    """Windows 的 open_mic 交给 sounddevice 的必须是**索引**（不是名字）。"""
    import vlt.devices as devices
    import vlt.platform.win as W

    _install_fake_sounddevice()
    seen: dict = {}
    orig = devices.resolve_device_name

    def fake_resolve(name, kind, devices_=None):   # noqa: ANN001
        seen.update(name=name, kind=kind)
        return 7

    devices.resolve_device_name = fake_resolve
    try:
        _open_and_close(lambda: W.open_mic("My USB Mic", rate=16000,
                                           channels=1, blocksize=1600))
    finally:
        devices.resolve_device_name = orig

    assert seen == {"name": "My USB Mic", "kind": "input"}, f"解析口径不对：{seen}"
    assert _OPENED["device"] == 7, (
        f"应把解析出的索引交给 sounddevice，实际 {_OPENED['device']!r}")
    assert _OPENED["samplerate"] == 16000, "Windows 仍按 16k 打开（WASAPI 会重采样）"
    print("  Windows open_mic：按名解析成索引后再打开 OK")


def test_windows_open_mic_unresolved_falls_back_to_default() -> None:
    """解析不到设备 → 交给 sounddevice 的是 None（= 系统默认），不硬塞名字。"""
    import vlt.devices as devices
    import vlt.platform.win as W

    _install_fake_sounddevice()
    orig = devices.resolve_device_name
    devices.resolve_device_name = lambda *a, **k: None
    try:
        _open_and_close(lambda: W.open_mic("不存在的设备", rate=16000,
                                           channels=1, blocksize=1600))
    finally:
        devices.resolve_device_name = orig

    assert _OPENED["device"] is None, (
        f"解析不到应回落默认设备（device=None），实际 {_OPENED['device']!r}")
    print("  Windows open_mic：设备名解析不到 → 回落默认输入设备 OK")


if __name__ == "__main__":
    print("test_mic_device:")
    test_device_passed_through_unchanged()
    test_windows_open_mic_resolves_name_to_index()
    test_windows_open_mic_unresolved_falls_back_to_default()
    print("ALL PASSED")
