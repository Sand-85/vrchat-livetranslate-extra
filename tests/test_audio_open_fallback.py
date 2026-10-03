#!/usr/bin/env python
"""WASAPI 打开失败时的两条兜底：**麦克风按原生采样率开** + **同名回落**。

## 真机事故（2026-10-02 测试者日志，v0.7.3）

设备表收敛到 WASAPI 之后，两条腿各挂了一次 —— 都是「WASAPI 端点跟别家不一样」：

    [mic] 采集线程退出：PortAudioError: Error opening RawInputStream:
          Invalid sample rate [PaErrorCode -9997]
    [mine][error] 打开虚拟声卡失败（#40 VoiceMeeter Input (VB-Audio VoiceMeeter VAIO)）：
          Unanticipated host error [PaErrorCode -9999]: 'WdmSyncIoctl: DeviceIoControl
          GLE = 0x00000490 …' [Windows WDM-KS error 0]

本机实测根因（同一台机器，真 `sd.RawInputStream`）：

    麦克风 (Realtek)  WASAPI @16000 → -9997 Invalid sample rate ｜ @48000 → OK
    麦克风 (Realtek)  MME    @16000 → OK（PortAudio 自己重采样）

→ **WASAPI 共享模式只认端点原生采样率**；16kHz 只有 MME/DirectSound 会帮你重采样。
  所以麦克风改成按原生采样率开（下游 `to_16k_mono` 本来就负责转 16k），
  同时两条腿都带上「同名设备在别的 host API 下」的回落候选。

## 这个测试钉住什么

① 麦克风按**原生采样率**打开（不是写死的 16k）；② 首选打不开 → 按同名回落且**逐次留痕**；
③ 虚拟声卡（译音输出）同理；④ 回落候选本身算得对；⑤ 真机（Windows）真开一次麦。
"""
from __future__ import annotations

import asyncio
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
]

# 0 = MME（同名的重复项，44100）；1 = WASAPI（真身，48000）
_DEVICES = [
    {"name": "Fake Mic", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0,
     "default_samplerate": 44100.0},
    {"name": "Fake Mic", "hostapi": 2, "max_input_channels": 2, "max_output_channels": 0,
     "default_samplerate": 48000.0},
    {"name": "Fake Out", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2,
     "default_samplerate": 44100.0},
    {"name": "Fake Out", "hostapi": 2, "max_input_channels": 0, "max_output_channels": 2,
     "default_samplerate": 48000.0},
]


class _FakeStream:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


class _FakeSD(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("sounddevice")
        self.opened: list[dict] = []
        self.out_opened: list[dict] = []
        self.fail_devices: set[int] = set()

    def query_devices(self, device=None, *a, **k):     # noqa: ANN001
        if device is None:
            return [dict(d) for d in _DEVICES]
        return dict(_DEVICES[int(device)])

    def query_hostapis(self, *a, **k):                 # noqa: ANN001
        return [dict(x) for x in _APIS]

    def RawInputStream(self, **kw):                    # noqa: N802
        self.opened.append(dict(kw))
        if kw.get("device") in self.fail_devices:
            raise OSError(f"PortAudioError: Error opening RawInputStream: "
                          f"Invalid sample rate [PaErrorCode -9997]（device={kw.get('device')}）")
        return _FakeStream()

    def RawOutputStream(self, **kw):                   # noqa: N802
        self.out_opened.append(dict(kw))
        if kw.get("device") in self.fail_devices:
            raise OSError("PortAudioError: Unanticipated host error [PaErrorCode -9999]"
                          "（WdmSyncIoctl…）")
        return _FakeStream()


def _install(fail_devices=()):
    import vlt.platform as P
    import vlt.platform.win as W

    fake = _FakeSD()
    fake.fail_devices = set(fail_devices)
    old_sd = sys.modules.get("sounddevice")
    old_backend = P.device_backend
    sys.modules["sounddevice"] = fake
    P.device_backend = lambda: W          # devices.py / engine 都走这一条
    return fake, old_sd, old_backend


def _restore(old_sd, old_backend) -> None:
    import vlt.platform as P

    P.device_backend = old_backend
    if old_sd is None:
        sys.modules.pop("sounddevice", None)
    else:
        sys.modules["sounddevice"] = old_sd


def _open_mic_and_close(name: str, rate: int = 16000):
    """真跑一遍 open_mic（开流 → 稍等 → 关流），返回 source。"""
    async def go():
        from vlt.platform.win import open_mic
        src = open_mic(name, rate=rate, channels=1, blocksize=1600)
        await asyncio.sleep(0.05)
        src.close()
        return src
    return asyncio.run(go())


# ---------------------------------------------------------------- 用例

def test_mic_opens_at_native_rate() -> None:
    """① 麦克风必须按**设备原生采样率**打开 —— 写死 16k 会被 WASAPI 拒（真机 -9997）。"""
    fake, old_sd, old_backend = _install()
    try:
        with _capture_out():
            src = _open_mic_and_close("Fake Mic")
        assert fake.opened, "麦克风根本没开流"
        kw = fake.opened[0]
        assert kw["samplerate"] == 48000, \
            f"应按原生 48000Hz 开（不是写死的 16k），实际 {kw['samplerate']}"
        assert kw["device"] == 1, f"应打开 WASAPI 那条（pa_index=1），实际 {kw['device']}"
        assert src.rate == 48000, f"source.rate 也该是 48000（下游按它重采样），实际 {src.rate}"
        print(f"  麦克风按原生采样率打开 OK（device=#{kw['device']} @{kw['samplerate']}Hz）")
    finally:
        _restore(old_sd, old_backend)


def test_mic_falls_back_to_same_name() -> None:
    """② 首选打不开 → 按同名回落到别的 host API，且**每次尝试都留痕**。"""
    fake, old_sd, old_backend = _install(fail_devices={1})
    try:
        with _capture_out() as buf:
            _open_mic_and_close("Fake Mic")
        assert [k["device"] for k in fake.opened] == [1, 0], \
            f"应先试 WASAPI(#1) 再回落 MME(#0)，实际 {[k['device'] for k in fake.opened]}"
        out = buf.getvalue()
        assert "设备 #1 打不开" in out, f"失败那一条要留痕：{out!r}"
        assert "已回落到同名设备 #0" in out, f"回落那一条要留痕：{out!r}"
        print("  麦克风首选失败 → 同名回落 OK（#1 失败日志 + #0 回落日志都在）")
    finally:
        _restore(old_sd, old_backend)


def test_virtualmic_falls_back_to_same_name() -> None:
    """③ 虚拟声卡（译音输出）同理：Voicemeeter 的 WASAPI 端点实测打不开。"""
    fake, old_sd, old_backend = _install(fail_devices={3})
    try:
        from vlt.output.virtualmic import VirtualMic
        seen: list[tuple[str, str]] = []
        vm = VirtualMic(device_index=3, device_name="Fake Out", sample_rate=48000,
                        on_status=lambda level, msg: seen.append((level, msg)),
                        device_fallbacks=[2])
        ok = vm.open()
        assert ok, "回落候选在，就该打开成功"
        assert [k["device"] for k in fake.out_opened] == [3, 2], \
            f"应先试 #3 再回落 #2，实际 {[k['device'] for k in fake.out_opened]}"
        assert vm._open_device_index == 2, "实际打开的应是回落那条"
        joined = " ".join(m for _l, m in seen)
        assert "打不开" in joined and "已回落到同名设备 #2" in joined, f"两条状态都要有：{seen}"
        vm.close()
        print("  虚拟声卡首选失败 → 同名回落 OK（打开的是 #2，状态栏两条都在）")
    finally:
        _restore(old_sd, old_backend)


def test_fallback_candidates_helper() -> None:
    """④ 回落候选本身：同名 + 排除首选 + 去重。"""
    fake, old_sd, old_backend = _install()
    try:
        import vlt.platform.win as W
        got_in = W.same_name_fallbacks("Fake Mic", "input", exclude=1)
        got_out = W.same_name_fallbacks("Fake Out", "output", exclude=3)
        assert got_in == [0], f"输入回落候选应为 [0]，实际 {got_in}"
        assert got_out == [2], f"输出回落候选应为 [2]，实际 {got_out}"
        assert W.same_name_fallbacks("不存在", "input") == []
        print("  回落候选计算 OK（同名、排除首选、不认识的名字返回空）")
    finally:
        _restore(old_sd, old_backend)


def test_real_mic_opens_at_native_rate() -> None:
    """⑤ 真机（Windows）：真开一次麦克风，采样率必须是设备原生值。"""
    if sys.platform != "win32":
        return _skip("test_real_mic_opens_at_native_rate", "非 Windows：没有 WASAPI")
    import sounddevice as sd
    devs = sd.query_devices()
    apis = [a["name"] for a in sd.query_hostapis()]
    cand = None
    for i, d in enumerate(devs):
        if d["max_input_channels"] > 0 and "WASAPI" in apis[d["hostapi"]]:
            cand = (i, d)
            break
    if cand is None:
        return _skip("test_real_mic_opens_at_native_rate", "本机 WASAPI 下没有输入设备")
    idx, d = cand
    native = int(d["default_samplerate"])
    try:
        src = _open_mic_and_close(str(d["name"]))
    except Exception as exc:                     # noqa: BLE001
        return _skip("test_real_mic_opens_at_native_rate",
                     f"本机麦克风打不开（{type(exc).__name__}: {exc}）")
    assert src.rate == native, f"应按原生 {native}Hz 打开，实际 {src.rate}"
    print(f"  真机麦克风「{d['name']}」按原生 {native}Hz 打开 OK")


if __name__ == "__main__":
    print("test_audio_open_fallback:")
    test_mic_opens_at_native_rate()
    test_mic_falls_back_to_same_name()
    test_virtualmic_falls_back_to_same_name()
    test_fallback_candidates_helper()
    test_real_mic_opens_at_native_rate()
    if _SKIPPED:
        print(f"  （跳过 {len(_SKIPPED)} 条：{'、'.join(_SKIPPED)}）")
    print("ALL PASSED")
