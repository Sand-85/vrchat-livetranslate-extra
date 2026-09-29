"""音频设备枚举与名称解析。

三类设备，三种枚举来源：
  - 麦克风（输入）：sounddevice / PipeWire 的 `Audio/Source`
  - VRChat 音频（loopback）：Windows = WASAPI loopback；Linux = PipeWire 的 `Audio/Sink`
  - 译音输出（输出）：sounddevice / PipeWire 的 `Audio/Sink`

**真实设备从 `vlt/platform/` 取，本模块只负责「解释」**：
（⚠️ 必须写 `platform.device_backend()` 而不是 `from .platform import device_backend` ——
后者把函数对象**绑死**在导入那一刻，测试里打桩 `vlt.platform.device_backend` 就失效了，
`tests/test_device_pick.py` 就是这么发现问题的。）
  - 平台模块返回与 sounddevice / pyaudiowpatch 同形状的原始 dict
  - 本模块把 dict 转成 `DeviceInfo`、做名称解析、排回退链
  - 测试通过 `devices=` 参数注入假列表，于是**全部逻辑离线可测、不碰硬件**
    （见 `tests/test_devices.py`）——这条不许破坏：它是换平台时唯一的安全网。

名称解析顺序：全名精确匹配 → 不区分大小写 → 去重后的子串匹配 → 解析不到返回 None。
配置里只存纯设备名字符串，绝不存设备索引（索引会随插拔/重启变化）。

> PortAudio 的串行锁（`PA_LOCK`）搬到了 `vlt/platform/base.py` —— 它现在是
> 「两个平台的麦克风枚举共用同一把锁」的问题，不再属于本模块。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import platform


@dataclass
class DeviceInfo:
    index: int
    name: str
    sample_rate: int
    channels: int
    kind: str  # "input" | "output" | "loopback"


def enumerate_mic_devices(devices: list[dict] | None = None) -> list[DeviceInfo]:
    """枚举麦克风（输入）设备。devices 参数用于测试注入。"""
    try:
        devs = devices if devices is not None else platform.device_backend().query_devices()
        result = []
        for i, d in enumerate(devs):
            if d.get("max_input_channels", 0) > 0:
                result.append(DeviceInfo(
                    index=i,
                    name=str(d.get("name", "")),
                    sample_rate=int(d.get("default_samplerate", 0)),
                    channels=int(d.get("max_input_channels", 0)),
                    kind="input",
                ))
        return result
    except Exception:
        return []


def enumerate_loopback_devices(devices: list[dict] | None = None) -> list[DeviceInfo]:
    """枚举「VRChat 音频」——Windows 是 WASAPI loopback，Linux 是 PipeWire 的输出节点。

    devices 参数用于测试注入（形状同 pyaudiowpatch 的 loopback 设备）。
    """
    try:
        devs = devices if devices is not None else platform.device_backend().query_loopback_devices()
        result = []
        for d in devs:
            result.append(DeviceInfo(
                index=int(d.get("index", 0)),
                name=str(d.get("name", "")),
                sample_rate=int(d.get("defaultSampleRate", 0)),
                channels=int(d.get("maxInputChannels", 0)),
                kind="loopback",
            ))
        return result
    except Exception:
        return []


def enumerate_audio_out_devices(devices: list[dict] | None = None) -> list[DeviceInfo]:
    """枚举译音输出（输出）设备。devices 参数用于测试注入。"""
    try:
        devs = devices if devices is not None else platform.device_backend().query_devices()
        result = []
        for i, d in enumerate(devs):
            if d.get("max_output_channels", 0) > 0:
                result.append(DeviceInfo(
                    index=i,
                    name=str(d.get("name", "")),
                    sample_rate=int(d.get("default_samplerate", 0)),
                    channels=int(d.get("max_output_channels", 0)),
                    kind="output",
                ))
        return result
    except Exception:
        return []


def resolve_device_name(
    name: str,
    kind: str,
    devices: list[dict] | None = None,
) -> int | None:
    """按设备名解析出设备索引。

    kind: "input" | "output" | "loopback"
    返回 None 表示"用默认/回退链"。

    解析顺序：
      1. 全名精确匹配
      2. 不区分大小写匹配
      3. 去重后的子串匹配（正则特殊字符用字面量匹配，不会崩也不会误匹配）
      4. 解析不到 → 返回 None
    """
    if not name:
        return None

    if kind == "loopback":
        infos = enumerate_loopback_devices(devices)
    elif kind == "input":
        infos = enumerate_mic_devices(devices)
    elif kind == "output":
        infos = enumerate_audio_out_devices(devices)
    else:
        return None

    # 1. 全名精确匹配
    for info in infos:
        if info.name == name:
            return info.index

    # 2. 不区分大小写
    name_lower = name.lower()
    for info in infos:
        if info.name.lower() == name_lower:
            return info.index

    # 3. 子串匹配（用字面量，不用正则——设备名里的括号/加号/方括号不会崩）
    seen_names: set[str] = set()
    for info in infos:
        if info.name in seen_names:
            continue
        seen_names.add(info.name)
        if name_lower in info.name.lower():
            return info.index

    return None


def format_device_display(info: DeviceInfo) -> str:
    """格式化设备显示字符串，例如 'Steam Streaming Speakers (48000Hz)'。"""
    if info.sample_rate > 0:
        return f"{info.name} ({info.sample_rate}Hz)"
    return info.name
