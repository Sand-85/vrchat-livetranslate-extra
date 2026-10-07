"""真机验收（应用级）：**真起界面**确认麦克风代理在启动时就起来了，且原声直通真的过线。

## 它证明什么

`tests/test_proxy_wiring.py` 是 headless + 打桩，能证明「接线对了」，但证明不了
「程序一起来，代理真的开了虚拟声卡、声音真的流出去」。本脚本补这一环：

  真 `TranslationGUI()`（**非 headless**，真建 Tk 窗口）
      → `__init__` 里的 `_start_proxy()` 真的跑
      → 断言日志出现「麦克风代理已启动 / 虚拟声卡已打开 / 麦克风直通已启动」
      → 并用**假麦克风（1kHz）**从虚拟声卡**录音端**抓回来，抓到 1kHz 才算过

麦克风那一路被换成假源（与 `verify_proxy_loopback.py` 同一套），所以**不需要真麦克风**，
也不需要 VRChat / 头显；但需要**真实桌面会话**（要开 Tk 窗口）与**已装虚拟声卡**。

## 环境前提

- Windows + 真实桌面会话 + 已装虚拟声卡（VB-CABLE / VoiceMeeter）；
- 缺任一项时几秒内明确报错退出（不 hang、不静默跳过）；
- 配置指向**临时沙箱**，绝不碰真实 `config.yaml`；不写任何文件。

跑法（仓库根目录）：

    ./.venv/Scripts/python.exe scripts/verify/verify_proxy_app_start.py
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from verify_proxy_loopback import (          # noqa: E402  同目录助手：假麦克风 + 抓取/分析
    DEFAULT_IN_PATTERNS,
    DEFAULT_OUT_PATTERNS,
    TONE_MIC_HZ,
    TOLERANCE_HZ,
    FakeCaptureBackend,
    FakeMicSource,
    analyse,
    capture,
    find_wasapi_device,
)

CFG_TEMPLATE = (
    "ui:\n  lang: zh\n"
    "session:\n  api_key: test-key-not-real\n"
    "output:\n"
    "  capture:\n    mic_device: ''\n"
    "  audio:\n"
    "    enabled: true\n"
    "    device: [{patterns}]\n"
    "    device_name: ''\n"
    "    sample_rate: 48000\n"
    "    buffer_ms: 300\n"
    "    max_buffer_ms: 2000\n"
    "    proxy:\n      enabled: true\n      passthrough_buffer_ms: 150\n"
)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="真起界面验证麦克风代理的启动路径与直通")
    ap.add_argument("--out-pattern", action="append", default=None)
    ap.add_argument("--in-pattern", action="append", default=None)
    args = ap.parse_args()
    out_pat = tuple(args.out_pattern) if args.out_pattern else DEFAULT_OUT_PATTERNS
    in_pat = tuple(args.in_pattern) if args.in_pattern else DEFAULT_IN_PATTERNS

    in_dev, in_name = find_wasapi_device(in_pat, want_input=True)
    print(f"录音端（这里抓回来）：#{in_dev} {in_name}")

    tmp = Path(tempfile.mkdtemp(prefix="vlt-proxy-app-verify-"))
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(
        CFG_TEMPLATE.format(patterns=", ".join(f"'{p}'" for p in out_pat)),
        encoding="utf-8", newline="\n")

    import vlt.config as cfg_mod
    import vlt.gui as gui_mod
    import vlt.gui_engine as ge
    from vlt import platform

    cfg_mod.DEFAULT_CONFIG = cfg_path
    gui_mod.DEFAULT_CONFIG = cfg_path

    src = FakeMicSource(TONE_MIC_HZ)
    saved_backend = platform.capture_backend
    platform.capture_backend = lambda: FakeCaptureBackend(src)      # type: ignore[assignment]

    from vlt.gui import TranslationGUI

    gui = None
    ok = False
    try:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                gui = TranslationGUI()               # 真窗口 → 走 __init__ 里的 _start_proxy()
                time.sleep(1.2)
        except Exception as exc:                     # noqa: BLE001
            print(f"[X] 起界面就失败了（有没有真实桌面会话？）：{type(exc).__name__}: {exc}")
            return 2
        out = buf.getvalue()
        lines = [ln for ln in out.splitlines() if ln.startswith("[proxy]")]
        print("=== 启动时打出的 [proxy] 行 ===")
        for ln in lines:
            print("   ", ln[:160])
        gui._sync_engine_ctx()
        print("=== 接线状态 ===")
        print("   gui._proxy     ：", type(gui._proxy).__name__ if gui._proxy else None)
        print("   _proxy_of(ctx) ：", type(ge._proxy_of(gui._engine_ctx)).__name__)

        peak, db = analyse(capture(0.5, in_dev), "从录音端真抓回来")
        ok = (gui._proxy is not None
              and ge._proxy_of(gui._engine_ctx) is not None
              and any("麦克风代理已启动" in ln for ln in lines)
              and any("虚拟声卡已打开" in ln for ln in lines)
              and any("麦克风直通已启动" in ln for ln in lines)
              and abs(peak - TONE_MIC_HZ) <= TOLERANCE_HZ)
        print(f"\n  期望抓到 {TONE_MIC_HZ:.0f}Hz ± {TOLERANCE_HZ:.0f}Hz，"
              f"实测 {peak:.1f} Hz（{db:.1f} dBFS）")
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            if gui is not None:
                try:
                    gui._on_close()
                except Exception as exc:             # noqa: BLE001
                    print("close 出错：", exc)
        platform.capture_backend = saved_backend      # type: ignore[assignment]

    print("  假麦克风源已释放：", src.closed)
    print("\n结论：", "✅ 应用级：启动即起代理 + 原声直通真的过线" if ok else "❌ 不符预期")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
