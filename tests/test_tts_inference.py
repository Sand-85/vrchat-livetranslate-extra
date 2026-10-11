"""Qwen-Audio-TTS 专用 ws 合成：离线验证（本地假 ws 服务器，不花钱）。

要证四件事：
  ① 路由判据：只有「该族模型 + 配了专属域名」才走 ws（其余仍走 HTTP）；
  ② 三条控制消息的形状正确（run-task / continue-task / finish-task，含 voice/rate/pitch）；
  ③ 真连线：二进制音频帧能按序拼成 PCM，且 on_chunk 逐帧回调（禁静默）；
  ④ 失败留痕：服务端 task-failed → 抛 TtsError（不吞）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from websockets.sync.server import serve                          # noqa: E402

from vlt import tts                                              # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    results.append((name, bool(cond)))
    print(f"  {'OK  ' if cond else 'FAIL'} {name}")


# ---------- ① 路由判据 ----------
print("① 路由判据")
check("该族 + 有域名 → 走 ws", tts.use_inference("qwen-audio-3.0-tts-flash", "h.example"))
check("该族 + 域名带空格也算", tts.use_inference("qwen-audio-3.0-tts-flash", "  h.example "))
check("非该族 → 不走 ws", not tts.use_inference("qwen3-tts-flash", "h.example"))
check("该族但没域名 → 不走 ws（避免静默走错路）",
      not tts.use_inference("qwen-audio-3.0-tts-flash", ""))
check("大小写不敏感", tts.use_inference("QWEN-AUDIO-3.0-TTS-FLASH", "h"))

# ---------- ② 载荷形状 ----------
print("② 载荷形状")
msgs = tts._inference_payloads(model="qwen-audio-3.0-tts-flash", voice="v-1", text="你好世界",
                               task_id="tid-1", rate=1.15, pitch=0.95, volume=50)
objs = [json.loads(m) for m in msgs]
check("三条消息", len(objs) == 3)
check("动作序列正确", [o["header"]["action"] for o in objs]
      == ["run-task", "continue-task", "finish-task"])
check("task_id 贯穿一致", {o["header"]["task_id"] for o in objs} == {"tid-1"})
run = objs[0]["payload"]
check("run-task 带 model/function",
      run["function"] == "SpeechSynthesizer" and run["model"] == "qwen-audio-3.0-tts-flash")
prm = run["parameters"]
check("parameters 带音色与调参",
      prm["voice"] == "v-1" and prm["rate"] == 1.15 and prm["pitch"] == 0.95
      and prm["format"] == "pcm" and prm["sample_rate"] == 24000)
check("文本在 continue-task 里", objs[1]["payload"]["input"]["text"] == "你好世界")

# ---------- ③ 真连线（本地假 ws 服务器） ----------
print("③ 真连线")
seen: dict[str, object] = {"actions": [], "text": None}
FRAMES = [b"\x01\x02" * 500, b"\x03\x04" * 300]


def handler(ws) -> None:
    fail = False
    for raw in ws:
        ev = json.loads(raw)
        if isinstance(ev, dict) and ev.get("envoy"):        # 测试开关：让服务端报 task-failed
            fail = True
            continue
        action = ev["header"]["action"]
        seen["actions"].append(action)
        if action == "continue-task":
            seen["text"] = ev["payload"]["input"]["text"]
        if action == "finish-task":
            if fail:
                ws.send(json.dumps({"header": {"event": "task-failed", "task_id": "x",
                                               "error_message": "Engine error [411]"}}))
                return
            ws.send(json.dumps({"header": {"event": "task-started", "task_id": "x"}}))
            for f in FRAMES:
                ws.send(f)
            ws.send(json.dumps({"header": {"event": "task-finished", "task_id": "x"}}))
            return


server = serve(handler, "127.0.0.1", 0)
port = server.socket.getsockname()[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.3)
host = f"ws://127.0.0.1:{port}"

chunks: list[bytes] = []
pcm = tts.synthesize_inference("测试一句话", voice="v-1", model="qwen-audio-3.0-tts-flash",
                               host=host, api_key="fake", rate=1.15, pitch=0.95, timeout=15.0,
                               on_chunk=chunks.append)
check("PCM 字节数 = 各帧之和", pcm == b"".join(FRAMES))
check("on_chunk 逐帧回调（禁静默）", chunks == FRAMES)
check("服务端收到三条动作", seen["actions"] == ["run-task", "continue-task", "finish-task"])
check("服务端收到文本", seen["text"] == "测试一句话")

# ---------- ④ 失败留痕 ----------
print("④ 失败留痕")


def handler_fail(ws) -> None:
    for raw in ws:
        ev = json.loads(raw)
        if ev["header"]["action"] == "finish-task":
            ws.send(json.dumps({"header": {"event": "task-failed", "task_id": "x",
                                           "error_message": "Engine error [411]"}}))
            return


server2 = serve(handler_fail, "127.0.0.1", 0)
port2 = server2.socket.getsockname()[1]
threading.Thread(target=server2.serve_forever, daemon=True).start()
time.sleep(0.3)
try:
    tts.synthesize_inference("x", voice="v", model="qwen-audio-3.0-tts-flash",
                             host=f"ws://127.0.0.1:{port2}", api_key="k", timeout=10.0)
    check("task-failed 应抛错", False)
except tts.TtsError as exc:
    check("task-failed 抛 TtsError 且带原文", "411" in str(exc))

# ---------- 汇总 ----------
bad = [n for n, ok in results if not ok]
print(f"\n共 {len(results)} 项，失败 {len(bad)} 项" + (f"：{bad}" if bad else ""))
sys.exit(1 if bad else 0)
