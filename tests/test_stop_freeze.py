"""回归：点「停止翻译」绝不许把界面线程冻住（用户实测 10~20s 无响应）。

## 事故（用户真机 2026-09-30 日志）

点「停止翻译」后窗口十几秒不动。日志里每次停止都长这样：

    22:30:14.414 [mic] 采集结束
    22:30:22.377 [gate] 输入门限：-28.5 dBFS ≥ 门限 → 开始上送     ← 停止 8s 后还在采！
    22:30:24.234 [loopback] 采集结束

七次停止的 `mic → loopback` 间隔中位数 **9.91s**（10.44/10.22/9.88/9.89/9.92/9.82），
且这期间第二条腿照常采集上送 —— 它的停止信号根本没发出去。

## 根因（三层，全部实测）

1. `vlt/session/qwen38.py` 的 `websockets.connect()` 没传 `close_timeout` → 默认 **10s**；
   真链路实测百炼服务端**不回 close 帧**，`Session.close()` 稳吃 10.30/10.32s
   （其中 `ws.close()` 10.01s，两次测量一致）。
2. `Engine.stop()` 在**调用方线程**上 `_stopped.wait(5)` + `_thread.join(5)` → 冻 ~10s。
3. `gui._stop()` 顺序 `for eng in self._engines: eng.stop()` → 双向下 **20.04s**
   （实测这段时间 Tk 事件循环处理了 **0** 个事件 = 窗口「未响应」），
   且第二个引擎在前一个收尾期间继续上送音频。

## 本文件断言的四件事

- 会话层：对端装死时 `close()` 有界（≤3s）**且留痕**（本仓库禁静默降级）；
- 引擎层：`request_stop()` 立即返回（<50ms）且**并发**下发（B 不必等 A 收尾）；
- 界面层：收尾要 5s 的引擎也不能让 `_stop()` 阻塞，收尾期间禁「开始」防抢设备；
- chatbox 排空预算收紧到 ≤2.5s，没排完的要留痕。

跑法：.venv/Scripts/python.exe tests/test_stop_freeze.py
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import vlt.engine as E                          # noqa: E402
import vlt.session.qwen38 as Q                  # noqa: E402
from vlt.config import AppConfig, Direction     # noqa: E402
from vlt.session.base import SessionConfig      # noqa: E402


# ---------------------------------------------------------------- 替身

class _DeafWS:
    """连上就装死的对端：握手给一帧 session.created，之后 close 帧永远不回。

    复刻真链路实测现象（百炼服务端不回 close 帧）—— 但把等待时间缩短成 6s，
    免得变异验证（停用修复）时一条用例要跑 10 秒。
    """

    def __init__(self, hang_s: float = 6.0) -> None:
        self.hang_s = hang_s
        self._sent_first = False
        self.sent: list[str] = []
        self.closed = False

    async def recv(self) -> str:
        if not self._sent_first:
            self._sent_first = True
            return '{"type":"session.created"}'
        await asyncio.sleep(3600)             # 之后不再有事件（装死）
        return ""

    async def send(self, data) -> None:        # noqa: ANN001
        if isinstance(data, str):
            self.sent.append(data)

    async def close(self, *a, **k) -> None:    # noqa: ANN002, ANN003
        await asyncio.sleep(self.hang_s)       # 对端不回 close 帧 → 只能靠超时兜底
        self.closed = True


class _FakeSession:
    """`close()` 需要 1s 的假会话：用来暴露「顺序 stop() 会晚发停止信号」。"""

    def __init__(self, cfg=None, close_s: float = 1.0) -> None:  # noqa: ANN001
        self.close_s = close_s
        self.closed = False

    async def start(self, on_text=None, on_audio=None, on_usage=None):  # noqa: ANN001
        return None

    async def send_audio(self, pcm: bytes) -> None:  # noqa: ARG002
        return None

    async def close(self) -> None:
        if self.close_s:
            await asyncio.sleep(self.close_s)
        self.closed = True

    def tick(self) -> None:
        return None


async def _fake_mic(session, tele, seconds: float = 0.0, device_pattern=None,  # noqa: ANN001
                    device_name=None, stop_event=None) -> None:
    """假采集器：复刻真实 mic 的契约 —— 无限循环，只认 stop_event。"""
    assert stop_event is not None, "引擎没把 stop_event 传给采集器 —— 停止路径必然失效"
    while not stop_event.is_set():
        await asyncio.sleep(0.05)


def _cfg() -> AppConfig:
    return AppConfig(
        session_base={"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
                      "workspace_id": "", "reconnect_backoff": [1],
                      "max_new_sessions_per_minute": 10, "final_silence_s": 1.0},
        directions={"mine": Direction(source_lang="zh", target_lang="en")},
        chatbox={}, merger={}, overlay={}, output={},
    )


class _Widget:
    def __init__(self) -> None:
        self.kw: dict = {}

    def configure(self, **kw) -> None:  # noqa: ANN003
        self.kw.update(kw)

    def state(self, *_a) -> None:
        return None


class _Root:
    """占位 Tk root：只提供 after/after_cancel（无头模式下 _poll 会用到）。"""

    def after(self, *_a, **_k) -> str:
        return "job"

    def after_cancel(self, *_a, **_k) -> None:
        return None


class _SlowEngine:
    """收尾要 0.6s 的引擎替身：**界面线程一旦同步等它就报错**。"""

    def __init__(self) -> None:
        self.request_calls = 0
        self.running = True

    def request_stop(self) -> None:
        self.request_calls += 1

    def stop(self, timeout: float = 5.0) -> None:      # noqa: ARG002
        raise AssertionError(
            "界面线程不许调用阻塞版 stop() —— 这正是「停止后 20s 无响应」的根因")

    def wait_stopped(self, timeout: float = 5.0) -> bool:   # noqa: ARG002
        time.sleep(0.6)
        self.running = False
        return True


# ---------------------------------------------------------------- 1) 会话层

def test_session_close_is_bounded_and_logged() -> None:
    captured: dict = {}

    class _Connect:
        async def __call__(self, uri, **kw):        # noqa: ANN001
            captured.update(kw)
            captured["uri"] = uri
            return _DeafWS()

    original = Q.websockets.connect
    Q.websockets.connect = _Connect()               # type: ignore[assignment]
    out = io.StringIO()
    try:
        async def run() -> float:
            s = Q.QwenLiveTranslateSession(SessionConfig(api_key="x"))
            await s.start(on_text=lambda d: None)
            t = time.perf_counter()
            await s.close()
            return time.perf_counter() - t

        with contextlib.redirect_stdout(out):
            elapsed = asyncio.run(run())
    finally:
        Q.websockets.connect = original             # type: ignore[assignment]

    assert "close_timeout" in captured, (
        "建连没传 close_timeout → 对端不回 close 帧时 websockets 默认干等 10s"
        "（真链路实测 ws.close() 10.01s，Session.close() 10.30s）")
    assert float(captured["close_timeout"]) <= 2.0, (
        f"close_timeout={captured['close_timeout']} 太宽松，停止还是要等这么久")
    assert elapsed < 3.0, (
        f"对端装死时 Session.close() 花了 {elapsed:.2f}s（必须收敛到 3s 内）")
    assert "[session]" in out.getvalue() and "close" in out.getvalue(), (
        f"关闭握手超时是**降级路径**，必须留痕（本仓库禁静默降级）；实际输出={out.getvalue()!r}")
    print(f"  close() 有界 OK（对端装死 {elapsed:.2f}s，且已留痕）")


# ---------------------------------------------------------------- 2) 引擎层

def test_request_stop_is_nonblocking_and_concurrent() -> None:
    original_session, original_mic = E.create_session, E.run_mic
    E.create_session = lambda scfg: _FakeSession(scfg, close_s=1.0)   # type: ignore[assignment]
    E.run_mic = _fake_mic                                            # type: ignore[assignment]
    try:
        engines = [E.Engine(cfg=_cfg(), direction="mine", source="mic", sinks=set(),
                            events=E.EngineEvents(), settle_s=30.0, dry_run=True)
                   for _ in range(2)]
        for e in engines:
            e.start()
        time.sleep(1.0)
        a, b = engines
        assert a.running and b.running, "两个引擎没都起来"

        t0 = time.perf_counter()
        a.request_stop()
        b.request_stop()
        elapsed = time.perf_counter() - t0

        assert elapsed < 0.1, (
            f"request_stop() 阻塞了 {elapsed * 1000:.0f}ms —— 它必须只发信号、不等收尾")
        assert a._stop_event.is_set() and b._stop_event.is_set(), "停止信号没下发到采集器"
        assert not a._stopped.is_set() and not b._stopped.is_set(), (
            "本用例需要「两个引擎都还在收尾中」才有区分度（A 的 close 要 1s）")

        threads = [e._thread for e in engines]
        for e in engines:
            assert e.wait_stopped(5.0), "引擎在 5s 内没收尾完"
        for e, th in zip(engines, threads):
            assert th is not None and not th.is_alive(), "引擎线程没收尾"
        print(f"  request_stop() 立即返回 OK（{elapsed * 1000:.1f}ms，两个引擎并发收尾）")
    finally:
        E.create_session = original_session      # type: ignore[assignment]
        E.run_mic = original_mic                 # type: ignore[assignment]


def test_chatbox_drain_is_bounded_and_logged() -> None:
    """停止时最多等 2s 排空 chatbox（原来 12×0.5s = 6s，纯白等）。"""
    original_session, original_mic = E.create_session, E.run_mic
    E.create_session = lambda scfg: _FakeSession(scfg, close_s=0.0)   # type: ignore[assignment]
    E.run_mic = _fake_mic                                            # type: ignore[assignment]
    out = io.StringIO()
    try:
        engine = E.Engine(cfg=_cfg(), direction="mine", source="mic", sinks={"chatbox"},
                          events=E.EngineEvents(), settle_s=30.0, dry_run=True)
        engine.start()
        time.sleep(0.8)
        cb = engine.chatbox
        assert cb is not None, "chatbox 没建起来（本用例需要一个真的 Chatbox）"
        for i in range(30):
            cb.send(f"排队消息 {i}", is_final=True)
        assert cb.pending_count > 0, "没能造出积压（限流应当把消息留在队列里）"

        t0 = time.perf_counter()
        with contextlib.redirect_stdout(out):
            engine.stop(timeout=10.0)
        elapsed = time.perf_counter() - t0
        assert elapsed < 3.0, (
            f"停止时在 chatbox 排空上花了 {elapsed:.2f}s（预算必须 ≤2s）")
        assert "未发完" in out.getvalue(), (
            f"没排完就放弃属于降级路径，必须留痕；实际输出={out.getvalue()[-600:]!r}")
        print(f"  chatbox 排空有界 OK（{elapsed:.2f}s，剩余条数已留痕）")
    finally:
        E.create_session = original_session      # type: ignore[assignment]
        E.run_mic = original_mic                 # type: ignore[assignment]


# ---------------------------------------------------------------- 3) 界面层

def test_gui_stop_does_not_block_main_thread() -> None:
    import vlt.i18n as _i18n
    _i18n.detect_system_language = lambda: "zh"      # CI 是英文系统：钉死语言
    from vlt.gui import TranslationGUI

    gui = TranslationGUI(headless=True)
    gui._root = _Root()                              # type: ignore[assignment]
    gui._start_btn = _Widget()                       # type: ignore[assignment]
    gui._stop_btn = _Widget()                        # type: ignore[assignment]
    fake = _SlowEngine()
    gui._engines = [fake]                            # type: ignore[list-item]
    gui._engine_dirs = ["mine"]

    t0 = time.perf_counter()
    gui._stop()
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.3, (
        f"gui._stop() 在界面线程上阻塞了 {elapsed:.2f}s"
        "（真机实测双向下 20.04s，等于窗口「未响应」）")
    assert fake.request_calls == 1, "没有向引擎下发停止信号"
    assert gui._start_btn.kw.get("state") == "disabled", (
        "收尾期间必须禁掉「开始翻译」：否则会与还在关设备的旧引擎抢麦克风/虚拟声卡")
    assert not gui._engines, "引擎列表应当立刻移走（收尾由后台线程负责）"

    deadline = time.time() + 5.0
    while time.time() < deadline and gui._start_btn.kw.get("state") != "normal":
        gui._poll()
        time.sleep(0.05)
    assert gui._start_btn.kw.get("state") == "normal", (
        "收尾完成后界面没恢复（用户会以为还卡着）")
    assert gui._stop_btn.kw.get("state") == "disabled"
    print(f"  gui._stop() 不阻塞 OK（{elapsed * 1000:.1f}ms 返回，后台收尾后自动恢复）")


if __name__ == "__main__":
    print("test_stop_freeze:")
    test_session_close_is_bounded_and_logged()
    test_request_stop_is_nonblocking_and_concurrent()
    test_chatbox_drain_is_bounded_and_logged()
    test_gui_stop_does_not_block_main_thread()
    print("ALL PASSED")
