#!/usr/bin/env python
"""「自动检测」与「手选麦克风」必须走**同一条通路**（回归钉死）。

## 治什么

以前两条路根本不同：

  * **Linux**：自动 → `device=None` → PortAudio 默认 = ALSA「default」；手选 → 描述 →
    PortAudio **JACK**。而且 PortAudio 把名字解析到 JACK 等于**隐式依赖可选的
    `pipewire-jack`**。**现在 Linux 两侧都走原生 `pw-record --target=<node.name>`**
    （只依赖 PipeWire 本体），自动 = 解析出 `default_source_node()` 再开。
  * **Windows**：自动 → `device=None` 且**不套用原生采样率**（16k，可能被 WASAPI 拒：
    `-9997`）；手选 → 具体索引 + 原生率 + 端点声道 + 同名回落。现在自动也解析成具体索引。

本文件用假后端记录实际参数，断言「自动」与「手选同一设备」产出的参数**一致**。

⚠️ 全程打桩（假 `LinuxMicSource` / 假 `sounddevice`），不碰真实音频设备。
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


# ---------------------------------------------------------------- Linux：假 LinuxMicSource

class _FakeMic:
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


def _open_linux(device_name, *, auto_node: str | None = None, table=None):   # noqa: ANN001, ANN202
    import vlt.platform.linux as L

    saved_cls, saved_default = L.LinuxMicSource, L.default_source_node
    L.LinuxMicSource = _FakeMic                                  # type: ignore[assignment]
    if auto_node is not None:
        L.default_source_node = lambda: auto_node                # type: ignore[assignment]
    table_saved = None
    if table is not None:
        import vlt.devices as D
        table_saved, D.enumerate_mic_devices = D.enumerate_mic_devices, table  # type: ignore[assignment]
    _FakeMic.instances.clear()
    loop = asyncio.new_event_loop()
    try:
        async def _go():                                          # noqa: ANN202
            return L.open_mic(device_name, rate=16000, channels=None, blocksize=1600)

        loop.run_until_complete(_go())
    finally:
        loop.close()
        L.LinuxMicSource, L.default_source_node = saved_cls, saved_default  # type: ignore[assignment]
        if table is not None:
            D.enumerate_mic_devices = table_saved                # type: ignore[assignment]
    assert _FakeMic.instances, "没构造 LinuxMicSource"
    return _FakeMic.instances[-1]


def test_linux_auto_uses_same_path_as_explicit() -> None:
    """Linux：自动检测 == 手选该源（同一条 `pw-record --target` 通路）。"""
    import vlt.platform as P
    if not P.IS_LINUX:
        return _skip("test_linux_auto_uses_same_path_as_explicit", "非 Linux")
    from vlt.devices import DeviceInfo

    node = "alsa_input.usb-Default-00.analog-stereo"

    def _table(devices=None):                                     # noqa: ANN001, ANN202
        return [DeviceInfo(index=0, name="Default Mic", sample_rate=48000, channels=2,
                           kind="input", node_name=node),
                DeviceInfo(index=1, name="Other Mic", sample_rate=44100, channels=1,
                           kind="input", node_name="alsa_input.other-00.mono-fallback")]

    with contextlib.redirect_stdout(io.StringIO()):
        auto = _open_linux(None, auto_node=node, table=_table)
        explicit = _open_linux("Default Mic", table=_table)
    assert auto.target == node, f"自动检测没解析成具体源的 node.name：{auto.target!r}"
    assert (auto.target, auto.rate, auto.channels) == \
        (explicit.target, explicit.rate, explicit.channels), (
            f"自动与手选的通路不一致：auto={auto.target!r} explicit={explicit.target!r}")
    print(f"  Linux：自动 == 手选（pw-record --target={auto.target}）OK")


def test_linux_auto_falls_back_when_no_default_source() -> None:
    """Linux：默认源解析不出来 → 空 target（不带 `--target`，用 PipeWire 默认输入）+ 留痕。"""
    import vlt.platform as P
    if not P.IS_LINUX:
        return _skip("test_linux_auto_falls_back_when_no_default_source", "非 Linux")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        auto = _open_linux(None, auto_node="")
    assert auto.target == "", f"解析失败应落空 target：{auto.target!r}"
    assert "查不到系统默认输入源" in buf.getvalue(), f"回落要留痕：{buf.getvalue()!r}"
    print("  Linux：默认源解析失败 → 空 target（并留痕）OK")


# ---------------------------------------------------------------- Windows：假 sounddevice

DEVS = [
    {"name": "Default Mic", "max_input_channels": 6, "max_output_channels": 0,
     "hostapi": 0, "default_samplerate": 48000.0},
    {"name": "Other Mic", "max_input_channels": 2, "max_output_channels": 0,
     "hostapi": 0, "default_samplerate": 44100.0},
]


def _install_sd(*, default_index: int = 0, wasapi_default_in: int = 0):
    created = {"opens": [], "closed": 0}

    class _Stream:
        def __init__(self, kw) -> None:          # noqa: ANN001
            self.kw = kw

        def __enter__(self):                     # noqa: ANN204
            return self

        def __exit__(self, *_a) -> bool:
            created["closed"] += 1
            return False

    def query_devices(device=None, kind=None, *_a, **_k):   # noqa: ANN001, ANN201
        if device is None and kind is None:
            return [dict(d) for d in DEVS]
        if isinstance(device, int):
            return dict(DEVS[device])
        if kind == "input":
            return dict(DEVS[default_index])
        for d in DEVS:
            if str(d["name"]) == str(device):
                return dict(d)
        raise ValueError(f"no device {device!r}")

    def _raw_in(**kw):                            # noqa: ANN202
        created["opens"].append(kw)
        return _Stream(kw)

    mod = types.ModuleType("sounddevice")
    mod.query_devices = query_devices             # type: ignore[attr-defined]
    mod.query_hostapis = lambda: [                # type: ignore[attr-defined]
        {"name": "Windows WASAPI", "default_input_device": wasapi_default_in,
         "default_output_device": 0}]
    mod.default = types.SimpleNamespace(device=[default_index, 0])  # type: ignore[attr-defined]
    mod.RawInputStream = _raw_in                  # type: ignore[attr-defined]
    old = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = mod
    return created, old


def _restore_sd(old: object) -> None:
    if old is None:
        sys.modules.pop("sounddevice", None)
    else:
        sys.modules["sounddevice"] = old          # type: ignore[assignment]


def _wait(pred, timeout: float = 3.0) -> bool:    # noqa: ANN001
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _open_via(open_fn):                           # noqa: ANN001, ANN202
    created, old = _install_sd()
    loop = asyncio.new_event_loop()
    try:
        async def _go():                          # noqa: ANN202
            return open_fn()

        src = loop.run_until_complete(_go())
        assert _wait(lambda: created["opens"]), "假 sounddevice 没记录到任何开流"
        kw = created["opens"][-1]
        src.close()
        return kw
    finally:
        loop.close()
        _restore_sd(old)


def test_windows_auto_uses_same_path_as_explicit() -> None:
    """Windows：自动检测解析成默认端点索引后，开流参数与手选该端点**完全一致**。"""
    import vlt.platform as P
    import vlt.platform.win as W

    class _Backend:                              # 让 resolve_device_name 用我们这张假表
        def query_devices(self):                 # noqa: ANN201
            return [{**d, "pa_index": i} for i, d in enumerate(DEVS)]

    orig_db = P.device_backend
    P.device_backend = lambda: _Backend()        # type: ignore[assignment]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            auto_kw = _open_via(lambda: W.open_mic(None, channels=None, blocksize=1600))
            explicit_kw = _open_via(
                lambda: W.open_mic("Default Mic", channels=None, blocksize=1600))
        assert auto_kw["device"] == 0, f"自动应解析成默认端点索引 0，实际 {auto_kw['device']!r}"
        assert auto_kw["channels"] == 6, f"自动应按端点 6 声道：{auto_kw['channels']}"
        assert auto_kw["samplerate"] == 48000, \
            f"自动应套用端点原生采样率 48000（而非 16000）：{auto_kw['samplerate']}"
        assert (auto_kw["device"], auto_kw["channels"], auto_kw["samplerate"]) == \
            (explicit_kw["device"], explicit_kw["channels"], explicit_kw["samplerate"]), (
                f"自动与手选的通路不一致：auto={auto_kw!r} explicit={explicit_kw!r}")
        print(f"  Windows：自动 == 手选（device={auto_kw['device']}, "
              f"{auto_kw['channels']}ch, {auto_kw['samplerate']}Hz）OK")
    finally:
        P.device_backend = orig_db               # type: ignore[assignment]


def test_windows_auto_falls_back_when_no_default() -> None:
    """Windows：拿不到默认输入索引 → 回落 PortAudio 默认（device=None）并留痕。"""
    import vlt.platform.win as W

    orig = W.default_input_index
    W.default_input_index = lambda: -1                       # type: ignore[assignment]
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            kw = _open_via(lambda: W.open_mic(None, channels=None, blocksize=1600))
        assert kw["device"] is None, f"拿不到默认索引应回落 device=None：{kw!r}"
        assert "拿不到系统默认输入设备" in buf.getvalue(), f"回落要留痕：{buf.getvalue()!r}"
        print("  Windows：拿不到默认输入设备 → device=None（并留痕）OK")
    finally:
        W.default_input_index = orig                         # type: ignore[assignment]


if __name__ == "__main__":
    print("test_mic_default_path:")
    test_linux_auto_uses_same_path_as_explicit()
    test_linux_auto_falls_back_when_no_default_source()
    test_windows_auto_uses_same_path_as_explicit()
    test_windows_auto_falls_back_when_no_default()
    if _SKIPPED:
        print(f"  （跳过 {len(_SKIPPED)} 条：{'、'.join(_SKIPPED)}）")
    print("ALL PASSED")
