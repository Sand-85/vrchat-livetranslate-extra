#!/usr/bin/env python
"""设备枚举失败的日志节流：**同一理由只留一行、恢复了补一行**（issue #26 第 3 条）。

## 治什么

`pick_loopback_target()` 的枚举失败分支原来**无条件**每次打一行
`[loopback] ❌ 枚举设备失败：…`。这个函数同时被两个重试循环调用：

- 电平探针的低频自愈（`level_probe.RETRY_S = 5.0`，见 `#26-1/#26-2`）—— 失败期间
  就是 **12 行/分钟**；
- 引擎 loopback 腿的重开路径。

探针自己那条 `[level] ❌ …` 已经有「理由没变就不重复打」的节流，而它每次重试都会先经过
`pick_loopback_target` → 于是「不刷屏」只兑现了一半。这里按**同一取舍**（与
`level_probe._enter_waiting` / `_note_open` 一致）把 `[loopback]` 这条也节流：
理由没变不重复打，从失败里恢复时补一行 ✅ —— 留痕不静默，也不淹掉日志。

## 离线

后端是假的（`vlt.platform.device_backend` 换成桩），不枚举真实设备、不碰音频栈。
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import vlt.engine as engine  # noqa: E402
from vlt import platform as vlt_platform  # noqa: E402


class _FakeBackend:
    """枚举设备的后端桩：想让它抛就抛、想让它空就空。"""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.devices: list[dict] = []

    def query_loopback_devices(self) -> list[dict]:
        if self.error is not None:
            raise self.error
        return list(self.devices)


def _call(backend: _FakeBackend) -> tuple[object, str]:
    """在被替换的后端下调用一次 `pick_loopback_target`，返回 (结果, 标准输出)。"""
    real = vlt_platform.device_backend
    vlt_platform.device_backend = lambda: backend          # type: ignore[assignment]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            out = engine.pick_loopback_target()
    finally:
        vlt_platform.device_backend = real                 # type: ignore[assignment]
    return out, buf.getvalue()


def _reset() -> None:
    """清掉模块级节流状态，让用例与执行顺序无关。"""
    engine._last_enum_err = None                            # noqa: SLF001


def test_same_reason_prints_once() -> None:
    _reset()
    backend = _FakeBackend()
    backend.error = RuntimeError("device enumeration blew up")
    lines: list[str] = []
    for _ in range(5):
        out, printed = _call(backend)
        assert out is None, "枚举失败时应返回 None"
        lines.extend(ln for ln in printed.splitlines() if "枚举设备失败" in ln)
    assert len(lines) == 1, f"同一失败理由打了 {len(lines)} 行（应只留一行）：{lines}"

    # 理由变了 → 立刻再打一行（换错误类型）
    backend.error = OSError("another reason")
    _, printed = _call(backend)
    assert "another reason" in printed, f"换了失败理由却没打：{printed!r}"
    print("  同一理由 5 次调用只留 1 行；理由变化立刻补一行 OK")


def test_recovery_prints_once() -> None:
    _reset()
    backend = _FakeBackend()
    backend.error = RuntimeError("boom")
    _, printed = _call(backend)
    assert "枚举设备失败" in printed

    backend.error = None                                   # 恢复（枚举成功，但没有设备）
    _, printed = _call(backend)
    assert "✅" in printed and "枚举" in printed, f"恢复没有补一行：{printed!r}"

    # 再调用：状态已清，不该再重复打恢复行
    _, printed = _call(backend)
    assert printed.strip() == "", f"恢复行重复打了：{printed!r}"

    # 恢复之后又失败 → 仍然要留痕（不是「一辈子只打一次」）
    backend.error = RuntimeError("boom again")
    _, printed = _call(backend)
    assert "枚举设备失败" in printed, "恢复后再失败必须重新留痕"
    print("  恢复补一行 ✅、不重复、之后再次失败仍留痕 OK")


def test_scan_callsite_still_logs() -> None:
    """静态扫描：失败分支仍然走留痕函数（别在后续重构里被静默掉）。"""
    src = (ROOT / "vlt" / "engine.py").read_text(encoding="utf-8")
    seg = src.split("loops = backend.query_loopback_devices()", 1)[1][:400]
    assert "_note_loopback_enum_error" in seg, \
        "枚举失败分支不再调用节流留痕函数（降级不许静默）"
    print("  失败分支仍调用节流留痕函数 OK")


if __name__ == "__main__":
    print("test_loopback_log_throttle:")
    test_same_reason_prints_once()
    test_recovery_prints_once()
    test_scan_callsite_still_logs()
    print("ALL PASSED")
