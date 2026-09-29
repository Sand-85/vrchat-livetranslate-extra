#!/usr/bin/env python
"""loopback 采集收尾顺序的回归测试：**关流之前，喂数据的线程必须已经退出**。

## 真实事故（用户实测闪退，faulthandler 抓到现场）

    File "...\\pyaudiowpatch\\__init__.py", line 640 in read     ← 崩在阻塞读里

一个线程卡在阻塞的 `stream.read()` 里，另一个线程把流 stop/close、把 PortAudio
terminate 掉 → 访问违规（用户点「停止翻译」时闪退，退出码 139）。

## 这个测试怎么钉住它

用假的 pyaudiowpatch 记录**调用顺序**，断言：一旦关了流，就不能再出现 `read()`；
且关流的那一刻，喂数据线程不能还活着。

## 为什么改成直接测平台后端

原来这里是 `E.run_loopback` + 打桩 `E.pick_loopback_device`，那套接口在
「平台抽象层」重构后没了（`run_loopback` 现在从 `CaptureBackend` 拿 `AudioSource`）。
不变式本身没变，只是**归属**变清楚了：收尾顺序是 `PyaudioLoopbackSource` 的实现细节，
所以直接测它。

好处：注入的是**假模块**而不是真的 `pyaudiowpatch`，于是在 Linux 上也能跑
（真模块只有 Windows 有 wheel），这条回归在两端都不会失守。
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EVENTS: list[str] = []
# 喂数据线程的名字由 QueueAudioSource 生成：f"vlt-{label}-pump"
READER_NAME = "vlt-loopback-pump"


def _reader_alive() -> bool:
    return any(t.name == READER_NAME for t in threading.enumerate())


class _FakeStream:
    """模拟真实 loopback 端点：先有数据，然后**变安静**（get_read_available 返回 0）。

    变安静这一步是关键——真实端点在没音频播放时就是这个状态，
    而阻塞式 read() 会在这里永久卡住（这正是崩溃的条件）。
    """

    def __init__(self, with_audio_calls: int = 8) -> None:
        self._polls = 0
        self._with_audio_calls = with_audio_calls

    def get_read_available(self) -> int:
        self._polls += 1
        return 4800 if self._polls <= self._with_audio_calls else 0

    def read(self, n, exception_on_overflow=False):   # noqa: ANN001, ARG002
        EVENTS.append("read")
        time.sleep(0.02)                              # 模拟一次读
        return b"\x00" * (n * 2 * 2)                  # 立体声 16bit 静音

    def stop_stream(self) -> None:
        EVENTS.append("stop_stream_WITH_READER_ALIVE" if _reader_alive() else "stop_stream")

    def close(self) -> None:
        EVENTS.append("close_WITH_READER_ALIVE" if _reader_alive() else "close")


class _FakePyAudio:
    paInt16 = 8

    def open(self, **kw):  # noqa: ANN003, ANN201
        return _FakeStream()

    def terminate(self) -> None:
        EVENTS.append("terminate_WITH_READER_ALIVE" if _reader_alive() else "terminate")


def _install_fake_pyaudiowpatch() -> None:
    mod = types.ModuleType("pyaudiowpatch")
    mod.PyAudio = _FakePyAudio          # type: ignore[attr-defined]
    mod.paInt16 = 8                     # type: ignore[attr-defined]
    sys.modules["pyaudiowpatch"] = mod


def _drive(src, seconds: float) -> None:
    """跑一段「读 → 关」，模拟引擎的采集循环 + finally 收尾。"""

    async def go() -> None:
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            await src.read(timeout=0.1)
        src.close()

    asyncio.run(go())


def _make_source():
    from vlt.platform.win import PyaudioLoopbackSource
    loop = asyncio.new_event_loop()
    try:
        src = PyaudioLoopbackSource(loop, device_index=63, name="Fake Loopback",
                                    rate=48000, channels=2)
    finally:
        loop.close()
    return src


def test_reader_joins_before_stream_close() -> None:
    EVENTS.clear()
    src = _make_source()
    src.start()
    _drive(src, 0.4)

    assert "stop_stream" in EVENTS or "stop_stream_WITH_READER_ALIVE" in EVENTS, \
        f"没有关闭流，采集可能没跑起来：{EVENTS}"

    # 核心不变式：关流/释放 PortAudio 时，喂数据线程必须已经退出
    bad = [e for e in EVENTS if e.endswith("_WITH_READER_ALIVE")]
    assert not bad, (
        f"关闭流/释放 PortAudio 时喂数据线程还活着（{bad}）—— 这正是访问违规的原因："
        f"一个线程卡在阻塞 read() 里，另一个线程把流销毁了。调用序列：{EVENTS}"
    )
    print(f"  teardown order OK（{len(EVENTS)} 次调用，关流前线程已退出）")


def test_reader_thread_exits() -> None:
    EVENTS.clear()
    src = _make_source()
    src.start()
    _drive(src, 0.3)

    alive = [t.name for t in threading.enumerate() if t.name == READER_NAME]
    assert not alive, (
        "采集返回后喂数据线程还活着 —— 它必须在关闭流之前被 join 掉，"
        "否则线程会在别人销毁流的同时继续 read()"
    )
    print("  reader thread joined OK（采集返回时线程已退出）")


def test_close_is_idempotent() -> None:
    """close() 必须幂等：引擎的 finally 与 Engine.stop() 都可能调它。"""
    EVENTS.clear()
    src = _make_source()
    src.start()
    src.close()
    src.close()
    stops = [e for e in EVENTS if e.startswith("stop_stream")]
    assert len(stops) == 1, f"close() 被调了两次，底层流被关了 {len(stops)} 次：{EVENTS}"
    print("  close() 幂等 OK（重复调用只关一次流）")


if __name__ == "__main__":
    _install_fake_pyaudiowpatch()
    print("test_loopback_teardown:")
    test_reader_joins_before_stream_close()
    test_reader_thread_exits()
    test_close_is_idempotent()
    print("ALL PASSED")
