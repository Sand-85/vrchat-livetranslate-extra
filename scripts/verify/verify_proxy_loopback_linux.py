"""真机验收（组件级，Linux）：麦克风代理 → 虚拟麦（VLT Mic）→ 抓回来，判「原声/译音」档位路由。

## 它证明什么

`tests/test_micproxy_linux.py` 全是打桩（假 pw-cat、假麦克风），**证明不了声音真的过线**。
本脚本用**真实的 PipeWire** 跑一遍：

    假麦克风源（1kHz）→ LinuxMicProxy 直通环形缓冲 → pw-cat → vlt_mic_sink
                                                        ↓（pw-loopback 内部回环）
                   从 vlt_mic_source（**VRChat 会读的那一头**）用 pw-record 抓回来 → 判主频

  · 原声档：应抓到 **1kHz**（= 麦克风直通真的通了）
  · 译音档：推 500Hz 进 `translated_sink` → 应抓到 **500Hz**（= 档位路由生效）
  · 切回原声档：又变回 **1kHz**

判据与 Windows 版**共用**（`_proxy_probe.py`）：主频 ±30Hz，不做逐字节比对
（中间过了环形缓冲 / 重采样 / 分块，但「1kHz 还是 500Hz」是确定性的）。

## 与 Windows 版的差别

Windows 版（`verify_proxy_loopback.py`）要用户**自备**虚拟声卡（VB-CABLE / VoiceMeeter），
还得按 host API 挑 WASAPI 那份端点。Linux **什么都不用装** —— 虚拟麦是程序运行时用
`pw-loopback` 声明出来的（见 `vlt/platform/linux.py`）。所以本脚本顺手把 Linux 特有的
两条硬约束也验了（两条都出过实测事故，依据见 `docs/平台约束记录.md`）：

  · **不抢默认输出**：声明虚拟麦前后 `default.audio.sink` 必须**不变**（第一节那次
    「用户系统声音突然没了」就是这个坑）；
  · **零残留**：退出后 `vlt_mic_sink` / `vlt_mic_source` 必须都消失 —— 否则 `pw-loopback`
    成了孤儿进程，节点会永久留在用户的音频图里。

## 环境前提

- **PipeWire 原生工具**：`pw-dump` / `pw-record` / `pw-cat`（缺了**几秒内**退出码 2，不 hang）；
- **不需要**虚拟声卡、麦克风、VRChat、头显、桌面会话（本脚本不开 Tk 窗口）；
- 会在 PipeWire 里**短暂**声明一对节点（就是产品运行时的行为），退出前一定回收；
- 不写任何文件、不改 `config.yaml`。

## 跑法（仓库根目录）

```
./.venv/bin/python scripts/verify/verify_proxy_loopback_linux.py
./.venv/bin/python scripts/verify/verify_proxy_loopback_linux.py --list
```

退出码：**0** = 判据全过；**1** = 有判据没过；**2** = 环境不满足（缺 PipeWire 工具 / 代理起不来）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]        # scripts/verify/ -> 仓库根
sys.path.insert(0, str(ROOT))

if not sys.platform.startswith("linux"):
    print("❌ 本脚本需要 Linux（PipeWire 原生工具 + 运行时声明的虚拟麦）", file=sys.stderr)
    raise SystemExit(2)

from vlt import platform                          # noqa: E402
from vlt.output.micproxy import (                 # noqa: E402
    MODE_PASSTHROUGH,
    MODE_TRANSLATED,
)
from vlt.platform import linux as L               # noqa: E402

from _proxy_probe import (                        # noqa: E402  同目录下的共用件
    TONE_MIC_HZ,
    TONE_TRANSLATED_HZ,
    FakeCaptureBackend,
    FakeMicSource,
    analyse,
    report,
    tone_48k_stereo,
)

RATE, CH = 48000, 2
SOURCE_NODE = "vlt_mic_source"
SINK_NODE = "vlt_mic_sink"


# ---------------------------------------------------------------- PipeWire 读取

def list_nodes() -> None:
    """列出 PipeWire 里的音频节点（排查用：看看虚拟麦在不在、类对不对）。"""
    import json

    dump = json.loads(subprocess.run(["pw-dump"], capture_output=True).stdout or b"[]")
    print("PipeWire 音频节点：")
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        cls = str(props.get("media.class") or "")
        if not cls.startswith(("Audio/", "Stream/")):
            continue
        print(f"  id={obj.get('id'):>4} {cls:<24} "
              f"name={props.get('node.name')!r} desc={props.get('node.description')!r}")


def capture(seconds: float):
    """从**虚拟麦**（VRChat 读的那一头）抓一段双声道 int16，返回 numpy (N, 2)。

    ⚠️ `--raw` 不能省：不给它 pw-record 会用 libsndfile 写容器格式，头 4 字节是 "dns."，
    当裸 PCM 解析出来的是垃圾 —— 而且 stderr 干净、不报错（`vlt/platform/linux.py` 记过这个坑）。
    """
    import numpy as np

    want = int(RATE * seconds) * CH * 2
    proc = subprocess.Popen(
        ["pw-record", "--raw", f"--target={SOURCE_NODE}",
         "--format=s16", f"--rate={RATE}", f"--channels={CH}", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    got = b""
    deadline = time.monotonic() + seconds + 4
    try:
        while len(got) < want and time.monotonic() < deadline:
            chunk = proc.stdout.read(want - len(got))
            if not chunk:
                break
            got += chunk
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    return np.frombuffer(got[: (len(got) // 4) * 4], dtype="<i2").reshape(-1, 2)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="麦克风代理 + 虚拟麦的真机回环验收（Linux）")
    ap.add_argument("--list", action="store_true", help="只列 PipeWire 音频节点后退出")
    args = ap.parse_args()

    if args.list:
        list_nodes()
        return 0

    missing = L.pw_tools_missing()
    if missing:
        print(f"[X] 缺 PipeWire 原生工具：{', '.join(missing)}\n"
              "    装 pipewire 包（pw-dump / pw-record / pw-cat 都在里面）。", flush=True)
        return 2

    before_default = L.default_output_index()
    print(f"声明虚拟麦之前的默认输出：节点 id={before_default}")
    print(f"虚拟麦起点是干净的：sink={bool(L.node_props_by_name(SINK_NODE))} "
          f"source={bool(L.node_props_by_name(SOURCE_NODE))}")

    src = FakeMicSource(TONE_MIC_HZ)
    saved_backend = platform.capture_backend
    platform.capture_backend = lambda: FakeCaptureBackend(src)      # type: ignore[assignment]

    cfg = {"sample_rate": RATE, "buffer_ms": 300, "max_buffer_ms": 2000,
           "proxy": {"enabled": True, "passthrough_buffer_ms": 150}}
    proxy = None
    results: dict[str, float] = {}
    stop_push = threading.Event()
    push_thread = None
    rc = 0
    try:
        # ★ 走**真门面**（不是直接 new LinuxMicProxy）：这正是界面启动时走的那条路
        proxy = platform.create_mic_proxy(cfg, None,
                                         lambda lvl, msg, **kw: print(
                                             f"    [{lvl}] {msg.format(**kw) if kw else msg}", flush=True))
        if not proxy.start():
            print("[X] 代理没起来（见上面的 [proxy] 日志）", flush=True)
            return 2
        print(f"代理已启动（{type(proxy).__name__}），当前档位 {proxy.mode}")

        L.clear_cache()
        sink_cls = L.node_props_by_name(SINK_NODE).get("media.class")
        print(f"虚拟麦已声明：{SINK_NODE} media.class={sink_cls!r} / {SOURCE_NODE} 就绪")
        if sink_cls != "Audio/Sink/Internal":
            print(f"[X] 可写入端的类不对（{sink_cls!r}）—— 必须 Audio/Sink/Internal，"
                  "否则会抢用户默认输出（见 docs/平台约束记录.md 第一节）", flush=True)
            return 1
        time.sleep(0.6)                                   # 让流与直通线程跑起来

        print(f"\n[1] 原声档（假麦克风 = {TONE_MIC_HZ:.0f}Hz 直通）")
        results["原声"] = analyse(capture(0.6), "抓到")[0]

        print(f"\n[2] 译音档（推 {TONE_TRANSLATED_HZ:.0f}Hz）")
        proxy.set_translation_active(True)
        if not proxy.set_mode(MODE_TRANSLATED):
            print("[X] 切译音档被拒（翻译没标记为运行中？）", flush=True)
            return 2
        blk = tone_48k_stereo(TONE_TRANSLATED_HZ, 100)

        def _pusher() -> None:
            while not stop_push.is_set():
                proxy.translated_sink.push(blk)
                time.sleep(0.05)

        push_thread = threading.Thread(target=_pusher, daemon=True)
        push_thread.start()
        time.sleep(0.5)
        results["译音"] = analyse(capture(0.6), "抓到")[0]
        stop_push.set()
        push_thread.join(timeout=1.0)

        print("\n[3] 切回原声档")
        proxy.set_mode(MODE_PASSTHROUGH)
        time.sleep(0.4)
        results["回原声"] = analyse(capture(0.6), "抓到")[0]

    finally:
        stop_push.set()
        if push_thread is not None:
            push_thread.join(timeout=1.0)
        if proxy is not None:
            proxy.close()
        platform.capture_backend = saved_backend          # type: ignore[assignment]

    # 判定放在 close() **之后**：`src.closed` 要能反映「采集线程真的释放了假麦克风源」
    # （close() 会 join 采集线程，线程的 finally 里才调 src.close()）。
    all_ok = report([("原声档应为 1kHz", results.get("原声", 0.0), TONE_MIC_HZ),
                     ("译音档应为 500Hz", results.get("译音", 0.0), TONE_TRANSLATED_HZ),
                     ("切回原声应为 1kHz", results.get("回原声", 0.0), TONE_MIC_HZ)])
    print("  假麦克风源已释放：", src.closed)
    if not all_ok or not src.closed:
        rc = 1

    # ---- Linux 特有的两条安全约束（回收后判：节点必须消失、默认输出必须没被动过）----
    time.sleep(1.0)
    L.clear_cache()
    left_sink = bool(L.node_props_by_name(SINK_NODE))
    left_source = bool(L.node_props_by_name(SOURCE_NODE))
    after_default = L.default_output_index()
    print("\n=== Linux 安全约束 ===")
    print(f"  {'✅' if not left_sink and not left_source else '❌'} 节点零残留："
          f"sink={left_sink} source={left_source}")
    print(f"  {'✅' if after_default == before_default else '❌'} 默认输出未被抢："
          f"{before_default} → {after_default}")
    if left_sink or left_source or after_default != before_default:
        rc = 1
    print("\n结论：", "✅ 原声直通 + 档位路由在真虚拟麦上成立" if rc == 0 else "❌ 有判据未通过")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
