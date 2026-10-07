#!/usr/bin/env python
"""回归：Linux 麦克风采集走**原生 `pw-record`**，不依赖 PortAudio / JACK。

## 为什么（2026-10）

以前 Linux 麦克风走 sounddevice/PortAudio，`sd.query_devices()` 把我们的设备名解析到
**JACK** host API 的条目 —— 而 `libjack` 由**可选包** `pipewire-jack` 提供。没装它的
PipeWire 环境里，按名字开麦直接抛错、整条腿挂掉（且「自动检测」同样中招）。

改成 `pw-record --target=<node.name>` 后**只依赖 PipeWire 本体**（`pw-record`，项目已声明/
已自检），且 `--channels=1` 由 PipeWire 服务端把源的全部声道降混进这一路。

## 本文件钉住

  · argv 精确靶向选中的源：`--target=<node.name>`、`--channels=1`、`--rate=16000`、
    `--format=s16`、`--raw`（裸 PCM，`--raw` 不能省）；
  · 目标为空时**不带** `--target`（用 PipeWire 默认输入）；
  · 描述 → `node.name` 的解析（`_mic_target_node`）；「自动检测」→ `default_source_node()`；
  · `open_mic` 最终把解析出的 target 交给 `LinuxMicSource`。

全程打桩（假 `LinuxMicSource`），**不真的起子进程**。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _FakeMic:
    """`LinuxMicSource` 替身：只记录构造参数，不 spawn `pw-record`。"""

    instances: list = []

    def __init__(self, loop, *, target: str = "", rate: int = 16000, channels: int = 1) -> None:
        self.target = target
        self.rate = rate
        self.channels = channels
        _FakeMic.instances.append(self)

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass


def _argv_of(target: str, *, rate: int = 16000, channels: int = 1) -> list[str]:
    from vlt.platform.linux import LinuxMicSource
    loop = asyncio.new_event_loop()
    try:
        return LinuxMicSource(loop, target=target, rate=rate, channels=channels).argv
    finally:
        loop.close()


def test_argv_targets_selected_source() -> None:
    """argv 精确靶向源：--target / --channels=1 / --rate=16000 / --format=s16 / --raw。"""
    argv = _argv_of("alsa_input.usb-Mic-00.mono-fallback")
    assert isinstance(argv, list) and argv[0] == "pw-record", argv
    assert "--target=alsa_input.usb-Mic-00.mono-fallback" in argv, f"没靶向选中的源：{argv}"
    assert "--channels=1" in argv, f"必须是单声道（PipeWire 服务端降混）：{argv}"
    assert "--rate=16000" in argv, f"必须直接 16k 采集：{argv}"
    assert "--format=s16" in argv and "--raw" in argv, f"裸 PCM 口径被破坏：{argv}"
    print(f"  pw-record argv 靶向/格式口径 OK：{argv}")


def test_argv_without_target_omits_target() -> None:
    """目标为空 → **不带** `--target`（用 PipeWire 默认输入），而不是写一个空的 --target=。"""
    argv = _argv_of("")
    assert not any(a.startswith("--target") for a in argv), f"空目标不该带 --target：{argv}"
    assert argv[0] == "pw-record", argv
    print("  空目标 → 不带 --target（用 PipeWire 默认输入）OK")


def test_mic_target_node_maps_description_to_node_name() -> None:
    """描述（设备表 name）→ PipeWire node.name；查不到返回 ""。"""
    import vlt.devices as D
    from vlt.devices import DeviceInfo
    from vlt.platform.linux import _mic_target_node

    orig = D.enumerate_mic_devices
    D.enumerate_mic_devices = lambda devices=None: [        # type: ignore[assignment]
        DeviceInfo(index=0, name="Fake Mic", sample_rate=48000, channels=2, kind="input",
                   node_name="alsa_input.fake-00.analog-stereo")]
    try:
        assert _mic_target_node("Fake Mic") == "alsa_input.fake-00.analog-stereo", "没映射出 node.name"
        assert _mic_target_node("Unknown") == "", "查不到应返回空串"
        assert _mic_target_node(None) == "", "空描述应返回空串"
        print("  描述 → node.name 映射（命中/未命中/空）OK")
    finally:
        D.enumerate_mic_devices = orig                       # type: ignore[assignment]


def _open_mic_capture(target_node, *, device_name=None, auto_node=None):   # noqa: ANN001, ANN202
    """在事件循环里调 `open_mic`，记录交给 `LinuxMicSource` 的 target。"""
    import vlt.platform.linux as L

    saved_cls = L.LinuxMicSource
    saved_default = L.default_source_node
    _FakeMic.instances.clear()
    L.LinuxMicSource = _FakeMic                              # type: ignore[assignment]
    if auto_node is not None:
        L.default_source_node = lambda: auto_node           # type: ignore[assignment]
    try:
        loop = asyncio.new_event_loop()
        try:
            async def _go():                                 # noqa: ANN202
                return L.open_mic(device_name, rate=16000, channels=None, blocksize=1600)

            loop.run_until_complete(_go())
        finally:
            loop.close()
        assert _FakeMic.instances, "没构造 LinuxMicSource"
        return _FakeMic.instances[-1]
    finally:
        L.LinuxMicSource = saved_cls                         # type: ignore[assignment]
        L.default_source_node = saved_default                # type: ignore[assignment]


def test_open_mic_auto_resolves_default_source_node() -> None:
    """自动检测：open_mic(None) 解析成 `default_source_node()` 的 node.name，走同一条通路。"""
    src = _open_mic_capture(None, device_name=None, auto_node="rnnoise_source")
    assert src.target == "rnnoise_source", f"自动检测没解析出 node.name：{src.target!r}"
    assert (src.rate, src.channels) == (16000, 1), (src.rate, src.channels)
    print("  open_mic(自动) → LinuxMicSource(target=default_source_node) OK")


def test_open_mic_explicit_resolves_device_to_target() -> None:
    """手选：描述 → node.name → LinuxMicSource(target=...)。"""
    import vlt.devices as D
    from vlt.devices import DeviceInfo

    orig = D.enumerate_mic_devices
    D.enumerate_mic_devices = lambda devices=None: [        # type: ignore[assignment]
        DeviceInfo(index=0, name="Fake Mic", sample_rate=48000, channels=2, kind="input",
                   node_name="alsa_input.fake-00.analog-stereo")]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            src = _open_mic_capture("alsa_input.fake-00.analog-stereo", device_name="Fake Mic")
        assert src.target == "alsa_input.fake-00.analog-stereo", f"手选没靶向 node.name：{src.target!r}"
        # 未命中 → 空 target（不带 --target）
        with contextlib.redirect_stdout(io.StringIO()):
            miss = _open_mic_capture("", device_name="不存在的设备")
        assert miss.target == "", f"未命中应落空 target：{miss.target!r}"
        print("  open_mic(手选) → node.name 靶向；未命中 → 空 target OK")
    finally:
        D.enumerate_mic_devices = orig                       # type: ignore[assignment]


if __name__ == "__main__":
    print("test_mic_rate:")
    test_argv_targets_selected_source()
    test_argv_without_target_omits_target()
    test_mic_target_node_maps_description_to_node_name()
    test_open_mic_auto_resolves_default_source_node()
    test_open_mic_explicit_resolves_device_to_target()
    print("ALL PASSED")
