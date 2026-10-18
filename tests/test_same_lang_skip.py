"""源语言 == 译入语言 → 这条腿不翻译（语音腿）。

背景：打字腿早就有同语种直通（`_async_send_text`：源=目标时 MT 原样返回）；
语音腿没有 —— `_session_payload()` 无条件下发 `translation.language = target`，而服务端没有
「不翻」的表达（缺字段整体拒绝），同语言发下去 = 让模型原样回吐：白花一次连接/token，
还会招来 repeat 病态（连续雷同译文 → 掐线重连）。

本用例守：`_build_and_run` 在「源==目标」时**不建会话**、明说原因；不同语言时照常往下走。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlt.config import Direction                                  # noqa: E402
from vlt.engine import Engine                                      # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    results.append((name, ok))
    print(f"  {name}  {'OK' if ok else '✗'}")


class _Ev:
    def __init__(self) -> None:
        self.msgs: list[tuple[str, str]] = []

    def on_status(self, level: str, msg: str, **kw) -> None:       # noqa: ANN003, ARG002
        self.msgs.append((level, msg))


class _Cfg:
    def __init__(self, d: Direction) -> None:
        self.directions = {"mine": d}


class _Sentinel(Exception):
    pass


def _engine(d: Direction) -> Engine:
    eng = Engine.__new__(Engine)
    eng._reconnect_attempt = 3                 # 顺便验证：跳过时不该动它
    eng._direction = "mine"
    eng._cfg = _Cfg(d)
    eng._events = _Ev()
    eng._session = None
    hits = {"session_cfg": 0, "create": 0}

    def _session_cfg():                        # noqa: ANN202
        hits["session_cfg"] += 1
        raise _Sentinel("走到 _session_cfg = 闸没拦住")

    async def _create_session(scfg):           # noqa: ANN001, ANN202, ARG001
        hits["create"] += 1
        raise _Sentinel("不该走到这里（只在 _session_cfg 里就应被拦住）")

    eng._session_cfg = _session_cfg             # type: ignore[method-assign]
    eng._create_session = _create_session       # type: ignore[method-assign]
    return eng


def main() -> int:
    # A) 源 == 目标 → 直接返回，不建会话，状态栏明说
    eng = _engine(Direction(source_lang="ru", target_lang="ru"))
    asyncio.run(eng._build_and_run())
    msgs = " | ".join(m for _, m in eng._events.msgs)
    check("同语言：没有建会话", eng._session is None)
    check("同语言：状态栏明说「不翻译」", "不翻译" in msgs)
    check("同语言：提示级别是 info（不是 warn/error）",
          all(lv == "info" for lv, _ in eng._events.msgs))

    # B) 语言不同 → 闸不拦（继续往下走：会走到建会话那一步）
    eng2 = _engine(Direction(source_lang="ru", target_lang="zh"))
    raised = False
    try:
        asyncio.run(eng2._build_and_run())
    except _Sentinel:
        raised = True
    except Exception:                          # noqa: BLE001  （别的异常也算「没被闸拦住」但要看住）
        raised = False
    check("不同语言：照常往下走（闸没拦）", raised)
    check("不同语言：没有「不翻译」的提示",
          not any("不翻译" in m for _, m in eng2._events.msgs))

    # C) 源语言为空（未指定）→ 不能误判成「相同」
    eng3 = _engine(Direction(source_lang="", target_lang="ru"))
    raised3 = False
    try:
        asyncio.run(eng3._build_and_run())
    except _Sentinel:
        raised3 = True
    except Exception:                          # noqa: BLE001
        raised3 = False
    check("源语言未指定：不误判（照常往下走）", raised3)

    bad = [n for n, ok in results if not ok]
    print(f"\n{'ALL PASSED' if not bad else '失败：' + ', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
