"""延迟埋点（默认关闭；`VLT_LATENCY_TRACE=1` 打开）。

为什么单开一个模块：TTS 请求→连接→首包、分段排队、写卡这几段的耗时要能**一起看**，
才知道该优化哪一段 —— 首包 0.36~0.42s 里有多少是连接握手、多少是服务端，只能靠埋点分出来。

约束（与 `tts._note` 同一约定）：
  · 默认零开销、零输出（不打开就是一个 `if`）；
  · 绝不抛异常 —— 埋点不能把主流程搞挂。
"""
from __future__ import annotations

import os
import time
from typing import Any

TRACE = os.environ.get("VLT_LATENCY_TRACE", "").strip().lower() in ("1", "true", "yes", "on")


def now() -> float:
    """单调时钟（埋点专用）。"""
    return time.monotonic()


def ms(a: float | None, b: float | None) -> str:
    """两个时间点之间的毫秒；任一为空给 `-`。纯函数，离线可测。"""
    if a is None or b is None:
        return "-"
    return f"{(b - a) * 1000:.0f}ms"


def fmt(label: str, stages: dict[str, Any]) -> str:
    """把若干阶段拼成一行。纯函数，离线可测。"""
    return f"[lat] {label} " + " ".join(f"{k}={v}" for k, v in stages.items())


def log(label: str, **stages: Any) -> None:
    """打开追踪时打一行；任何异常都吞掉。"""
    if not TRACE:
        return
    try:
        print(fmt(label, stages), flush=True)
    except Exception:                                    # noqa: BLE001
        pass


def flag_on() -> bool:
    """调用点可用来跳过昂贵的取时间/构造 dict。"""
    return TRACE
