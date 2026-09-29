#!/usr/bin/env python
"""虚拟声卡声明参数的**安全断言**。

## 这个测试守的是什么

用户实测过一次事故：虚拟声卡的 capture 侧用 `Audio/Sink` 时，
**一创建就抢走了系统默认输出** —— 用户正在放的音乐/通话/游戏声音全被改道，
表现是「突然没声音了」（实测约 1.8 秒后 `default.audio.sink` 变成我们的节点）。

根因：WirePlumber 选默认输出时的候选类**精确等于** `{"Audio/Sink", "Audio/Duplex"}`
（`default-nodes/rescan.lua:63-65`），而没有任何属性可以让节点「退出选举」
（`priority.session` / `node.virtual` / `node.hidden` / `node.disabled` / `node.link-group`
 全试过或读过源码，都无效）。

所以正确做法是把 capture 侧的类换成 `Audio/Sink/Internal` —— 它不在候选列表里，
从机制上不可能被选成默认输出。

这个测试就是防止将来有人「顺手」把它改回 `Audio/Sink`：改了这里立刻红。

## 能在两个平台跑

`vlt.platform.linux` 顶层只 import 标准库，所以 Windows 上也能 import 并检查参数 ——
而这条参数**必须**在两端都保持一致（它就是给 Linux 用的声明）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.platform.linux import (  # noqa: E402
    DEFAULT_OUTPUT_CLASSES,
    VIRTUAL_SINK_PROPS,
    VIRTUAL_SOURCE_PROPS,
    loopback_argv,
)


def _prop(props_str: str, key: str) -> str | None:
    """从 SPA-JSON 风格的 props 串里取出某个键的值（带/不带引号都认）。"""
    m = re.search(rf'(?:^|[{{ ]){re.escape(key)}=("[^"]*"|\S+)', props_str)
    if not m:
        return None
    return m.group(1).strip('"')


def _capture_props(argv: list[str]) -> str:
    return argv[argv.index("--capture-props") + 1]


def _playback_props(argv: list[str]) -> str:
    return argv[argv.index("--playback-props") + 1]


def test_capture_side_is_not_a_default_output_candidate() -> None:
    """★ 核心：capture 侧的类**不能**落在默认输出的候选类里。

    这是那次「用户声音突然没了」事故的直接防线。
    """
    argv = loopback_argv("vlt_mic_sink", "vlt_mic_source", "VLT Mic")
    cls = _prop(_capture_props(argv), "media.class")
    assert cls is not None, f"capture props 里没有 media.class：{argv}"
    assert cls not in DEFAULT_OUTPUT_CLASSES, (
        f"capture 侧的 media.class={cls!r} 属于默认输出候选 {DEFAULT_OUTPUT_CLASSES} —— "
        f"创建虚拟声卡会抢走用户的默认输出、把系统声音改道。必须用 Audio/Sink/Internal。"
    )
    assert cls == "Audio/Sink/Internal", f"capture 侧类变了：{cls!r}"
    print(f"  capture 侧类 = {cls}（不在默认输出候选 {DEFAULT_OUTPUT_CLASSES}）OK")


def test_playback_side_is_a_normal_microphone() -> None:
    """playback 侧是**正常麦克风**（Audio/Source）—— 用户口径「可以参与默认选举」。"""
    argv = loopback_argv("vlt_mic_sink", "vlt_mic_source", "VLT Mic")
    cls = _prop(_playback_props(argv), "media.class")
    assert cls == "Audio/Source", f"playback 侧类不对：{cls!r}"
    print("  playback 侧类 = Audio/Source（正常麦克风）OK")


def test_direction_is_not_swapped() -> None:
    """方向设反是实测踩过的坑：capture 侧是可写入端、playback 侧才是麦克风。"""
    argv = loopback_argv("vlt_mic_sink", "vlt_mic_source", "VLT Mic")
    cap, play = _capture_props(argv), _playback_props(argv)
    assert "vlt_mic_sink" in cap, "capture 侧没拿到 sink 名字（方向可能设反）"
    assert "vlt_mic_source" in play, "playback 侧没拿到 source 名字（方向可能设反）"
    print("  方向正确：capture=可写入端 / playback=麦克风 OK")


def test_node_names_are_explicit() -> None:
    """两端都必须显式命名：写入侧要靠 `--target=<名字>` 定位，名字不能是自动生成的。"""
    argv = loopback_argv("vlt_mic_sink", "vlt_mic_source", "VLT Mic")
    assert "node.name=vlt_mic_sink" in _capture_props(argv)
    assert "node.name=vlt_mic_source" in _playback_props(argv)
    print("  两端 node.name 都显式指定 OK")


def test_no_priority_hack() -> None:
    """不该再靠 `priority.session` 来「防止被选成默认」—— 实测无效，留着是误导。

    同时也守一条纪律：默认选举相关的参数**不要**在这里加，
    真正的防线是「不在候选类里」，不是调优先级。
    """
    argv = loopback_argv("vlt_mic_sink", "vlt_mic_source", "VLT Mic")
    for part in (_capture_props(argv), _playback_props(argv)):
        assert "priority.session" not in part, (
            f"又加回 priority.session 了：{part!r} —— 实测挡不住默认选举，"
            f"真正的防线是 capture 侧用 Audio/Sink/Internal（不在候选类里）"
        )
    print("  没有 priority.session 之类的假防护 OK")


def test_props_are_wellformed_spa_json() -> None:
    """props 串要能被 `pw-loopback` 解析：大括号成对、键值成对（含空格的值要带引号）。"""
    argv = loopback_argv("a", "b", "VLT Mic")
    for part in (_capture_props(argv), _playback_props(argv)):
        assert part.startswith("{") and part.endswith("}"), f"大括号不成对：{part!r}"
        assert "VLT Mic" in part, "描述里有空格，必须带引号"
    print("  props 串格式正确 OK")


def test_default_output_classes_match_wireplumber() -> None:
    """候选类表必须与 WirePlumber 源码一致（改了就是认知漂移，要重新核）。

    抄自 `default-nodes/rescan.lua`：
        pushSelectDefaultNodeEvent (..., "audio.sink", "in", { "Audio/Sink", "Audio/Duplex" })
    """
    assert set(DEFAULT_OUTPUT_CLASSES) == {"Audio/Sink", "Audio/Duplex"}, \
        f"候选类表变了：{DEFAULT_OUTPUT_CLASSES}"
    print("  默认输出候选类表与 WP 源码一致 OK")


if __name__ == "__main__":
    print("test_virtualmic_cable:")
    test_capture_side_is_not_a_default_output_candidate()
    test_playback_side_is_a_normal_microphone()
    test_direction_is_not_swapped()
    test_node_names_are_explicit()
    test_no_priority_hack()
    test_props_are_wellformed_spa_json()
    test_default_output_classes_match_wireplumber()
    print("ALL PASSED")
