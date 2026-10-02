#!/usr/bin/env python
"""loopback 采集的**声道数**回归：必须按设备原生声道数打开，压成 2 会一条都收不到。

## 真实事故（海外用户实测，2026-10-02）

    [loopback] 采集端点「Voicemeeter In 6 (VB-Audio Voicemeeter VAIO) [Loopback]」48000Hz ×8ch
    [theirs][error] 运行错误：[Errno -9998] Invalid number of channels

他的设备是 **7.1（8 声道）**。改动前 `PyaudioLoopbackSource` 一律 `min(2, channels)` 打开，
而 WASAPI 的 loopback 端点**只能用端点混音格式的声道数打开** → PortAudio 直接拒：
`paInvalidChannelCount`（**-9998**）。设置里的电平条复用同一处代码，所以两处一起挂。

本机反向复现（2 声道设备，把声道数强行写成 8）：

    OSError: [Errno -9998] Invalid number of channels     ← 与用户日志逐字相同

## 这个测试怎么钉住它

假 pyaudiowpatch 记录**实际请求的声道数**，钉住三条不变式：
① 按原生声道数开；② 原生打不开才回落 2 声道，且**两个分支各留一行日志**（禁静默降级）；
③ 候选全打不开时必须抛错 + terminate，绝不静默返回一个收不到数据的采集源。

另加两条**真设备**用例（Windows 才跑）：原生声道数照开不报错；强行写 8 时真会 -9998 并回落。

⚠️ 日志走 **print**（`vlt/engine.py` 里 `[loopback]` 那族就是 print 进日志文件的），
不是 logging —— 所以这里也按 stdout 捕获来断言，别改成 caplog。
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
    """捕获构造期间 stdout 上的 `[loopback]` 行。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


# ---------------------------------------------------------------- 假 pyaudiowpatch

class _FakeStream:
    def get_read_available(self) -> int:
        return 0

    def read(self, n, exception_on_overflow=False):  # noqa: ANN001, ARG002
        return b""

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        pass


def _install_fake(plan: dict[int, Exception | None]) -> tuple[dict, object]:
    """装一个假 pyaudiowpatch：`plan[声道数]` = 打开该声道数时抛的异常（None = 成功）。"""
    created: dict = {}

    class _FakePyAudio:
        paInt16 = 8

        def __init__(self) -> None:
            self.plan = plan
            self.attempts: list[int] = []
            self.terminated = False
            created["pa"] = self

        def open(self, **kw):  # noqa: ANN003, ANN201
            ch = kw["channels"]
            self.attempts.append(ch)
            err = self.plan.get(ch)
            if err is not None:
                raise err
            return _FakeStream()

        def terminate(self) -> None:
            self.terminated = True

    mod = types.ModuleType("pyaudiowpatch")
    mod.PyAudio = _FakePyAudio          # type: ignore[attr-defined]
    mod.paInt16 = 8                     # type: ignore[attr-defined]
    old = sys.modules.get("pyaudiowpatch")
    sys.modules["pyaudiowpatch"] = mod
    return created, old


def _restore(old: object) -> None:
    if old is None:
        sys.modules.pop("pyaudiowpatch", None)
    else:
        sys.modules["pyaudiowpatch"] = old  # type: ignore[assignment]


def _make_source(*, channels: int, rate: int = 48000):
    from vlt.platform.win import PyaudioLoopbackSource
    loop = asyncio.new_event_loop()
    src = PyaudioLoopbackSource(loop, device_index=63, name="Fake 7.1 Loopback",
                                rate=rate, channels=channels)
    return src, loop


# ---------------------------------------------------------------- 假模块用例

def test_opens_with_native_channels() -> None:
    """8 声道设备 → 必须**按 8 声道**打开（改动前压成了 2，改完还压就是这次的事故）。"""
    created, old = _install_fake({8: None})
    try:
        with _capture_out() as buf:
            src, loop = _make_source(channels=8)
        try:
            pa = created["pa"]
            assert pa.attempts == [8], (
                f"没有按原生声道数打开：实际请求过 {pa.attempts} 声道 —— "
                "WASAPI loopback 压成 2 会被 PortAudio 拒（-9998）"
            )
            assert src.channels == 8, f"source.channels 应为 8（下游按它降混），实际 {src.channels}"
            assert not pa.terminated, "打开成功不该 terminate PortAudio"
            assert "已回落" not in buf.getvalue(), f"一次就开成功，不该有回落：{buf.getvalue()}"
            print(f"  8ch 设备按原生声道数打开 OK（attempts={pa.attempts}）")
        finally:
            src.close()
            loop.close()
    finally:
        _restore(old)


def test_falls_back_to_stereo_when_native_fails() -> None:
    """原生声道数打不开 → 回落 2 声道，且**失败行与降级行都要留痕**（禁静默降级）。"""
    err = OSError(-9998, "Invalid number of channels")
    created, old = _install_fake({8: err, 2: None})
    try:
        with _capture_out() as buf:
            src, loop = _make_source(channels=8)
        try:
            pa = created["pa"]
            out = buf.getvalue()
            assert pa.attempts == [8, 2], f"回落顺序应为 8 → 2，实际 {pa.attempts}"
            assert src.channels == 2, f"回落成功后 source.channels 应为 2，实际 {src.channels}"
            assert "按 8 声道打开失败" in out and "-9998" in out, (
                f"原生失败那一行没留痕：{out!r}"
            )
            assert "已回落 2ch" in out, f"回落那一行没留痕：{out!r}"
            print(f"  原生失败 → 回落 2 声道 OK（attempts={pa.attempts}，两条日志都在）")
        finally:
            src.close()
            loop.close()
    finally:
        _restore(old)


def test_all_candidates_fail_raises_and_terminates() -> None:
    """候选全打不开 → 抛错 + terminate。绝不静默返回一个收不到数据的采集源。"""
    err = OSError(-9998, "Invalid number of channels")
    created, old = _install_fake({8: err, 2: err})
    try:
        with _capture_out():
            try:
                _make_source(channels=8)
            except OSError as exc:
                assert "-9998" in str(exc), f"抛出来的应是原始 PortAudio 错误，实际：{exc}"
            else:
                raise AssertionError("全都打不开时**必须抛错**（响亮失败），不能静默返回采集源")
        assert created["pa"].terminated, "失败路径必须 terminate PortAudio（否则句柄泄漏）"
        print("  全部候选都失败 → 抛原始错误 + terminated OK")
    finally:
        _restore(old)


# ---------------------------------------------------------------- 真设备用例

def _real_loopback() -> dict | None:
    import pyaudiowpatch as pyaudio
    pa = pyaudio.PyAudio()
    try:
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if d.get("isLoopbackDevice") or "loopback" in str(d.get("name", "")).lower():
                return dict(d)
    finally:
        pa.terminate()
    return None


def _open_real(d: dict, channels: int):
    from vlt.platform.win import PyaudioLoopbackSource
    loop = asyncio.new_event_loop()
    try:
        src = PyaudioLoopbackSource(loop, device_index=int(d["index"]),
                                    name=str(d["name"]), rate=int(d["defaultSampleRate"]),
                                    channels=channels)
        return src, loop
    except Exception:
        loop.close()
        raise


def test_real_device_opens_with_native_channels() -> None:
    """真设备：按原生声道数打开（2 声道设备就是 2），不该有失败/回落行。"""
    if sys.platform != "win32":
        return _skip("test_real_device_opens_with_native_channels", "非 Windows：没有 WASAPI loopback")
    d = _real_loopback()
    if d is None:
        return _skip("test_real_device_opens_with_native_channels", "本机没有 loopback 端点")
    native = int(d["maxInputChannels"])
    with _capture_out() as buf:
        src, loop = _open_real(d, native)
    try:
        assert src.channels == native, f"source.channels 应为原生 {native}，实际 {src.channels}"
        assert "打开失败" not in buf.getvalue(), f"按原生声道数打开不该失败：{buf.getvalue()!r}"
        print(f"  真设备「{d['name']}」原生 {native}ch 打开 OK")
    finally:
        src.close()
        loop.close()


def test_real_device_forced_8ch_falls_back() -> None:
    """真设备**反向验证**：非 8 声道设备上强行按 8 声道开 → 真会 -9998 → 回落原生并留痕。

    这条同时证明「-9998 = 声道数不匹配」这个判据本身，报错与用户日志逐字相同。
    """
    if sys.platform != "win32":
        return _skip("test_real_device_forced_8ch_falls_back", "非 Windows：没有 WASAPI loopback")
    d = _real_loopback()
    if d is None:
        return _skip("test_real_device_forced_8ch_falls_back", "本机没有 loopback 端点")
    native = int(d["maxInputChannels"])
    if native >= 8:
        return _skip("test_real_device_forced_8ch_falls_back",
                     f"本机设备本身就是 {native} 声道（这条是给非 8 声道设备的反向验证）")
    with _capture_out() as buf:
        src, loop = _open_real(d, 8)
    try:
        out = buf.getvalue()
        assert src.channels == native, (
            f"8 声道打不开后应回落到原生 {native}ch，实际 {src.channels}"
        )
        assert "-9998" in out, (
            f"本机 {native}ch 设备上按 8ch 打开，预期 PortAudio 报 -9998（与用户日志同码）：{out!r}"
        )
        assert "已回落" in out, f"回落那一行没留痕：{out!r}"
        print(f"  真设备「{d['name']}」强开 8ch → 果然 -9998 → 回落 {native}ch OK")
    finally:
        src.close()
        loop.close()


if __name__ == "__main__":
    print("test_loopback_channels:")
    test_opens_with_native_channels()
    test_falls_back_to_stereo_when_native_fails()
    test_all_candidates_fail_raises_and_terminates()
    test_real_device_opens_with_native_channels()
    test_real_device_forced_8ch_falls_back()
    if _SKIPPED:
        print(f"  （跳过 {len(_SKIPPED)} 条：{'、'.join(_SKIPPED)}）")
    print("ALL PASSED")
