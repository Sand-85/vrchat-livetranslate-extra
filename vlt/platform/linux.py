"""Linux 平台实现（PipeWire 原生工具 + 子进程管道）。

⚠️ 本模块是 **Linux 独占**：打包 Windows 版时通过 `--exclude-module vlt.platform.linux`
剔除，所以 Windows 产物里不会出现 pipewire / pw-dump / pw-record 等任何字样。
（这就是「内嵌为常量 + 模块排除」方案的结构性保证，见 vlt/platform/base.py。）

## 为什么全程用 `pw-*` 而不是 `pactl` / `parec` / `pacat`

PipeWire 提供了 PulseAudio 兼容层（`pipewire-pulse`），`pactl`/`parec`/`pacat` 也能用。
**但实测在本机上 `pactl` 直接 `Connection refused`**（Pulse 套接字不可达），
而 `pw-dump` 正常返回 —— 兼容层不是必装件，原生工具才是。
设备枚举、采集、播放因此全部走 PipeWire 原生的 `pw-dump` / `pw-record` / `pw-cat`。

## 为什么设备表不直接用 sounddevice

`sounddevice.query_devices()` 在 Linux 上走 JACK 后端（pipewire-jack），它会把
**每个正在播放的应用流**也暴露成「输入设备」——实测列表里出现
`Google Chrome`、`vesktop`、`speech-dispatcher-dummy` 这些。直接拿来当「麦克风下拉框」
会是一堆噪音。

所以：**设备表从 `pw-dump` 构建**（只取 `Audio/Source` 与 `Audio/Sink`），
而**麦克风打开走 sounddevice 的按名打开**（`device="<node.description>"`，
sounddevice 接受字符串设备名）。这样既干净，又不引入新的采集依赖。

## 与 Windows 的形状对齐

`query_loopback_devices()` 返回与 pyaudiowpatch 同形状的 dict
（`index` / `name` / `defaultSampleRate` / `maxInputChannels`），
所以 `vlt/devices.py` 的纯逻辑（名称解析、回退链）一行都不用改。

注意 `index` 用的是 PipeWire 的**节点 id**（是个真 int），但它**只用于一次枚举之内**
的定位 —— 配置里永远只存 `name` 字符串，因为节点 id 每次重启都会变。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .audio import QueueAudioSource, SoundDeviceMicSource
from .base import AudioSource, LoopbackTarget

log = logging.getLogger(__name__)

# `pw-dump` 一次约 0.4MB / 100ms，而 GUI 一次设备扫描会连着问好几遍
# （麦克风一遍、输出一遍、loopback 一遍）→ 加个极短的 TTL 缓存，避免白跑。
_DUMP_TTL_S = 2.0
_dump_cache: tuple[float, list[dict]] | None = None


class PipeWireUnavailable(RuntimeError):
    """连不上 PipeWire（没装 / 没跑 / 沙盒里看不到套接字）。"""


# ---------------------------------------------------------------- pw-dump

def _pw_dump(force: bool = False) -> list[dict]:
    """调 `pw-dump` 拿全量对象快照（带 2 秒 TTL 缓存）。

    这是本模块唯一的数据来源：设备表、默认设备、采样率全部从这一份快照里读，
    避免多次调用之间状态漂移（用户正在插拔设备时尤其明显）。
    """
    global _dump_cache
    now = time.monotonic()
    if not force and _dump_cache is not None and now - _dump_cache[0] < _DUMP_TTL_S:
        return _dump_cache[1]

    if shutil.which("pw-dump") is None:
        raise PipeWireUnavailable("找不到 pw-dump（装 pipewire 包）")
    try:
        res = subprocess.run(["pw-dump"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PipeWireUnavailable(f"执行 pw-dump 失败：{exc}") from exc
    if res.returncode != 0:
        raise PipeWireUnavailable(
            f"pw-dump 退出码 {res.returncode}：{res.stderr.decode('utf-8', 'replace').strip()}")
    try:
        data = json.loads(res.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise PipeWireUnavailable(f"pw-dump 输出不是合法 JSON：{exc}") from exc
    if not isinstance(data, list):
        raise PipeWireUnavailable("pw-dump 输出不是数组")

    _dump_cache = (now, data)
    return data


def clear_cache() -> None:
    """丢掉 pw-dump 缓存 —— 设备扫描前想拿最新状态时调。"""
    global _dump_cache
    _dump_cache = None


def _nodes(dump: list[dict]) -> list[tuple[int, str, dict]]:
    """(节点 id, media.class, props) 三元组，只取音频节点。"""
    out = []
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        media_class = str(props.get("media.class") or "")
        if not media_class.startswith("Audio/"):
            continue
        out.append((int(obj.get("id") or 0), media_class, props))
    return out


def _default_rate(dump: list[dict]) -> int:
    """PipeWire 的 `default.clock.rate`（取不到就 48000，PipeWire 的常规默认）。"""
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Core":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        try:
            return int(props.get("default.clock.rate") or 48000)
        except (TypeError, ValueError):
            return 48000
    return 48000


def _display_name(props: dict) -> str:
    """人类可读的设备名：description → nick → node.name 依次回落。

    `node.name`（如 `alsa_output.usb-Mi_REDMI____...analog-stereo`）是稳定标识，
    但塞进下拉框太丑；description 才是给人看的。
    """
    for key in ("node.description", "node.nick", "node.name"):
        val = str(props.get(key) or "").strip()
        if val:
            return val
    return ""


def _channels(props: dict) -> int:
    try:
        return max(1, int(props.get("audio.channels") or 2))
    except (TypeError, ValueError):
        return 2


# ---------------------------------------------------------------- 设备枚举

def query_devices() -> list[dict]:
    """设备表，形状与 sounddevice 一致（`max_input_channels` / `max_output_channels`）。

    * `Audio/Source` → `max_input_channels > 0`（麦克风）
    * `Audio/Sink`   → `max_output_channels > 0`（播放设备 / 译音目标）

    顺序按 `node.name` 排序，保证同一台机器上多次枚举结果稳定
    （`devices.py` 的 index 是列表位置，不能每次变）。
    """
    dump = _pw_dump()
    rate = _default_rate(dump)
    rows: list[dict] = []
    for node_id, media_class, props in _nodes(dump):
        name = _display_name(props)
        if not name:
            continue
        node_name = str(props.get("node.name") or "")
        ch = _channels(props)
        if media_class == "Audio/Source":
            rows.append({"name": name, "node_name": node_name,
                         "max_input_channels": ch, "max_output_channels": 0,
                         "default_samplerate": float(rate), "pw_id": node_id})
        elif media_class == "Audio/Sink":
            rows.append({"name": name, "node_name": node_name,
                         "max_input_channels": 0, "max_output_channels": ch,
                         "default_samplerate": float(rate), "pw_id": node_id})
    rows.sort(key=lambda d: d.get("node_name") or d.get("name") or "")
    return rows


def query_loopback_devices() -> list[dict]:
    """可抓取的「系统输出」，形状与 pyaudiowpatch 的 loopback 设备一致。

    Windows 上每个输出设备都有一个对应的 loopback「录音副本」；
    Linux 上对应概念就是**输出节点本身** —— 对 sink 录音拿到的正是它的 monitor
    （`pw-record --target <node.name>`）。

    `name` 用 description（给人看、给用户存进 config），
    `node_name` 是 `--target` 真正要用的稳定标识。
    """
    dump = _pw_dump()
    rate = _default_rate(dump)
    rows = []
    for node_id, media_class, props in _nodes(dump):
        if media_class != "Audio/Sink":
            continue
        name = _display_name(props)
        if not name:
            continue
        rows.append({
            "index": node_id,                                   # 仅本次枚举内有效
            "name": name,
            "node_name": str(props.get("node.name") or ""),
            "defaultSampleRate": int(rate),
            "maxInputChannels": _channels(props),
        })
    rows.sort(key=lambda d: d.get("node_name") or d.get("name") or "")
    return rows


# VRChat（Proton/Wine）的**播放输出**在 PipeWire 里是 `Stream/Output/Audio` 节点，
# 而**不是** `Audio/Sink` —— 所以 `query_loopback_devices()`（只列 sink）永远看不到它，
# 「VRChat 音频」下拉里自然也就选不到。实测节点名取自可执行文件：
# `application.name` / `node.name` = `"VRChat.exe"`（同一进程会开 2 个输出流，
# 都连到默认 sink；定向采集任一个都是 VRChat 的声音）。
#
# 为什么抓流而不抓默认 sink：默认 sink 上是**所有**应用混在一起的声音
# （实测同一条链路上还有 Google Chrome / vesktop / 系统提示音），
# 抓它会把音乐、浏览器声音一起当成「游戏内语音」喂给翻译模型。
_VRCHAT_HINTS = ("vrchat",)

# 最多抓几路 VRChat 输出流。实测正常是 2 路；给个上限纯属防御（万一 Wine 抽风开一堆）。
VRCHAT_MAX_STREAMS = 8


def find_vrchat_output_streams() -> list[dict]:
    """找 VRChat 的**全部音频输出流**节点（Linux 专用，`Stream/Output/Audio`）。

    ⚠️ VRChat（Wine/Proton 经 pipewire-pulse）会开**多个**播放流：实测同一次运行里
    有 `media.name` = `audio stream #1` 与 `audio stream #5` 两个节点，各自可能承载
    一部分声音（也可能有一个处于 `pulse.corked` 暂停态）。所以这里返回**全部**，
    由调用方每一路开一条 `pw-record` 再混音 —— 只抓第一个会丢声音。

    返回与 `query_loopback_devices()` 同形状的 dict，外加：
      * `media_class`：固定 `Stream/Output/Audio`；
      * `serial`：`object.serial` —— **这才是 `pw-record --target=` 要用的标识**。
        全部节点的 `node.name` 都是 `VRChat.exe`，按名字无法区分多个流；
        而 `--target` 只接受「**序列号或名称**」，传数字 node id 会被当成名字匹配落空
        （实测：`--target=147` 没连到 147，而是回落到默认目标）。用 serial 精确。

    刻意用 `_pw_dump(force=True)`：调用方是「等 VRChat 出现」的轮询，
    2 秒 TTL 缓存会把「刚刚启动」的流节点挡在门外。

    找不到 VRChat（没跑 / 还没出声）返回空列表。
    """
    try:
        dump = _pw_dump(force=True)
    except PipeWireUnavailable:
        return []
    rate = _default_rate(dump)
    found: list[tuple[int, dict]] = []
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        # ⚠️ 这里**不能**走 `_nodes()`：它按 `media.class` 前缀 `Audio/` 过滤，
        # 而应用流是 `Stream/Output/Audio`，前缀是 `Stream/`。
        if str(props.get("media.class") or "") != "Stream/Output/Audio":
            continue
        hay = (str(props.get("application.name") or "") + " "
               + str(props.get("node.name") or "")).lower()
        if not any(hint in hay for hint in _VRCHAT_HINTS):
            continue
        node_name = str(props.get("node.name") or "")
        serial = props.get("object.serial")
        if not node_name or serial is None:
            continue
        stream_name = str(props.get("media.name") or "")
        label = str(props.get("application.name") or node_name)
        if stream_name:
            label = f"{label} ({stream_name})"       # 多个流时日志里才好区分
        found.append((int(obj.get("id") or 0), {
            "index": int(obj.get("id") or 0),
            "name": label,
            "node_name": node_name,
            "serial": str(serial),
            "defaultSampleRate": int(rate),
            "maxInputChannels": _channels(props),
            "media_class": "Stream/Output/Audio",
        }))
    found.sort(key=lambda t: t[0])                    # 按 node id 稳定排序（同一次快照内确定）
    if len(found) > VRCHAT_MAX_STREAMS:
        log.warning("[vrchat] 匹配到 %d 路 VRChat 输出流，只取前 %d 路",
                    len(found), VRCHAT_MAX_STREAMS)
        found = found[:VRCHAT_MAX_STREAMS]
    return [d for _id, d in found]


def default_output_index() -> int:
    """WirePlumber 的默认输出（sink）节点 id；取不到返回 -1。

    从 Metadata 对象的 `default.audio.sink` 里读 —— 这是 WirePlumber 存放
    「当前默认设备」的地方（`pactl get-default-sink` 读的也是同一份）。
    """
    try:
        dump = _pw_dump()
    except PipeWireUnavailable:
        return -1
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Metadata":
            continue
        for entry in (obj.get("metadata") or []):
            if entry.get("key") != "default.audio.sink":
                continue
            val = entry.get("value")
            if isinstance(val, dict):
                target = str(val.get("name") or "")
                for node_id, media_class, props in _nodes(dump):
                    if media_class == "Audio/Sink" and str(props.get("node.name")) == target:
                        return node_id
    return -1


def device_info_by_index(index: int) -> dict:
    """按节点 id 取 props（拿名字用）；取不到返回 {}。"""
    try:
        dump = _pw_dump()
    except PipeWireUnavailable:
        return {}
    for node_id, _media_class, props in _nodes(dump):
        if node_id == int(index):
            return dict(props)
    return {}


# ---------------------------------------------------------------- 字体

# fc-match 找不到时的兜底路径（Arch 上 noto-cjk / adobe-source-han-sans 都常见）。
_CJK_FONT_FALLBACKS = (
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Light.ttc",
    "/usr/share/fonts/adobe-source-han-sans/SourceHanSansCN-Regular.otf",
    "/usr/share/fonts/TTF/NotoSansCJK-Regular.ttc",
)


def find_cjk_font() -> str | None:
    """用 fontconfig 找一个中日韩字体；找不到返回 None。

    先问 `fc-match`（尊重用户自己的字体偏好），再退回已知路径。
    Windows 侧对应的是 `C:/Windows/Fonts/msyh.ttc`（见 win.py）。
    """
    if shutil.which("fc-match"):
        try:
            res = subprocess.run(
                ["fc-match", "-f", "%{file}", "Noto Sans CJK SC:lang=zh-cn"],
                capture_output=True, timeout=5,
            )
            if res.returncode == 0:
                cand = res.stdout.decode("utf-8", "replace").strip()
                if cand and Path(cand).exists():
                    return cand
        except (OSError, subprocess.TimeoutExpired):
            pass
    for cand in _CJK_FONT_FALLBACKS:
        if Path(cand).exists():
            return cand
    return None


# ---------------------------------------------------------------- 界面语言

# 语言代码前缀 → 本项目支持的界面语言。与 win.py 的口径必须一致：
# **不支持的语言一律回落 "en"**（外国用户按英文接待远比按中文合理）。
_LANG_MAP = {"zh": "zh", "en": "en", "ja": "ja", "ko": "ko", "ru": "ru"}


def detect_ui_language() -> str:
    """按 POSIX 惯例读界面语言：`LC_ALL` → `LC_MESSAGES` → `LANG`。

    形如 `zh_CN.UTF-8` / `ja_JP.utf8` / `en_US` / `C` / `POSIX`。
    取不到、是 `C`/`POSIX`、或不在支持列表里 → `"en"`。
    """
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        raw = (os.environ.get(var) or "").strip()
        if not raw:
            continue
        code = raw.split(".", 1)[0].split("@", 1)[0].strip().lower()
        primary = code.replace("_", "-").split("-", 1)[0]
        if primary in _LANG_MAP:
            return _LANG_MAP[primary]
        return "en"                       # C / POSIX / 德语 / 法语… → 英文
    return "en"


# ---------------------------------------------------------------- 虚拟声卡（运行时声明）

# 默认**输出**的候选类，抄自 WirePlumber 源码
# `/usr/share/wireplumber/scripts/default-nodes/rescan.lua:63-65`：
#     pushSelectDefaultNodeEvent (..., "audio.sink", "in", { "Audio/Sink", "Audio/Duplex" })
# 这个列表是**精确匹配**（约束动词 "c" = 「属于其中之一」），不是前缀匹配 ——
# 所以只要我们的类不在这两项里，就永远不会被选成默认输出。
DEFAULT_OUTPUT_CLASSES = ("Audio/Sink", "Audio/Duplex")

# ⚠️⚠️ capture 侧必须用 `Audio/Sink/Internal`，**绝不能用 `Audio/Sink`**。
#
# 这是整个虚拟声卡设计里最要命的一条，理由是实测出来的：
#
#   一开始 capture 侧写的是 `Audio/Sink`。结果**一创建虚拟声卡，用户的系统声音就没了** ——
#   WirePlumber 把我们的节点选成了默认输出，用户正在放的音乐/通话/游戏声音全被改道进了
#   虚拟设备。实测时间线：创建后约 1.8 秒，`default.audio.sink` 变成 `vlt_mic_sink`。
#
#   试过并且**确认无效**的「防护」（都读过 WP 源码或实测排除，别再走回头路）：
#     ✗ priority.session = 0   —— 实测挡不住（真实声卡是 696/1009/1109，我们 0，仍然被选中；
#                                 根因至今没查清，所以**不能依赖它**）
#     ✗ node.virtual = true    —— 默认选举根本不看这个属性（它只被 media-role 链接用）
#     ✗ node.hidden            —— 不存在这个属性
#     ✗ node.disabled          —— 只被 alsa / v4l2 / libcamera 这些硬件 monitor 读
#     ✗ node.link-group        —— 只有 `module-filter-chain` 注册过的 smart filter 才被豁免
#                                 （is_filter_smart 要求 link-group 已登记），pw-loopback 拿不到
#
#   `Audio/Sink/Internal` 是 WirePlumber 自己在用的合法类（bluez 设备集的内部成员节点，
#   见 monitors/bluez/create-set-node.lua），**不在默认输出的候选列表里** ——
#   于是「抢默认输出」这件事从机制上不可能发生，而不是靠事后还原去补救。
#
#   用户明确的口径：「虚拟麦就是正常的麦克风，可以参与默认选举」，
#   所以我们**不动 source 侧**（它保持 Audio/Source）——被选成默认麦克风是可接受的，
#   而且真实麦克风的 priority.session（2009/2100）本来就压过我们的 0。
VIRTUAL_SINK_PROPS: dict[str, object] = {
    "media.class": "Audio/Sink/Internal",
    "node.virtual": "true",
    "node.autoconnect": "false",
}
VIRTUAL_SOURCE_PROPS: dict[str, object] = {
    "media.class": "Audio/Source",
    "node.virtual": "true",
    "node.autoconnect": "false",
}


def loopback_argv(sink_name: str, source_name: str, description: str,
                  *, channels: int = 2) -> list[str]:
    """构造 `pw-loopback` 的 argv：一趟产出「可写入端 + 虚拟麦」。

    ⚠️ 方向容易设反（实测核实过）：
      * `-i/--capture-props` → 带「音频输入」的那端 → 我们往里写的那一端
      * `-o/--playback-props` → 带「音频输出」的那端 → **Audio/Source**（VRChat 选作麦克风）
      设反的后果：没有可写入的端，或没有 VRChat 能选的麦克风。

    写入侧用 `pw-cat --playback --target=<sink_name>` 显式指定目标 ——
    WirePlumber 的 `linking/find-defined-target` 按 `node.name` 匹配，且它跑在
    `find-default-target` **之前**，所以我们的音频只会进自己的节点，
    绝不会落到默认输出（= 用户的扬声器）上去。
    """
    def _props(base: dict, name: str) -> str:
        items = dict(base)
        items["node.name"] = name
        items["node.description"] = description
        inner = " ".join(f'{k}="{v}"' if isinstance(v, str) and " " in str(v) else f"{k}={v}"
                         for k, v in items.items())
        return "{ " + inner + " }"

    return [
        "pw-loopback",
        "-c", str(channels),
        "-m", "[ FL FR ]" if channels == 2 else "[ MONO ]",
        "--capture-props", _props(VIRTUAL_SINK_PROPS, sink_name),
        "--playback-props", _props(VIRTUAL_SOURCE_PROPS, source_name),
    ]


def node_props_by_name(name: str) -> dict:
    """按 `node.name` 取一个节点的 props（取不到返回 {}）。"""
    try:
        dump = _pw_dump()
    except PipeWireUnavailable:
        return {}
    for _node_id, _media_class, props in _nodes(dump):
        if str(props.get("node.name") or "") == name:
            return dict(props)
    return {}


def assert_not_default_output_candidate(node_name: str) -> tuple[bool, str]:
    """**只读**断言：这个节点的类不能落在默认输出的候选类里。

    这是「不打扰用户音频」的最后一道保险 —— 万一将来有人把 VIRTUAL_SINK_PROPS
    改回 `Audio/Sink`（或 WirePlumber 改了候选规则），这里会拦住，让我们禁用译音这条腿，
    而不是悄悄把用户的系统声音改道。

    ⚠️ 刻意**不做任何全局状态修改**（不 set-default、不写元数据）：那种「先抢再还原」
    的做法用户已经明确否决 —— 还原期间用户的音频是真的断的。
    """
    props = node_props_by_name(node_name)
    media_class = str(props.get("media.class") or "")
    if not media_class:
        return False, f"找不到节点 {node_name!r} 的 media.class"
    if media_class in DEFAULT_OUTPUT_CLASSES:
        return False, (f"节点 {node_name!r} 的类 {media_class!r} 属于默认输出候选"
                       f"{DEFAULT_OUTPUT_CLASSES} —— 会抢走用户的默认输出，已拒绝启用")
    return True, f"{node_name!r} 的类 {media_class!r} 不在默认输出候选里（安全）"


def _test_process_guard() -> str | None:
    """防呆：**测试进程里拒绝真的声明虚拟声卡**。

    写这个不是洁癖 —— 实测踩过四次：单元测试只桩住了 Windows 侧
    （`E.pick_output_device` / `E.VirtualMic`），于是 Linux 分支绕过打桩、
    真的拉起了 `pw-loopback`，动到了用户的音频图。
    正确修法是测试两侧都桩（见 `tests/test_virtualmic.py` 的 `_stub_audio_out`），
    这里只是**最后一道保险**：万一又漏了，宁可这条腿不启用，也不能动用户的音频。

    正常使用（`python -m vlt.gui` / `-m vlt.app`）永远不会命中这个判断。
    """
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    if not path:
        return None
    p = Path(path)
    if p.name.startswith("test_") or "tests" in p.parts:
        return f"检测到测试进程（{p.name}）→ 拒绝真的声明虚拟声卡"
    return None


class VirtualMicCable:
    """**运行时**声明一对 PipeWire 节点：可写入端 + 虚拟麦。

    生命周期 = 本对象的生命周期：`start()` 拉起 `pw-loopback`，`stop()` 发 SIGTERM，
    节点由 PipeWire 自动回收。全程不写任何配置文件、不重启任何服务、不改任何全局状态。
    """

    def __init__(self, sink_name: str = "vlt_mic_sink",
                 source_name: str = "vlt_mic_source",
                 description: str = "VLT Mic", channels: int = 2) -> None:
        self.sink_name = sink_name
        self.source_name = source_name
        self._description = description
        self._channels = channels
        self._proc: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, ready_timeout: float = 8.0) -> bool:
        """拉起并确认就绪。任何一步不对都返回 False（调用方据此禁用译音这条腿）。"""
        if self.running:
            return True
        blocked = _test_process_guard()
        if blocked:
            log.warning("[vmic] %s（这条腿不启用；测试必须两侧都打桩）", blocked)
            return False
        try:
            self._proc = subprocess.Popen(
                loopback_argv(self.sink_name, self.source_name, self._description,
                              channels=self._channels),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except Exception as exc:  # noqa: BLE001
            log.warning("[vmic] 拉起 pw-loopback 失败：%s: %s", type(exc).__name__, exc)
            return False

        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if not self.running:
                log.warning("[vmic] pw-loopback 已退出（stderr：%s）", self.stderr_tail()[:400])
                self._proc = None
                return False
            clear_cache()                       # 别被 TTL 缓存骗到（刚建的节点还看不到）
            if node_props_by_name(self.source_name) and node_props_by_name(self.sink_name):
                break
            time.sleep(0.1)
        else:
            log.warning("[vmic] 等待虚拟声卡节点超时（%.0fs）", ready_timeout)
            self.stop()
            return False

        # ★ 关键防线：确认可写入端的类**不参与默认输出选举**。
        # 这一步是只读的 —— 失败就整条腿不启用，绝不「先抢再还原」。
        ok, detail = assert_not_default_output_candidate(self.sink_name)
        if not ok:
            log.error("[vmic] ❌ 拒绝启用译音输出：%s", detail)
            self.stop()
            return False
        log.info("[vmic] 虚拟声卡就绪：%s（可写入）/ %s（虚拟麦）", self.sink_name, self.source_name)
        return True

    def stderr_tail(self) -> str:
        proc = self._proc
        if proc is None or proc.stderr is None or proc.poll() is None:
            return ""
        try:
            return proc.stderr.read().decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            return ""

    def stop(self) -> None:
        """幂等。SIGTERM → 节点由 PipeWire 回收。"""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("[vmic] pw-loopback 未响应 SIGTERM，改用 SIGKILL")
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        try:
            if proc.stderr is not None:
                proc.stderr.close()
        except Exception:  # noqa: BLE001
            pass


class LinuxAudioOut:
    """Linux 译音输出：虚拟声卡声明 + 写入端，绑成一个对象一起管生命周期。

    对外接口与 Windows 的 `VirtualMic` 一致（`open` / `push` / `end_sentence` / `close`），
    所以引擎侧不需要知道自己在哪个平台。
    """

    def __init__(self, cable: VirtualMicCable, sink) -> None:  # noqa: ANN001
        self._cable = cable
        self._sink = sink

    @property
    def device_name(self) -> str:
        return self._cable.sink_name

    @property
    def _sample_rate(self) -> int:                 # 兼容 VirtualMic 的属性名
        return self._sink._sample_rate

    def open(self) -> bool:
        return self._sink.open()

    def push(self, pcm_48k_stereo: bytes) -> None:
        self._sink.push(pcm_48k_stereo)

    def end_sentence(self) -> None:
        self._sink.end_sentence()

    def close(self) -> None:
        self._sink.close()
        self._cable.stop()


def open_audio_out(audio_cfg: dict, on_status) -> LinuxAudioOut | None:  # noqa: ANN001
    """声明虚拟声卡并接上写入端。任何一步失败都返回 None（其余功能不受影响）。

    生命周期：`close()` 会先关写入端、再销毁虚拟声卡 —— 顺序不能反，
    否则正在播的那句会被管道断开截断。
    """
    from ..output.virtualmic import PwCatVirtualMic

    cable = VirtualMicCable()
    if not cable.start():
        on_status("error", "虚拟声卡声明失败 → 译音输出已禁用（其余功能不受影响）")
        return None

    sink = PwCatVirtualMic(
        cable.sink_name,
        sample_rate=int(audio_cfg.get("sample_rate", 48000)),
        buffer_ms=int(audio_cfg.get("buffer_ms", 300)),
        max_buffer_ms=int(audio_cfg.get("max_buffer_ms", 2000)),
        on_status=on_status,
    )
    # 注意：这里**不打开** sink —— 交给调用方统一调 open()，
    # 与 Windows 侧「造出来 → 调 open()」的形状保持一致。
    return LinuxAudioOut(cable, sink)



# ---------------------------------------------------------------- 平台能力探测

def pw_tools_missing() -> list[str]:
    """缺哪些 PipeWire 原生工具（给 setup.sh / 启动自检报错用）。"""
    return [t for t in ("pw-dump", "pw-record", "pw-cat") if shutil.which(t) is None]


# ---------------------------------------------------------------- 采集

# ⚠️ **`--raw` 不能省**（实测踩到的静默坑）：
# 不给 `--raw` 时 pw-record 会用 libsndfile 写一个**容器格式**——实测 stdout 头 4 字节
# 是 ASCII "dns."（容器魔数），后面才是数据。把它当裸 PCM 解析出来的"音频"是垃圾，
# 而且不会报错（stderr 干净），只是峰值恒定、看起来"有信号"，极难发现。
# 播放侧同理：`pw-cat --playback` 不给 `--raw` 会报
# `sndfile: failed to open audio file "-": Format not recognised` 而**根本没播出去**。
_RAW = "--raw"


class PwRecordSource(QueueAudioSource):
    """系统声采集：`pw-record` 子进程读 stdout（PipeWire 原生，monitor 语义）。

    为什么不用 `parec`：它走的是 PulseAudio 兼容层，实测在本机上直接
    `Connection refused`（兼容层不是必装件）。`pw-record` 连的是 PipeWire 本体。

    `--target=<sink>` 抓的就是该 sink 的 **monitor**（输出内容的副本）——
    这正是 Windows 上 WASAPI loopback 的等价物。
    """

    label = "loopback"

    def __init__(self, loop, *, target: str, rate: int = 16000,
                 channels: int = 1, latency_ms: int = 50) -> None:
        super().__init__(loop, rate=rate, channels=channels)
        self._target = target
        self._latency_ms = latency_ms
        self._block = max(2, int(rate * 0.1)) * 2 * channels   # 100ms 一块
        self._proc: subprocess.Popen | None = None

    @property
    def argv(self) -> list[str]:
        return [
            "pw-record",
            f"--target={self._target}",
            "--format=s16",
            f"--rate={self.rate}",
            f"--channels={self.channels}",
            f"--latency={self._latency_ms}ms",
            _RAW,
            "-",
        ]

    def _pump(self, stop: threading.Event) -> None:
        self._proc = subprocess.Popen(
            self.argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,                      # 不做用户态缓冲：100ms 一块要实时到位
        )
        assert self._proc.stdout is not None
        try:
            while not stop.is_set():
                data = self._proc.stdout.read(self._block)
                if not data:
                    break                    # 子进程结束（设备被拔/daemon 重启）
                self._emit(data)
        finally:
            pass

    def _teardown(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                log.warning("[loopback] pw-record 未响应 SIGTERM，改用 SIGKILL")
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass

    def stderr_tail(self) -> str:
        """子进程的 stderr（出错排查用；进程已结束才读得到内容）。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return ""
        try:
            if proc.poll() is None:
                return ""
            return proc.stderr.read().decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            return ""


def _mic_native_rate(device_name: str | None, fallback: int = 48000) -> int:
    """设备的原生采样率 —— Linux 上**必须**用它打开 mic。

    ⚠️ PortAudio 的 ALSA 后端**不做采样率转换**：设备是 48000 时用 16000 打开会直接
    `PortAudioError: Invalid sample rate [PaErrorCode -9997]`（真机实测，2026-09）。
    Windows 的 WASAPI/MME 会自己重采样，所以那边一直用 16k 没事 —— 这是纯 Linux 的坑。

    查不到设备信息就按 PipeWire 的常规默认 48000（PipeWire 图内统一跑在 48k）。
    """
    import sounddevice as sd
    try:
        info = sd.query_devices(device_name) if device_name else sd.query_devices(kind="input")
        rate = int(float(info.get("default_samplerate") or 0))
    except Exception:  # noqa: BLE001 — 查不到不该让整条腿挂掉
        return fallback
    return rate or fallback


def open_mic(device_name: str | None, *, rate: int = 16000, channels: int = 1,
             blocksize: int) -> AudioSource:
    """麦克风采集（与 Windows 共用同一个 sounddevice 实现）。

    设备用**名字**打开（`sd.RawInputStream` 接受字符串设备名），所以不需要在
    「我们设备表的索引」和「sounddevice 的索引」之间做映射 —— Linux 上那张表来自
    pw-dump，索引跟 PortAudio 毫无关系。

    ⚠️ 打开用的**不是** `rate`(16000)，而是设备原生率（见 `_mic_native_rate`）：
        `source.rate` 即原生率，引擎的 `_pump_capture` 会拿它把每块 PCM 重采样到 16k
        （`to_16k_mono` 已支持 48000/44100 等非整数倍）。`rate` 只作兜底参考。
    """
    import asyncio

    native = _mic_native_rate(device_name)
    src = SoundDeviceMicSource(asyncio.get_running_loop(), device_name,
                               rate=native, channels=channels, blocksize=blocksize)
    src.start()
    return src


def open_loopback(target: LoopbackTarget, *, blocksize: int) -> AudioSource:
    """打开一路系统声采集。`target.id` 是 PipeWire 的 `node.name`。

    直接在 16kHz 单声道上采集：PipeWire 的图内重采样是系统级的（所有应用共用），
    质量有保证，也省掉 Python 侧一遍重采样。引擎里的 `to_16k_mono` 因此是恒等变换。
    """
    import asyncio

    src = PwRecordSource(asyncio.get_running_loop(), target=target.id,
                         rate=16000, channels=1)
    src.start()
    return src


# ---------------------------------------------------------------- 手腕屏后端

def create_wrist_overlay(cfg: Any, config_path: Any = None, dry_run: bool = False) -> Any:
    """手腕屏后端：Linux 用**自建 OpenXR overlay**（`vlt/output/openxr_overlay.py`）。

    与 Windows 侧结构对等：我们的进程直接作为 overlay session 连合成器，
    不依赖 WayVR 之类的第三方管理器、不注册、不写配置、不重启。
    两者公开接口一致，所以 engine/gui 不需要分平台。
    """
    from ..output.openxr_overlay import OpenXrOverlay
    return OpenXrOverlay(cfg, config_path=config_path, dry_run=dry_run)
