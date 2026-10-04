"""会话正常结束序列验收（issue #53）：`session.finish → session.finished → 断连`。

## 真实问题

海外版 QwenCloud 后台把**每个** Realtime WS 会话都记成 `400`，而应用侧翻译完全正常
（有 520 input / 166 output tokens 的用量，说明不是连上就被拒）。官方文档里
Qwen3.8-LiveTranslate 的结束序列是：

    client → session.finish ；server → session.finished ；client → close WebSocket

原实现发完 `session.finish` 只 `sleep(0.3)` 就 `ws.close()`，**从不处理** `session.finished`；
而且接收循环在 `_closing` 时会在处理任何一帧前就 `return` —— 即使服务端回了也收不到。

## 这里钉住什么

1. `close()` 发完 `session.finish` 会**等到** `session.finished` 才断连（日志留痕）；
2. 收尾阶段（`_closing=True`）接收循环**仍然**认 `session.finished`（这是修复的核心，
   只删掉那句提前 return 就等于没修）；
3. 服务端不回时**有界**：等满 `FINISH_WAIT_S` 就强制断连，并且日志写明「没收到」；
4. 连接中途断开 → 不再白等，日志写明「连接断开」而不是冒充“收到了”；
5. 从未 `start()` 过的会话 `close()` 不抛、不卡。

（`_got_finished` 是「真·收到」的证据，必须只在真的收到 `session.finished` 时置位；
停止日志靠它区分 QwenCloud / 国内线路，不能被「连接断了」冒充。）
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _FakeWS:
    """最小 websockets 替身：`send` / `close` / `async for` 三件事。

    `on_send` 让用例决定「服务端」收到某条消息后怎么回（塞帧 / 断开 / 装死）。
    """

    def __init__(self, on_send=None) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._on_send = on_send

    async def send(self, payload: str) -> None:
        msg = json.loads(payload)
        self.sent.append(msg)
        if self._on_send is not None:
            await self._on_send(self, msg)

    async def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(None)          # 结束 async for

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


def _new_session(ws) -> tuple[object, asyncio.Task]:
    """造一个已「连上」的会话：手工铺好 ws / 事件 / 接收循环（不碰网络）。"""
    from vlt.session.base import SessionConfig
    from vlt.session.qwen38 import QwenLiveTranslateSession

    s = QwenLiveTranslateSession(SessionConfig())      # 默认 model=qwen3.8 → gen 38
    s._ws = ws
    s._finished_evt = asyncio.Event()
    task = asyncio.create_task(s._recv_loop())
    return s, task


def test_close_waits_for_session_finished() -> None:
    """★ 收到 session.finished 才断连；且不该白等满超时（正常路径应当是秒回）。"""
    from vlt.session.qwen38 import FINISH_WAIT_S

    async def run() -> tuple[str, float, _FakeWS]:
        async def on_send(ws: _FakeWS, msg: dict) -> None:
            if msg.get("type") == "session.finish":
                # 服务端回「已结束」——修复前这句永远被接收循环丢掉。
                ws._queue.put_nowait(json.dumps({"type": "session.finished"}))

        ws = _FakeWS(on_send)
        s, task = _new_session(ws)
        buf = io.StringIO()
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(buf):
            await s.close()
        elapsed = time.perf_counter() - t0
        task.cancel()
        assert FINISH_WAIT_S >= 0.5, "FINISH_WAIT_S 太小的话正常路径也可能被误判成超时"
        return buf.getvalue(), elapsed, ws

    out, elapsed, ws = asyncio.run(run())
    assert "已收到 session.finished" in out, f"没等到 session.finished 就断连了：{out!r}"
    assert ws.closed, "ws 没被关掉"
    assert elapsed < 0.5, f"收到 finished 后不该再白等（用了 {elapsed:.2f}s）"
    print("  close() 等到 session.finished 才断连（且秒回）OK")


def test_close_is_where_finished_gets_dropped_without_the_fix() -> None:
    """★ 核心回归：收尾阶段也必须认 session.finished（不是「提前 return 就完了」）。"""
    async def run() -> tuple[bool, str]:
        async def on_send(ws: _FakeWS, msg: dict) -> None:
            if msg.get("type") == "session.finish":
                ws._queue.put_nowait(json.dumps({"type": "session.finished"}))

        ws = _FakeWS(on_send)
        s, task = _new_session(ws)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            await s.close()
        got = s._got_finished
        task.cancel()
        return got, buf.getvalue()

    got, out = asyncio.run(run())
    assert got, "收尾阶段收到的 session.finished 被丢了（接收循环的 closing 分支又提前 return 了）"
    assert "已收到 session.finished" in out
    print("  收尾阶段的 session.finished 不再被丢 OK")


def test_close_is_bounded_when_server_never_finishes() -> None:
    """服务端装死（不回 session.finished）→ 等满上限就强制断连，并且留痕。"""
    from vlt.session.qwen38 import FINISH_WAIT_S

    async def run() -> tuple[str, float, _FakeWS]:
        ws = _FakeWS()                        # on_send=None：收到什么都装死
        s, task = _new_session(ws)
        buf = io.StringIO()
        t0 = time.perf_counter()
        with contextlib.redirect_stdout(buf):
            await s.close()
        elapsed = time.perf_counter() - t0
        task.cancel()
        return buf.getvalue(), elapsed, ws

    out, elapsed, ws = asyncio.run(run())
    assert "没收到 session.finished" in out, f"装死路径必须留痕：{out!r}"
    assert ws.closed, "超时后也必须断连"
    assert elapsed >= FINISH_WAIT_S * 0.9, f"没等满上限就断了？（{elapsed:.2f}s）"
    assert elapsed < FINISH_WAIT_S + 1.5, f"等待无界了（{elapsed:.2f}s）"
    print(f"  服务端不回 session.finished → {FINISH_WAIT_S:.1f}s 有界收尾 OK")


def test_close_reports_connection_drop_instead_of_faking_finished() -> None:
    """等的时候连接断了 → 日志如实说「连接断开」，不许冒充收到 finished。"""
    async def run() -> tuple[str, bool, _FakeWS]:
        async def on_send(ws: _FakeWS, msg: dict) -> None:
            if msg.get("type") == "session.finish":
                ws._queue.put_nowait(None)     # 服务端没发 finished 就直接断开

        ws = _FakeWS(on_send)
        s, task = _new_session(ws)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            await s.close()
        got = s._got_finished
        task.cancel()
        return buf.getvalue(), got, ws

    out, got, ws = asyncio.run(run())
    assert "连接断开" in out, f"断开路径没留痕：{out!r}"
    assert "已收到 session.finished" not in out, "把「断开」冒充成了「收到 finished」"
    assert not got, "_got_finished 不能由「连接断了」置位"
    assert ws.closed
    print("  等待期间连接断开 → 如实留痕、不冒充收到 OK")


def test_close_without_start_does_not_hang() -> None:
    """从未 start()（没有 ws / 没有事件）的会话：close() 不抛、不卡。"""
    from vlt.session.base import SessionConfig
    from vlt.session.qwen38 import QwenLiveTranslateSession

    s = QwenLiveTranslateSession(SessionConfig())

    async def run() -> float:
        t0 = time.perf_counter()
        await s.close()
        return time.perf_counter() - t0

    elapsed = asyncio.run(run())
    assert elapsed < 0.2, f"没 start 过的会话 close() 卡了 {elapsed:.2f}s"
    print("  未 start() 的 close() 不卡 OK")


if __name__ == "__main__":
    print("test_session_finish:")
    test_close_waits_for_session_finished()
    test_close_is_where_finished_gets_dropped_without_the_fix()
    test_close_is_bounded_when_server_never_finishes()
    test_close_reports_connection_drop_instead_of_faking_finished()
    test_close_without_start_does_not_hang()
    print("ALL PASSED")
