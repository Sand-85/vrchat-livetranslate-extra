"""真机验收（组件级）：麦克风代理 → 虚拟声卡 → 抓回录音端，判「原声/译音」档位路由。

## 它证明什么

`tests/test_micproxy.py` 全是打桩（假 sounddevice、假采集后端），**证明不了声音真的过线**。
本脚本用真虚拟声卡（VB-CABLE / VoiceMeeter）跑一遍：

    假麦克风源（1kHz 正弦）→ MicProxy 直通环形缓冲 → 虚拟声卡输出端（WASAPI）
                                                      ↓（线缆内部回环）
                       从虚拟声卡**录音端**抓回来 → 主频判档位

  · 原声档：应抓到 **1kHz**（= 麦克风直通真的通了）
  · 译音档：推 500Hz 进 `translated_sink` → 应抓到 **500Hz**（= 档位路由生效）
  · 切回原声档：又变回 **1kHz**

判据用**主频（±30Hz）**，不逐字节比：中间过了环形缓冲 / 重采样 / PortAudio 分块，
逐字节相等不成立，但「1kHz 还是 500Hz」是确定性的。

## 环境前提

- Windows + **已装虚拟声卡**（VB-CABLE 的 `CABLE Input`/`CABLE Output`，
  或 VoiceMeeter 的 `VoiceMeeter Input`/`VoiceMeeter Output`）；
- 不需要麦克风、不需要头显、不需要 VRChat：麦克风那一路被换成假源；
- 缺虚拟声卡时**几秒内明确报错退出**（退出码 2），不 hang；
- 不写任何文件、不改 `config.yaml`。

跑法（仓库根目录）：

    ./.venv/Scripts/python.exe scripts/verify/verify_proxy_loopback.py
    ./.venv/Scripts/python.exe scripts/verify/verify_proxy_loopback.py --out-pattern "voicemeeter input"
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

#: 输出端（程序往里推）的默认关键词：VB-CABLE → CABLE Input；VoiceMeeter → VoiceMeeter Input
DEFAULT_OUT_PATTERNS = ("cable input", "voicemeeter input", "vb-audio")
#: 录音端（我们从这里抓）的默认关键词
DEFAULT_IN_PATTERNS = ("cable output", "voicemeeter output", "vb-audio")

# 假正弦频率、主频容差、假麦克风源、FFT 判据 —— 全部与 Linux 版共用（`_proxy_probe.py`）。
# 判据抄两份必然漂移，而「看着绿、其实没测到」正是这类探针最要命的失败模式。
from _proxy_probe import (          # noqa: E402  同目录下的共用件
    TOLERANCE_HZ as TOLERANCE_HZ,   # ↖ 显式重导出给 verify_proxy_app_start.py 用（ruff 认这个写法）
    TONE_MIC_HZ,
    TONE_TRANSLATED_HZ,
    FakeCaptureBackend,
    FakeMicSource,
    analyse,
    report,
)


# ---------------------------------------------------------------- 设备 / 抓取 / 分析

def find_wasapi_device(patterns, *, want_input: bool) -> tuple[int, str]:
    """按关键词 + WASAPI（hostapi==2）找设备；找不到就**立刻**报错（不静默跳过）。

    ⚠️ 关键词只取名字的一部分并小写比较：VB-CABLE / VoiceMeeter 装完会在 4 套 host API
    下各列一份（MME/DirectSound 是 44100），必须钉住 WASAPI 那一份。
    """
    import sounddevice as sd

    wants = [p.lower() for p in patterns]
    for i, d in enumerate(sd.query_devices()):
        name = str(d["name"])
        chan = d["max_input_channels"] if want_input else d["max_output_channels"]
        if chan > 0 and d["hostapi"] == 2 and any(p in name.lower() for p in wants):
            return i, name
    kind = "录音端" if want_input else "输出端"
    print(
        f"[X] 找不到 WASAPI 的虚拟声卡{kind}（关键词 {list(patterns)}）。\n"
        "    先确认装了虚拟声卡（VB-CABLE / VoiceMeeter），或显式指定：\n"
        "      --out-pattern / --in-pattern \"<设备名里的片段>\"\n"
        "    本机现有设备可用 `--list` 查看。", flush=True)
    raise SystemExit(2)          # 2 = 环境不满足（不是判据失败）


def list_devices() -> None:
    import sounddevice as sd

    print("本机设备（hostapi==2 即 WASAPI）：")
    for i, d in enumerate(sd.query_devices()):
        print(f"  {i:3d} hostapi={d['hostapi']} in={d['max_input_channels']:2d} "
              f"out={d['max_output_channels']:2d} {d['name']}")


def capture(seconds: float, dev: int, samplerate: int = 48000):
    """从指定设备抓一段双声道 int16，返回 numpy (N, 2)。"""
    import numpy as np
    import sounddevice as sd

    frames = int(samplerate * seconds)
    stream = sd.RawInputStream(device=dev, samplerate=samplerate, channels=2,
                               dtype="int16", blocksize=2400)
    got = bytearray()
    stream.start()
    try:
        while len(got) < frames * 4:
            data, _overflowed = stream.read(2400)
            if data:
                got += bytes(data)
    finally:
        stream.stop()
        stream.close()
    return np.frombuffer(bytes(got), dtype="<i2").reshape(-1, 2)


def main() -> int:
    ap = argparse.ArgumentParser(description="麦克风代理 + 虚拟声卡的真机回环验收")
    ap.add_argument("--out-pattern", action="append", default=None,
                    help="输出端（程序往里推）关键词，可多次；默认 cable input / voicemeeter input / vb-audio")
    ap.add_argument("--in-pattern", action="append", default=None,
                    help="录音端（本脚本从这里抓）关键词，可多次；默认 cable output / voicemeeter output / vb-audio")
    ap.add_argument("--list", action="store_true", help="只列设备后退出")
    args = ap.parse_args()
    if args.list:
        list_devices()
        return 0

    out_pat = tuple(args.out_pattern) if args.out_pattern else DEFAULT_OUT_PATTERNS
    in_pat = tuple(args.in_pattern) if args.in_pattern else DEFAULT_IN_PATTERNS
    out_dev, out_name = find_wasapi_device(out_pat, want_input=False)
    in_dev, in_name = find_wasapi_device(in_pat, want_input=True)
    print(f"输出端（程序往里推）：#{out_dev} {out_name}")
    print(f"录音端（这里抓回来）：#{in_dev} {in_name}")

    from vlt import platform
    from vlt.output.micproxy import MODE_PASSTHROUGH, MODE_TRANSLATED, MicProxy

    src = FakeMicSource(TONE_MIC_HZ)
    saved_backend = platform.capture_backend
    platform.capture_backend = lambda: FakeCaptureBackend(src)      # type: ignore[assignment]

    cfg = {"device": list(out_pat), "sample_rate": 48000,
           "buffer_ms": 300, "max_buffer_ms": 2000,
           "proxy": {"enabled": True, "passthrough_buffer_ms": 150}}
    proxy = MicProxy(audio_cfg=cfg, mic_name=None,
                     on_status=lambda lvl, msg, **kw: print(
                         f"    {lvl}: {msg.format(**kw) if kw else msg}"))
    results: dict[str, float] = {}
    stop_push = threading.Event()
    push_thread = None
    try:
        if not proxy.start():
            print("[X] 代理没起来（虚拟声卡打不开？）—— 见上面的 [proxy] 日志")
            return 2
        print(f"\n代理已启动，当前档位 {proxy.mode}")
        time.sleep(0.6)                                   # 让流与直通线程跑起来

        print(f"\n[1] 原声档（假麦克风 = {TONE_MIC_HZ:.0f}Hz 直通）")
        results["原声"] = analyse(capture(0.5, in_dev), "抓到")[0]

        print(f"\n[2] 译音档（推 {TONE_TRANSLATED_HZ:.0f}Hz）")
        proxy.set_translation_active(True)
        if not proxy.set_mode(MODE_TRANSLATED):
            print("[X] 切译音档被拒（翻译没标记为运行中？）")
            return 2

        def _pusher() -> None:
            blk = b"".join(
                struct.pack("<hh",
                            int(0.3 * 32767 * math.sin(2 * math.pi * TONE_TRANSLATED_HZ * i / 48000)),
                            int(0.3 * 32767 * math.sin(2 * math.pi * TONE_TRANSLATED_HZ * i / 48000)))
                for i in range(4800))                      # 100ms 立体声
            while not stop_push.is_set():
                proxy.translated_sink.push(blk)
                time.sleep(0.05)

        push_thread = threading.Thread(target=_pusher, daemon=True)
        push_thread.start()
        time.sleep(0.5)
        results["译音"] = analyse(capture(0.5, in_dev), "抓到")[0]
        stop_push.set()
        push_thread.join(timeout=1.0)

        print("\n[3] 切回原声档")
        proxy.set_mode(MODE_PASSTHROUGH)
        time.sleep(0.4)
        results["回原声"] = analyse(capture(0.5, in_dev), "抓到")[0]
    finally:
        stop_push.set()
        proxy.close()
        platform.capture_backend = saved_backend          # type: ignore[assignment]

    all_ok = report([("原声档应为 1kHz", results.get("原声", 0.0), TONE_MIC_HZ),
                     ("译音档应为 500Hz", results.get("译音", 0.0), TONE_TRANSLATED_HZ),
                     ("切回原声应为 1kHz", results.get("回原声", 0.0), TONE_MIC_HZ)])
    print("  假麦克风源已释放：", src.closed)
    print("\n结论：", "✅ 原声直通 + 档位路由在真虚拟声卡上成立" if all_ok else "❌ 有判据未通过")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
