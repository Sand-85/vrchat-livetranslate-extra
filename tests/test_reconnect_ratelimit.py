"""重连退避：服务端限流（1007）时不能再 2s 一撞（2026-10-17 真机「俄语失效」）。

真机现象（`logs/gui_20261017_174429.log`，20:09:44–58）：一分钟内 6 次语言切换/重开 + 35 条重连日志，
服务端一直回 `1007 ... Requests rate limit exceeded`；而我们的 `_reconnect_loop` 每次都从 2s 重新数，
于是每 2s 再撞一次、把限流窗口一直续着 → 用户看到的是「这个方向彻底没声」。

本用例守三件事（离线、不连网）：
  A) 限流原因 → 走**限流台阶** 30/60/120，提示里明说「服务端限流」，而不是 2s/5s；
  B) 普通断开 → 台阶保持原样（2/5），别把正常抖动也拖慢；
  C) 台阶**跨会话累计**（第二次风暴不从第一级重来）；会话活得 ≥60s 才归零。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlt.engine import Engine, _is_rate_limited                    # noqa: E402

results: list[tuple[str, bool]] = []
WAITS: list[float] = []


def check(name: str, ok: bool) -> None:
    results.append((name, ok))
    print(f"  {name}  {'OK' if ok else '✗'}")


class _Ev:
    def __init__(self) -> None:
        self.msgs: list[tuple[str, str]] = []

    def on_status(self, level: str, msg: str, **kw) -> None:        # noqa: ANN003, ARG002
        self.msgs.append((level, msg))


class _Sess:
    def __init__(self, fail_reason: str) -> None:
        self.fail_reason = fail_reason


class _Cfg:
    def __init__(self) -> None:
        self.session_base = {"reconnect_backoff": [2, 5], "ratelimit_backoff": [30, 60, 120]}


def _engine(*, fail_reason: str, started_ago: float = 0.0, fail_times: int = 1) -> Engine:
    """造一个只够跑 `_reconnect_loop` 的引擎（不连网、不起线程）。"""
    eng = Engine.__new__(Engine)
    eng._stopping = False
    eng._session = _Sess(fail_reason)
    eng._events = _Ev()
    eng._cfg = _Cfg()
    eng._reconnect_attempt = 0
    eng._session_started_at = time.monotonic() - started_ago if started_ago else 0.0
    calls = {"n": 0}

    async def _reconnect() -> None:
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise RuntimeError("connect refused")

    eng._reconnect_session = _reconnect                    # type: ignore[method-assign]
    return eng


async def _run(eng: Engine, reason: str) -> None:
    await eng._reconnect_loop(reason)


def _drive(eng: Engine, reason: str) -> None:
    WAITS.clear()
    real = asyncio.sleep

    async def fake_sleep(sec: float) -> None:               # 只记录，不真睡
        WAITS.append(float(sec))
        await real(0)

    asyncio.sleep = fake_sleep
    try:
        asyncio.run(_run(eng, reason))
    finally:
        asyncio.sleep = real


def main() -> int:
    # ---- 打表：限流识别 ----
    check("1007 rate limit → 限流", _is_rate_limited(
        "ConnectionClosedError: received 1007 (invalid frame payload data) "
        "Requests rate limit exceeded, please try again later."))
    check("HTTP 429 → 限流", _is_rate_limited("HTTP 429 Too Many Requests"))
    check("普通的接收循环结束 → 不是限流", not _is_rate_limited("接收循环已结束"))
    check("repeat 抑制 → 不是限流", not _is_rate_limited("本地 repeat 抑制：连续 3 条最终译文雷同"))

    # ---- A) 限流：30 → 60，且提示明说 ----
    e = _engine(fail_reason="ConnectionClosedError: received 1007 ... rate limit exceeded")
    _drive(e, "接收循环已结束")
    check(f"限流时走 30/60 台阶（实测 {WAITS}）", WAITS == [30.0, 60.0])
    check("状态栏明说「服务端限流」（禁静默）",
          any("服务端限流" in m for _, m in e._events.msgs))

    # ---- C) 台阶跨会话累计：再死一次不从 30 重来（应 120 = 封顶） ----
    e2 = _engine(fail_reason="1007 rate limit exceeded")
    e2._reconnect_attempt = 3                                # 模拟前面已经撞过几次
    _drive(e2, "接收循环已结束")
    check(f"台阶不重来（继续往上封顶 120，实测 {WAITS}）", WAITS == [120.0, 120.0])

    # ---- C') 会话活得够久 → 台阶归零 ----
    e3 = _engine(fail_reason="1007 rate limit exceeded", started_ago=61.0)
    e3._reconnect_attempt = 5
    _drive(e3, "接收循环已结束")
    check(f"会话活得 ≥60s → 台阶归零（实测 {WAITS}）", WAITS == [30.0, 60.0])

    # ---- B) 普通断开：保持原台阶 ----
    e4 = _engine(fail_reason="接收循环已结束")
    _drive(e4, "接收循环已结束")
    check(f"普通断开仍是 2/5 台阶（实测 {WAITS}）", WAITS == [2.0, 5.0])
    check("普通断开的提示措辞没变（含「后重连」）",
          any("后重连" in m for _, m in e4._events.msgs)
          and not any("服务端限流" in m for _, m in e4._events.msgs))

    bad = [n for n, ok in results if not ok]
    print(f"\n{'ALL PASSED' if not bad else '失败：' + ', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
