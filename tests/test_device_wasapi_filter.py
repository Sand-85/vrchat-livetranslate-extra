#!/usr/bin/env python
"""设备枚举只保留 **Windows 已启用（WASAPI）** 那一套的回归测试。

## 真实问题（用户实测报上来的）

Windows 上 PortAudio 有 4 个 host API（MME / DirectSound / WASAPI / WDM-KS），
**同一块声卡在每个 API 下各算一条**，还夹着一堆已禁用 / 未插入的幽灵端点：

    本机实测 sd.query_devices() ＝ 41 条
    Windows「声音」里真正已启用的（Get-PnpDevice -Class AudioEndpoint, Status=OK）＝ 7 个
    PortAudio 的 WASAPI 那一套 ＝ 正好那 7 个（名字逐个对得上，采样率统一 48000）

不收敛的后果：
  ① 界面上同一支麦克风出现两三条（界面按「名字 (采样率Hz)」显示）；
  ② 同名那几条采样率还不一样（MME/DirectSound 报 44100、WASAPI 报 48000）；
  ③ 会列出根本没启用的设备；
  ④ 按名字解析总命中第一条，而 MME 排最前 → 用户挑了 WASAPI，实际打开的仍是 MME。

## 这个测试怎么钉住它

用**假 sounddevice**（4 个 host API + 重复 + 幽灵）直接测 `win.query_devices()`：
  ① 只留 WASAPI；② 每条带 `pa_index` = **真实** PortAudio 索引（过滤后会错位！）；
  ③ 收敛时留痕、WASAPI 取不到时**回落全表并留痕**（不静默）。
另加真机用例（Windows 才跑）：真枚举出来的每条都必须是 WASAPI 且 `pa_index` 齐全、无同名重复。
"""
from __future__ import annotations

import contextlib
import io
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_SKIPPED: list[str] = []


def _skip(name: str, why: str) -> None:
    _SKIPPED.append(name)
    print(f"  ⏭ 跳过 {name}：{why}")


@contextlib.contextmanager
def _capture_out():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


# ---------------------------------------------------------------- 假 sounddevice

_APIS = [
    {"name": "MME"},
    {"name": "Windows DirectSound"},
    {"name": "Windows WASAPI"},
    {"name": "Windows WDM-KS"},
]

# 顺序刻意排成真机那样：MME 在最前、WASAPI 夹在中间、WDM-KS 在最后，且带上幽灵端点
_DEVICES = [
    # 0  MME   —— 麦克风 A（重复项，44100）
    {"name": "Mic A", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0,
     "default_samplerate": 44100.0},
    # 1  MME   —— 未启用幽灵（空名括号）
    {"name": "耳机 ()", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0,
     "default_samplerate": 44100.0},
    # 2  WASAPI —— 麦克风 A（**真身**，48000）
    {"name": "Mic A", "hostapi": 2, "max_input_channels": 2, "max_output_channels": 0,
     "default_samplerate": 48000.0},
    # 3  WDM-KS —— 未启用幽灵（立体声混音）
    {"name": "立体声混音 (Realtek HD Audio Stereo input)", "hostapi": 3,
     "max_input_channels": 2, "max_output_channels": 0, "default_samplerate": 48000.0},
    # 4  DirectSound —— 扬声器 B（重复项，44100）
    {"name": "扬声器 B", "hostapi": 1, "max_input_channels": 0, "max_output_channels": 2,
     "default_samplerate": 44100.0},
    # 5  WASAPI —— 扬声器 B（**真身**，48000）
    {"name": "扬声器 B", "hostapi": 2, "max_input_channels": 0, "max_output_channels": 2,
     "default_samplerate": 48000.0},
]


def _install_fake_sd(apis=_APIS, devices=_DEVICES):
    class _FakeSD(types.ModuleType):
        def query_devices(self):                    # noqa: ANN201
            return [dict(d) for d in devices]

        def query_hostapis(self):                   # noqa: ANN201
            return [dict(a) for a in apis]

    old = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = _FakeSD("sounddevice")
    return old


def _restore(old) -> None:                          # noqa: ANN001
    if old is None:
        sys.modules.pop("sounddevice", None)
    else:
        sys.modules["sounddevice"] = old


def _query():
    from vlt.platform.win import query_devices
    return query_devices()


# ---------------------------------------------------------------- 假 sounddevice 用例

def test_keeps_only_wasapi() -> None:
    """① 只留 WASAPI：重复项与未启用幽灵都不该出现在设备表里。"""
    old = _install_fake_sd()
    try:
        with _capture_out() as buf:
            devs = _query()
        names = [d["name"] for d in devs]
        assert names == ["Mic A", "扬声器 B"], f"设备表应收敛到 WASAPI 那两条，实际 {names}"
        assert all(d["hostapi"] == 2 for d in devs), "只剩 WASAPI 的条目"
        assert all(d["default_samplerate"] == 48000.0 for d in devs), \
            "采样率应统一为 WASAPI 的 48000（不再出现 44100 的重复项）"
        out = buf.getvalue()
        assert "设备表收敛到 WASAPI" in out and "4 条" in out, \
            f"收敛要留痕（几条被隐藏、为什么）：{out!r}"
        print("  只留 WASAPI OK（6 条 → 2 条，重复项与幽灵都隐藏，且留痕）")
    finally:
        _restore(old)


def test_pa_index_is_true_index() -> None:
    """② `pa_index` 必须是**过滤前**的真实 PortAudio 索引 —— 否则会打开到错位的设备。

    这是这次改动最容易埋雷的地方：过滤会把列表下标整体前移，而下游是拿这个数
    去 `sd.RawInputStream(device=…)` / `RawOutputStream(device=…)` 的。
    """
    old = _install_fake_sd()
    try:
        with _capture_out():
            devs = _query()
        got = {d["name"]: d["pa_index"] for d in devs}
        assert got == {"Mic A": 2, "扬声器 B": 5}, \
            f"pa_index 必须是真实索引（Mic A=2、扬声器 B=5），实际 {got}"

        # 下游真正拿到的设备号（devices.py 的 DeviceInfo.index）也必须是真实索引
        from vlt.devices import enumerate_audio_out_devices, enumerate_mic_devices
        mics = enumerate_mic_devices(devs)
        outs = enumerate_audio_out_devices(devs)
        assert [m.index for m in mics] == [2], f"麦克风索引应为 2，实际 {[m.index for m in mics]}"
        assert [o.index for o in outs] == [5], f"输出索引应为 5，实际 {[o.index for o in outs]}"
        assert [m.sample_rate for m in mics] == [48000], "显示/使用的采样率应为 48000"

        # 老配置里存的是**名字** → 仍要解析到 WASAPI 那条
        from vlt.devices import resolve_device_name
        assert resolve_device_name("Mic A", "input", devs) == 2, "按名字应解析到 WASAPI 那条"
        print("  pa_index = 真实索引 OK（下游拿到的设备号不错位；老配置按名字仍解析得到）")
    finally:
        _restore(old)


def test_falls_back_loudly_when_no_wasapi() -> None:
    """③ 没有 WASAPI host API → **回落全表并留痕**（极端环境下一个设备都选不到更糟）。"""
    old = _install_fake_sd(apis=[{"name": "MME"}, {"name": "Windows DirectSound"}])
    try:
        with _capture_out() as buf:
            devs = _query()
        assert len(devs) == len(_DEVICES), "应回落完整设备表（4 条也不许少给）"
        assert "没有 WASAPI host API" in buf.getvalue(), f"回落必须留痕：{buf.getvalue()!r}"
        print("  没有 WASAPI → 回落全表 + 留痕 OK")
    finally:
        _restore(old)


def test_falls_back_loudly_when_wasapi_empty() -> None:
    """④ WASAPI 在、但一个设备都没枚举到 → 同样回落全表并留痕。"""
    # 把两条 WASAPI 设备挪到别的 hostapi 下，模拟「WASAPI 一个都没有」
    devs = [dict(d, hostapi=0 if d["hostapi"] == 2 else d["hostapi"]) for d in _DEVICES]
    old = _install_fake_sd(devices=devs)
    try:
        with _capture_out() as buf:
            out = _query()
        assert len(out) == len(devs), "应回落完整设备表"
        assert "回落完整设备表" in buf.getvalue(), f"回落必须留痕：{buf.getvalue()!r}"
        print("  WASAPI 空 → 回落全表 + 留痕 OK")
    finally:
        _restore(old)


# ---------------------------------------------------------------- 真机用例

def test_real_devices_are_wasapi_only() -> None:
    """⑤ 真机：枚举出来的每条都必须是 WASAPI，`pa_index` 齐全，且没有同名重复。"""
    if sys.platform != "win32":
        return _skip("test_real_devices_are_wasapi_only", "非 Windows：没有 WASAPI host API")
    import sounddevice as sd
    raw = [dict(d) for d in sd.query_devices()]
    apis = [dict(a) for a in sd.query_hostapis()]
    wasapi = [i for i, a in enumerate(apis) if "WASAPI" in str(a.get("name", "")).upper()]
    if not wasapi:
        return _skip("test_real_devices_are_wasapi_only", "本机没有 WASAPI host API")
    with _capture_out():
        devs = _query()
    if not devs:
        return _skip("test_real_devices_are_wasapi_only", "本机 WASAPI 下没有设备")
    assert all(d["hostapi"] in wasapi for d in devs), "设备表里混进了非 WASAPI 的条目"
    assert all("pa_index" in d for d in devs), "每条都要带 pa_index"
    names = [d["name"] for d in devs]
    assert len(names) == len(set(names)), f"还有同名重复：{names}"
    assert len(devs) < len(raw), \
        f"收敛后应少于原始条目（原始 {len(raw)} 条、收敛后 {len(devs)} 条）"
    print(f"  真机：{len(raw)} 条 → {len(devs)} 条（全是 WASAPI、无重复）OK")
    # 顺带把收敛后的清单打出来，方便和 Windows「声音」面板对照
    print("     收敛后：" + "、".join(
        f"{d['name']}({int(d['default_samplerate'])}Hz)" for d in devs))


if __name__ == "__main__":
    print("test_device_wasapi_filter:")
    test_keeps_only_wasapi()
    test_pa_index_is_true_index()
    test_falls_back_loudly_when_no_wasapi()
    test_falls_back_loudly_when_wasapi_empty()
    test_real_devices_are_wasapi_only()
    if _SKIPPED:
        print(f"  （跳过 {len(_SKIPPED)} 条：{'、'.join(_SKIPPED)}）")
    print("ALL PASSED")
