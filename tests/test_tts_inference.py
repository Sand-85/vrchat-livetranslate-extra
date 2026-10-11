"""Qwen-Audio-TTS 专用 ws 合成：离线验证（本地假 ws 服务器，不花钱）。

要证四件事：
  ① 路由判据：只有「该族模型 + 配了专属域名」才走 ws（其余仍走 HTTP）；
  ② 三条控制消息的形状正确（run-task / continue-task / finish-task，含 voice/rate/pitch）；
  ③ 真连线：二进制音频帧能按序拼成 PCM，且 on_chunk 逐帧回调（禁静默）；
  ④ 失败留痕：服务端 task-failed → 抛 TtsError（不吞）。
"""
from __future__ import annotations

import json
import struct
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

# ---------- ⑤ 域名解析（显式优先，其次由业务空间 ID 推） ----------
print("⑤ 域名解析")
check("只给 ID → 补全地域域名",
      tts.inference_host_for("ws-abc") == "ws-abc.cn-beijing.maas.aliyuncs.com")
check("直接粘整个域名 → 原样用",
      tts.inference_host_for("ws-x.cn-beijing.maas.aliyuncs.com")
      == "ws-x.cn-beijing.maas.aliyuncs.com")
check("空 / 空白 → 空串（调用方回落）",
      tts.inference_host_for("") == "" and tts.inference_host_for("   ") == "")
check("显式配置优先",
      tts.resolve_inference_host("explicit.host", "ws-id") == "explicit.host")
check("没显式就推",
      tts.resolve_inference_host("", "ws-id") == "ws-id.cn-beijing.maas.aliyuncs.com")
check("都没有 → 空（不走 ws）", tts.resolve_inference_host("", "") == "")

# ---------- ⑥ 音色页试听（voice_lab.sample_pcm）也走 ws ----------
print("⑥ 试听路径")
from vlt import voice_lab                                          # noqa: E402

server3 = serve(handler, "127.0.0.1", 0)
port3 = server3.socket.getsockname()[1]
threading.Thread(target=server3.serve_forever, daemon=True).start()
time.sleep(0.3)
pcm_aud = voice_lab.sample_pcm("v-1", "qwen-audio-3.0-tts-flash", api_key="k",
                               endpoint="http://127.0.0.1:1/dead",      # 打不通的 HTTP 端点
                               inference_host=f"ws://127.0.0.1:{port3}",
                               pitch=0.95, speech_rate=1.15, timeout=15.0)
check("试听出音频（若走 HTTP 会因死端点失败 → 证明走了 ws）", pcm_aud == b"".join(FRAMES))

# ---------- ⑦ 取宿主助手绝不抛（CI 教训） ----------
print("⑦ 回落宿主取值")
check("空 base_url → 空串（不抛）", tts.safe_host_of("") == "")
check("缺 scheme → 空串（不抛）", tts.safe_host_of("maas.qianwenaiapi.com/api-ws/v1/realtime") == "")
check("正常 base_url → host", tts.safe_host_of("https://maas.qianwenaiapi.com/api/v1")
      == "maas.qianwenaiapi.com")
check("None → 空串（不抛）", tts.safe_host_of(None) == "")          # type: ignore[arg-type]

# ---------- ⑧ 尾部静音裁剪（收台音贴住话音的关键） ----------
print("⑧ 尾部静音")
RATE = 24000


def _mk(speech_ms: int, tail_ms: int, amp: int = 8000) -> bytes:
    sp = b"".join(struct.pack("<h", amp if i % 2 else -amp)
                  for i in range(int(RATE * speech_ms / 1000)))
    return sp + b"\x00\x00" * int(RATE * tail_ms / 1000)


_ms = lambda p: len(p) / 2 / RATE * 1000          # noqa: E731
out = tts.trim_trailing_silence(_mk(300, 400))
check("长尾静音被裁到「语音+80ms 余量」", 370 <= _ms(out) <= 390)
out2 = tts.trim_trailing_silence(_mk(300, 50))
check("尾巴本来就短 → 原样不动", abs(_ms(out2) - 350) < 1)
allsil = b"\x00\x00" * RATE
check("整段静音 → 原样返回（不误裁）", tts.trim_trailing_silence(allsil) == allsil)
check("空 / 极短 → 原样", tts.trim_trailing_silence(b"") == b""
      and tts.trim_trailing_silence(b"\x01\x02") == b"\x01\x02")

# 帧内末尾静音长度（服务端分片常把语音与静音混在一片里）
mix = _mk(100, 200)
check("一片「语音+静音」→ 末尾静音段测出 200ms", abs(tts.trailing_quiet_run(mix) / 2 / RATE * 1000 - 200) < 2)
check("整片静音 → 等于帧长", tts.trailing_quiet_run(b"\x00\x00" * 480) == 960)
check("整片语音 → 0", tts.trailing_quiet_run(_mk(120, 0)) == 0)

# ---------- ⑨ 流式：只丢尾静音，语音零延迟 ----------
print("⑨ 流式丢尾静音")


def handler_tail(ws) -> None:
    """分三片：语音+静音混片 → 纯静音片 → 结束（模拟服务端的粗粒度分片）。"""
    for raw in ws:
        ev = json.loads(raw)
        if ev["header"]["action"] == "finish-task":
            ws.send(json.dumps({"header": {"event": "task-started", "task_id": "x"}}))
            ws.send(_mk(200, 300))                      # 混片
            ws.send(b"\x00\x00" * (RATE // 2))          # 0.5s 纯静音
            ws.send(json.dumps({"header": {"event": "task-finished", "task_id": "x"}}))
            return


server4 = serve(handler_tail, "127.0.0.1", 0)
port4 = server4.socket.getsockname()[1]
threading.Thread(target=server4.serve_forever, daemon=True).start()
time.sleep(0.3)
parts = list(tts.synthesize_stream("x", voice="v", model="qwen-audio-3.0-tts-flash",
                                   api_key="k", inference_host=f"ws://127.0.0.1:{port4}",
                                   timeout=15.0))
joined = b"".join(parts)
check("尾部静音被丢掉（总长相较原始 0.8s 明显缩短）", _ms(joined) < 400)
check("留下的尾巴 ≈ 语音+80ms 余量", 270 <= _ms(joined) <= 300)
check("首片就是语音（没有被整体扣住）", len(parts) >= 2 and _ms(parts[0]) >= 190)

# ---------- 汇总 ----------
bad = [n for n, ok in results if not ok]
print(f"\n共 {len(results)} 项，失败 {len(bad)} 项" + (f"：{bad}" if bad else ""))
sys.exit(1 if bad else 0)
