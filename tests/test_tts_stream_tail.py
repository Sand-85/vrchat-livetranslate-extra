"""疑点 #X：一句话被截断只剩半句 —— 句中停顿被当成「说完了」（离线复现，不花钱）。

真机证据（`logs/gui_20261013_220841.log`，2026-10-13 晚）：

    22:15:55.525 [chatbox] → 最终版 (76B) 我之前帮人弄过，俩人弄半宿变弄边骂     ← 17 字
    22:15:56.473 [tts] 流式提前收尾（已连续 250ms 纯零，认定说完）
    22:15:56.475 [mine][info] 打字已送出（17 字 → zh，已出声 1.2s）            ← 只念了前半句
    22:16:02.115 [tts] Qwen-Audio-TTS：ws 合成 163634 字节                     ← 服务端给足了 ≈3.4s

同一份日志里其它 5 句的时长都随字数增长（3 字 0.9s / 8 字 1.5s / 11 字 2.3s / 12 字 1.4s），
只有这一句 17 字 → 1.2s，收尾理由也是**唯一一次**「已连续 250ms 纯零」——
落点正好在逗号后（前 6 字「我之前帮人弄过」）→ 后半句整段丢了。

这里用假的 `synthesize_inference` 脚本化喂帧，离线复现两件事：
  A) 句中一段 300ms 纯零之后**服务端还在继续发片** → 后半句必须留在输出里（本用例主回归）；
  B) 真尾（纯零够长 + 服务端不再发片，但也不立刻关流）→ 仍要提前收尾，别把 900ms 静音全吐出去。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlt import tts                                                # noqa: E402

results: list[tuple[str, bool]] = []

SPEECH = 0x4000                     # 非零样本（16384 > TRIM_SILENCE_THRESHOLD 328）
RATE = tts.SAMPLE_RATE


def check(name: str, ok: bool) -> None:
    results.append((name, ok))
    print(f"  {name}  {'OK' if ok else '✗'}")


def _speech(ms: int) -> bytes:
    return (SPEECH.to_bytes(2, "little")) * int(RATE * ms / 1000.0)


def _zeros(ms: int) -> bytes:
    return bytes(2 * int(RATE * ms / 1000.0))


def _run(frames: list[bytes], *, hold_s: float) -> bytes:
    """跑真的 `_stream_inference`，只把「服务端发片」这一层换成脚本。

    `hold_s` = 喂完帧之后**先不关流**多久（真机：尾帧有时压到 ~1.9s，且流不立刻关）。
    """
    real = tts.synthesize_inference

    def fake(text: str, **kw) -> None:                             # 扮演服务端
        cb = kw["on_chunk"]
        for f in frames:
            cb(f)
            time.sleep(0.01)
        time.sleep(hold_s)

    tts.synthesize_inference = fake
    try:
        return b"".join(tts._stream_inference("测试", voice="v", model="m", host="h",
                                              api_key="k", rate=1.0, pitch=1.0, timeout=5.0))
    finally:
        tts.synthesize_inference = real


def _speech_bytes(pcm: bytes) -> int:
    """输出里「像语音」的字节数（用来判断两段话音是否都在）。"""
    cnt = 0
    for i in range(len(pcm) // 2):
        v = pcm[i * 2] | (pcm[i * 2 + 1] << 8)
        if v >= 0x8000:
            v -= 0x10000
        if v > tts.TRIM_SILENCE_THRESHOLD or v < -tts.TRIM_SILENCE_THRESHOLD:
            cnt += 2
    return cnt


def main() -> int:
    one = len(_speech(400))          # 一段 400ms 语音的字节数

    # A) 句中 300ms 纯零之后服务端继续发 → 后半句不能丢
    out_a = _run([_speech(400), _zeros(300), _speech(400), _zeros(120)], hold_s=0.0)
    check("句中停顿不再截断后半句（两段语音都在）", _speech_bytes(out_a) >= one * 2 * 0.95)
    check("A 路总长够（前半 + 后半）", len(out_a) >= one * 2)

    # B) 真尾：纯零够长 + 服务端不再发片（也不立刻关流）→ 仍提前收尾
    out_b = _run([_speech(400), _zeros(900)], hold_s=1.5)
    check("真尾仍提前收尾（没把 900ms 静音全吐出去）", len(out_b) < one + int(RATE * 0.5) * 2)
    check("真尾仍保住了话音", _speech_bytes(out_b) >= one * 0.95)

    bad = [n for n, ok in results if not ok]
    print(f"\n{'ALL PASSED' if not bad else '失败：' + ', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
