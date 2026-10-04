# -*- coding: utf-8 -*-
"""通联开关音（每句开头 on / 结尾 off）—— `vlt/sfx.py` + 引擎推送顺序。

只测离线部分：读 WAV/重采样/推入顺序。真链路（真出声）不在这里跑。
"""
from __future__ import annotations

import sys
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlt import sfx as sfx_mod  # noqa: E402
from vlt.output.virtualmic import resample_24k_mono_to_48k_stereo  # noqa: E402

RATE48_STEREO_BYTES = 4800 * 2 * 2      # 0.1s@24k 单声道 → 48k 立体声 s16le 的字节数


def _write_wav(path: Path, *, sr: int, seconds: float, bits: int = 16,
               channels: int = 1, freq: float = 845.0) -> None:
    t = np.arange(int(sr * seconds)) / sr
    mono = 0.8 * np.sin(2 * np.pi * freq * t)
    data = np.repeat(mono, channels) if channels > 1 else mono
    if bits == 8:
        buf = ((data * 127) + 128).astype(np.uint8).tobytes()
        sw = 1
    else:
        buf = (data * 32767).astype(np.int16).tobytes()
        sw = 2
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels); w.setsampwidth(sw); w.setframerate(sr)
        w.writeframes(buf)


def test_read_wav_any_format() -> bool:
    """22.05k/16bit/单声道、44.1k/16bit/双声道、22.05k/8bit 都要读成 24k 单声道。"""
    ok = True
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for tag, kw in (("22k16m", dict(sr=22050, seconds=0.2, bits=16, channels=1)),
                        ("44k16s", dict(sr=44100, seconds=0.2, bits=16, channels=2)),
                        ("22k8m", dict(sr=22050, seconds=0.2, bits=8, channels=1))):
            p = d / f"{tag}.wav"
            _write_wav(p, **kw)
            pcm = sfx_mod.read_wav_as_24k_mono(p)
            secs = len(pcm) / 2 / sfx_mod.TARGET_RATE
            peak = float(np.abs(np.frombuffer(pcm, dtype=np.int16)).max()) / 32767
            good = abs(secs - 0.2) < 0.01 and 0.6 < peak < 0.95
            print(f"    {tag}: {secs:.3f}s 峰值 {peak:.2f} {'OK' if good else '✗'}")
            ok &= good
    print("  ✓ 任意位深/采样率/声道都能读成 24k 单声道")
    return ok


def test_load_pair_missing_file_traces() -> bool:
    """配了却找不到文件 → 那一侧为空（不播），另一侧照常；必须有留痕（禁静默降级）。"""
    ok = True
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _write_wav(d / "on.wav", sr=22050, seconds=0.2)
        cfg = {"open_sfx": "on.wav", "close_sfx": "nope.wav"}
        op, cl = sfx_mod.load_pair(cfg, (d,))
        ok &= bool(op) and not cl
        print(f"    open 有音频={bool(op)} · close（文件缺失）为空={not cl} "
              f"{'OK' if ok else '✗'}")
        # 没配置就是两侧都空
        op2, cl2 = sfx_mod.load_pair({}, (d,))
        ok &= not op2 and not cl2
        print(f"    未配置时两侧都为空={not op2 and not cl2} {'OK' if ok else '✗'}")
    print("  ✓ 缺失文件只影响那一侧，且会留痕")
    return ok


def test_engine_push_order() -> bool:
    """★ 引擎推入顺序必须是：开台音 → 正文分片 → 收台音 → 封句。"""
    import vlt.engine as engine_mod

    class Recorder:
        def __init__(self) -> None:
            self.events: list[tuple[str, int]] = []

        def push(self, pcm: bytes) -> None:
            self.events.append(("push", len(pcm)))

        def end_sentence(self) -> None:
            self.events.append(("end", 0))

    class Slot:
        """假 slot：给两片正文分片，然后 finished。"""

        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks

        def take(self, idx: int):
            if idx < len(self.chunks):
                return self.chunks[idx], False
            return None, True

    ok = True
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _write_wav(d / "on.wav", sr=22050, seconds=0.2, freq=845.0)
        _write_wav(d / "off.wav", sr=22050, seconds=0.4, freq=1325.0)
        ON = sfx_mod.load_pair({"open_sfx": "on.wav", "close_sfx": "off.wav"}, (d,))

        eng = engine_mod.Engine.__new__(engine_mod.Engine)      # 不跑 __init__（会建真设备）
        eng._cfg = SimpleNamespace(text_input={"tts": {"open_sfx": "on.wav", "close_sfx": "off.wav"}})
        eng._virtualmic = Recorder()
        eng._sfx_cache = None
        # 让相对路径按临时目录解析（等价于源码运行时的 BUNDLE_DIR=仓库根）
        engine_mod.BUNDLE_DIR = d

        chunk24 = (np.zeros(2400, dtype=np.int16)).tobytes()      # 0.1s @24k
        slot = Slot([chunk24, chunk24])
        eng._drain_slot_to_mic(slot)

        ev = eng._virtualmic.events
        want = [("push", len(ON[0])),
                ("push", RATE48_STEREO_BYTES), ("push", RATE48_STEREO_BYTES),
                ("push", len(ON[1])), ("end", 0)]
        good = ev == want
        print(f"    推入序列：{[(k, v if k == 'push' else '') for k, v in ev]}")
        if not good:
            print(f"    期望：{want}   实际：{ev}")
        ok &= good

        # 对照：把开关音配成空 → 只剩正文分片与封句（证明"没配就不播"）
        eng2 = engine_mod.Engine.__new__(engine_mod.Engine)
        eng2._cfg = SimpleNamespace(text_input={"tts": {}})
        eng2._virtualmic = Recorder()
        eng2._sfx_cache = None
        eng2._drain_slot_to_mic(Slot([chunk24]))
        plain = eng2._virtualmic.events
        want2 = [("push", RATE48_STEREO_BYTES), ("end", 0)]
        good2 = plain == want2
        print(f"    未配置时：{plain} {'OK' if good2 else '✗'}")
        ok &= good2
    print("  ✓ 顺序正确：开台音 → 分片 → 收台音 → 封句；未配置则不播")
    return ok


def test_voice_binding() -> bool:
    """★ 开关音按音色绑定：`sfx_voice` 留空 = 任何音色都播；填了 = 只在当前音色是它时播。

    用户要求：「这个选项只和国民护卫队音色绑定」。闸门在引擎里**每次调用都判一次** ——
    界面上换音色，下一条打字立刻生效，不用重启。
    """
    import vlt.engine as engine_mod

    MP = "qwen-tts-vc-MetroPolice-voice-20261003201838644-0a4b"
    CA = "qwen-tts-vd-clear_auto-voice-20260926233229068-247d"
    ok = True

    # ① 纯函数：留空/相符/不符/重建容错/当前为空
    cases = [
        ("留空 = 不绑定 → 任何音色都播", {"voice": CA}, True),
        ("绑 M、当前 M → 播", {"voice": MP, "sfx_voice": MP}, True),
        ("绑 M、当前 clear_auto → 不播", {"voice": CA, "sfx_voice": MP}, False),
        ("绑 M、当前是重建后的 M（时间戳不同）→ 仍播",
         {"voice": "qwen-tts-vc-MetroPolice-voice-20261004120000000-ffff", "sfx_voice": MP}, True),
        ("绑 M 但当前没设音色 → 不播", {"voice": "", "sfx_voice": MP}, False),
    ]
    for label, cfg, want in cases:
        got = sfx_mod.bound_to_current_voice(cfg)
        good = got is want
        print(f"    {label}：{got} {'OK' if good else '✗'}")
        ok &= good

    # ② 引擎闸门：绑到别的音色 → 一点开关音都不推
    class Recorder:
        def __init__(self) -> None:
            self.events: list[str] = []

        def push(self, pcm: bytes) -> None:
            self.events.append("push")

        def end_sentence(self) -> None:
            self.events.append("end")

    class Slot:
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks

        def take(self, idx: int):
            if idx < len(self.chunks):
                return self.chunks[idx], False
            return None, True

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        _write_wav(d / "on.wav", sr=22050, seconds=0.2, freq=845.0)
        _write_wav(d / "off.wav", sr=22050, seconds=0.4, freq=1325.0)
        old_bundle = engine_mod.BUNDLE_DIR
        engine_mod.BUNDLE_DIR = d
        try:
            tts = {"open_sfx": "on.wav", "close_sfx": "off.wav"}
            eng = engine_mod.Engine.__new__(engine_mod.Engine)
            eng._cfg = SimpleNamespace(text_input={"tts": dict(tts, voice=CA, sfx_voice=MP)})
            eng._virtualmic = Recorder()
            eng._sfx_cache = None
            eng._drain_slot_to_mic(Slot([np.zeros(2400, dtype=np.int16).tobytes()]))
            cond = eng._virtualmic.events == ["push", "end"]     # 只有正文分片 + 封句
            print(f"    引擎：当前 clear_auto（非绑定音色）→ 无开关音  "
                  f"{'OK' if cond else '✗'}  {eng._virtualmic.events}")
            ok &= cond

            # ③ 换成绑定的那条音色 → 立刻带上开关音（同一实例，不重启）
            eng._cfg.text_input["tts"]["voice"] = MP
            eng._virtualmic.events.clear()
            eng._drain_slot_to_mic(Slot([np.zeros(2400, dtype=np.int16).tobytes()]))
            cond = (len(eng._virtualmic.events) == 4
                    and eng._virtualmic.events[0] == "push"
                    and eng._virtualmic.events[-1] == "end")
            print(f"    引擎：切到绑定音色 → 开关音立刻回来（{eng._virtualmic.events}）  "
                  f"{'OK' if cond else '✗'}")
            ok &= cond
        finally:
            engine_mod.BUNDLE_DIR = old_bundle
    return ok


def main() -> int:
    results = [
        ("读任意格式 → 24k 单声道", test_read_wav_any_format()),
        ("开关音按音色绑定（留空/相符/不符/重建容错 + 引擎闸门 + 切音色即时生效）",
         test_voice_binding()),
        ("缺文件只影响一侧 + 留痕", test_load_pair_missing_file_traces()),
        ("引擎推入顺序", test_engine_push_order()),
    ]
    bad = [n for n, r in results if not r]
    print("ALL PASSED" if not bad else f"FAILED: {', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
