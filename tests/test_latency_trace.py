"""延迟埋点与线程优先级助手（2026-10-07 工程化优化）。

覆盖：
  1. `latency.ms/fmt` 纯函数；
  2. 默认**零输出**（不打开追踪时 `log()` 什么都不打）；
  3. `VLT_LATENCY_TRACE=1` 时确实打一行（子进程里开环境变量验证）；
  4. `bump_thread_priority()` 任何情况下都不抛异常、返回 bool；
  5. `_VoiceSlot` 的时间线字段初值（只有 t_created 有值，其余 None）。
"""
from __future__ import annotations

import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlt import latency                                    # noqa: E402
from vlt.platform import bump_thread_priority              # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    results.append((name, ok))
    print(f"  {name}  {'OK' if ok else '✗'}")


def main() -> int:
    # ① 纯函数
    check("ms(None, x) 返回 '-'", latency.ms(None, 1.0) == "-")
    check("ms 计算正确", latency.ms(1.0, 1.25) == "250ms")
    check("fmt 形状", latency.fmt("段", {"排队": "12ms"}) == "[lat] 段 排队=12ms")

    # ② 默认零输出（本进程没设 VLT_LATENCY_TRACE）
    if latency.TRACE:
        print("  ⚠️ 本进程设了 VLT_LATENCY_TRACE，跳过零输出检查")
    else:
        buf = io.StringIO()
        with redirect_stdout(buf):
            latency.log("段", 排队="1ms")
        check("未开启时不输出任何东西", buf.getvalue() == "")

    # ③ 打开时打一行（子进程，避免污染本进程）
    r = subprocess.run([sys.executable, "-c",
                        "import os;os.environ['VLT_LATENCY_TRACE']='1';"
                        "from vlt import latency;latency.log('段#1', 排队='12ms')"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    check("开启后打出一行 [lat]",
          r.returncode == 0 and "[lat] 段#1 排队=12ms" in r.stdout)

    # ④ 优先级助手：不抛异常、返回 bool
    ok = True
    try:
        for lvl in ("idle", "below", "normal", "above", "highest", "不存在的档"):
            v = bump_thread_priority(lvl)
            ok = ok and isinstance(v, bool)
    except Exception as exc:                               # noqa: BLE001
        ok = False
        print(f"    抛异常了：{type(exc).__name__}: {exc}")
    check("bump_thread_priority 六种入参都不抛、返回 bool", ok)

    # ⑤ _VoiceSlot 时间线初值
    from vlt.engine import _VoiceSlot                      # noqa: E402
    slot = _VoiceSlot("你好")
    check("slot.t_created 有值", isinstance(slot.t_created, float))
    check("其余时间线初值为 None",
          all(getattr(slot, k) is None for k in
              ("t_synth", "t_connect", "t_first", "t_synth_done", "t_write", "t_write_done")))
    check("sent 初值 0", slot.sent == 0)

    bad = [n for n, ok_ in results if not ok_]
    print(f"\n{'ALL PASSED' if not bad else '失败：' + ', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
