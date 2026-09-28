"""译音音源 A/B（热切换）验收：配置解析 / 引擎路由 / 设置界面接线。

覆盖：
  1) config：`output.audio.mode` 解析（默认 realtime；tts/b/typing/same → tts；脏值回落）
  2) 引擎 A 模式：模型的音频进虚拟麦；**不**调用本地 TTS
  3) 引擎 B 模式：不向模型要音频（会话配置强制 False）、模型音频一律丢弃、
     终版译文由本地流式 TTS 出声（分片直推 + 封句）
  4) 虚拟声卡门控：以前是「总开关 AND 方向级 output_audio」，打字腿只开总开关时白推；
     现在打字腿自己就能把虚拟麦要起来
  5) 界面：设置里 A/B 单选存在、切换即时写回 config.yaml 并通知在跑的引擎重建会话

全程离线：TTS 与引擎的 IO 都被替换，不连服务端、不花钱。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 界面语言钉死为中文：CI / 外国机器是英文系统，而本文件的断言写的是中文文案
# （上游 test_config_save / test_update_dialog 同样的做法）。产品代码不依赖这个补丁。
import vlt.i18n as _i18n                      # noqa: E402

_i18n.detect_system_language = lambda: "zh"

import vlt.engine as engine_mod            # noqa: E402
import vlt.gui as gui_mod                  # noqa: E402
from vlt.config import AppConfig, Direction, load_config   # noqa: E402
from vlt.engine import Engine, EngineEvents                # noqa: E402

EXAMPLE = ROOT / "config.example.yaml"


# ---------------------------------------------------------------- 替身


class FakeVirtualMic:
    def __init__(self) -> None:
        self.pushed: list[bytes] = []
        self.sentences = 0

    def push(self, pcm: bytes) -> None:
        self.pushed.append(pcm)

    def end_sentence(self) -> None:
        self.sentences += 1

    def close(self) -> None:  # 引擎清理时会调
        pass


class FakeChatbox:
    def send(self, *a, **kw) -> bool:
        return True

    def flush_pending(self) -> None:
        pass


def _mk(mode: str = "realtime", *, output_audio: bool = True,
        audio_enabled: bool = True, direction: str = "mine") -> Engine:
    cfg = AppConfig(
        session_base={"api_key": "sk-test", "model": "qwen3.8-livetranslate-flash-realtime",
                      "base_url": "wss://example.invalid"},
        directions={"mine": Direction(source_lang="zh", target_lang="zh",
                                      output_audio=output_audio),
                    "theirs": Direction(source_lang="en", target_lang="zh",
                                        output_audio=output_audio)},
        chatbox={"max_chars": 144},
        merger={},
        output={"audio": {"enabled": audio_enabled, "mode": mode}},
        text_input={"model": "qwen-mt-flash", "tts": {"enabled": True, "stream": True,
                                                      "model": "qwen3-tts-flash",
                                                      "voice": "Cherry"}},
    )
    return Engine(cfg=cfg, direction=direction, source="mic", sinks={"chatbox"},
                  events=EngineEvents(), dry_run=True)


# ---------------------------------------------------------------- 1) 配置解析


def test_config_mode() -> bool:
    ok = True
    cases = [("tts", "tts"), ("TTS", "tts"), ("b", "tts"), ("same", "tts"),
             ("realtime", "realtime"), ("", "realtime"), ("乱写", "realtime")]
    tmpl = EXAMPLE.read_text(encoding="utf-8")
    for value, want in cases:
        d = Path(tempfile.mkdtemp())
        try:
            import yaml

            raw = yaml.safe_load(tmpl) or {}
            raw.setdefault("output", {}).setdefault("audio", {})["mode"] = value
            p = d / "config.yaml"
            p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
            cfg = load_config(p, api_key="sk-test")
            got = (cfg.output.get("audio") or {}).get("mode")
            cond = got == want
            print(f"  mode={value!r:>10} → {got!r:>10}（期望 {want}）  {'OK' if cond else '✗'}")
            ok &= cond
        finally:
            shutil.rmtree(d, ignore_errors=True)
    return ok


# ---------------------------------------------------------------- 2/3) 引擎路由


def test_engine_routing() -> bool:
    ok = True
    real_stream = engine_mod.synthesize_stream
    calls: list[str] = []
    pcm24 = b"\x01\x02" * 2400                      # 0.1s @24k

    def fake_stream(text, **kw):
        calls.append(text)
        yield pcm24

    engine_mod.synthesize_stream = fake_stream
    try:
        # A 模式：会话要音频、模型的音频进虚拟麦、不合成
        a = _mk("realtime")
        a._virtualmic, a._chatbox = FakeVirtualMic(), FakeChatbox()
        a._on_audio(b"\x03\x04" * 1200)
        a._maybe_speak_final("你好")
        cond = (len(a._virtualmic.pushed) == 1 and a._virtualmic.pushed[0]
                and calls == [] and a._session_cfg().output_audio is True)
        print(f"  A 模式：模型音频推入 {len(a._virtualmic.pushed)} 段，TTS 调用 {len(calls)} 次，"
              f"会话要音频={a._session_cfg().output_audio}  {'OK' if cond else '✗'}")
        ok &= cond

        # B 模式：不要模型音频、模型音频丢弃、终版译文走本地 TTS
        calls.clear()
        b = _mk("tts")
        b._virtualmic, b._chatbox = FakeVirtualMic(), FakeChatbox()
        b._on_audio(b"\x03\x04" * 1200)
        cond = (len(b._virtualmic.pushed) == 0 and b._session_cfg().output_audio is False)
        print(f"  B 模式：会话要音频={b._session_cfg().output_audio}，"
              f"模型音频被丢弃={len(b._virtualmic.pushed) == 0}  {'OK' if cond else '✗'}")
        ok &= cond

        # B 模式出声：直接驱动协程（等价于 _maybe_speak_final 调度到的那条路）
        asyncio.run(b._speak_for_voice_leg("你好"))
        cond = (calls == ["你好"] and len(b._virtualmic.pushed) == 1
                and b._virtualmic.sentences == 1)
        print(f"  B 模式出声：TTS 调用={calls}，推入 {len(b._virtualmic.pushed)} 段，"
              f"封句 {b._virtualmic.sentences} 次  {'OK' if cond else '✗'}")
        ok &= cond

        # B 模式但方向级 output_audio 关着 → 不出声
        calls.clear()
        c = _mk("tts", output_audio=False)
        c._virtualmic, c._chatbox = FakeVirtualMic(), FakeChatbox()
        c._maybe_speak_final("你好")
        cond = calls == [] and c._virtualmic.pushed == []
        print(f"  B 但方向级关：TTS 调用 {len(calls)} 次  {'OK' if cond else '✗'}")
        ok &= cond

        # 虚拟声卡门控：只开总开关、方向级关着（打字腿场景）→ 仍要建虚拟麦
        d = _mk("realtime", output_audio=False)
        cond = d._needs_virtualmic() is True
        print(f"  打字腿场景（总开关开/方向级关）：需要虚拟麦={d._needs_virtualmic()}  "
              f"{'OK' if cond else '✗'}")
        ok &= cond
        e = _mk("realtime", output_audio=False, direction="theirs")
        cond = e._needs_virtualmic() is False
        print(f"  「别人说」方向（打字腿不适用）：需要虚拟麦={e._needs_virtualmic()}  "
              f"{'OK' if cond else '✗'}")
        ok &= cond
    finally:
        engine_mod.synthesize_stream = real_stream
    return ok


def test_voice_delta_dedupe() -> bool:
    """B 模式判重：前缀扩展只念后缀、重复下发不重念（实测踩过"你好你好我是我是…"）。"""
    ok = True
    eng = _mk("tts")

    # ① 服务端连续下发前缀扩展的终版 → 第一次整句，之后只念新增后缀
    seq = ["你好，", "你好，我是逆袭。", "你好，我是逆袭。今天我们来测试一下，",
           "你好，我是逆袭。今天我们来测试一下，VRChat里的实时同声传译。"]
    got = [eng._voice_delta_for(s) for s in seq]
    cond = got == ["你好，", "我是逆袭。", "今天我们来测试一下，",
                   "VRChat里的实时同声传译。"]
    print(f"  前缀扩展：{got}  {'OK' if cond else '✗'}")
    ok &= cond

    # ② 同一条终版紧接着重复下发 → 不再念
    cond = eng._voice_delta_for(seq[-1]) is None
    print(f"  紧邻重复：{eng._voice_delta_for(seq[-1])}（应为 None）  {'OK' if cond else '✗'}")
    ok &= cond

    # ③ 标点轻微漂移的续写也要认出来（共同前缀 ≥ 80%）
    eng._voice_spoken, eng._voice_spoken_ts = "今天天气不错，我们去公园散步。", 0.0
    got = eng._voice_delta_for("今天天气不错，我们去公园散步吧。顺便买点水果。")
    cond = got == "吧。顺便买点水果。"
    print(f"  标点漂移续写：{got!r}  {'OK' if cond else '✗'}")
    ok &= cond

    # ④ 完全无关的新句子 → 整句念
    got = eng._voice_delta_for("这句话和上一句没有关系。")
    cond = got == "这句话和上一句没有关系。"
    print(f"  新句子：{got!r}  {'OK' if cond else '✗'}")
    ok &= cond

    # ⑤ 更短的重复下发 → 忽略
    eng._voice_spoken = "完整的一句话。后半句。"
    cond = eng._voice_delta_for("完整的一句话。") is None
    print(f"  更短的重复：{eng._voice_delta_for('完整的一句话。')}（应为 None）  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    # ⑥ 隔得够久后用户又说了一模一样的话 → 照念（别把真重复吞掉）
    eng._voice_spoken, eng._voice_spoken_ts = "好的，谢谢。", 0.0
    eng._voice_spoken_ts = time.monotonic() - 5.0        # 5 秒前
    got = eng._voice_delta_for("好的，谢谢。")
    cond = got == "好的，谢谢。"
    print(f"  隔久后的同样文本：{got!r}  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


# ---------------------------------------------------------------- 4) 热切换 API


def test_voice_serialized() -> bool:
    """并发防交错：两段增量几乎同时到 → 必须串行（甚至合并成一次），绝不并发写同一虚拟声卡。

    实测踩过：前缀扩展的终版相隔 0.09s 到来，两路流式分片交错进抖动缓冲 →
    听感是"整段反复重念"，而总时长与 ASR 都看不出问题。
    """
    ok = True
    inflight = 0
    max_inflight = 0
    calls: list[str] = []
    real_stream = engine_mod.synthesize_stream

    def slow_stream(text, **kw):
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        calls.append(text)

        def gen():
            nonlocal inflight
            try:
                for _ in range(3):
                    time.sleep(0.05)           # 模拟流式合成耗时
                    yield b"\x01\x02" * 1200
            finally:
                inflight -= 1

        return gen()

    engine_mod.synthesize_stream = slow_stream
    eng = _mk("tts")
    eng._virtualmic, eng._chatbox = FakeVirtualMic(), FakeChatbox()

    async def scenario():
        eng._loop = asyncio.get_running_loop()
        eng._maybe_speak_final("你好，")
        eng._maybe_speak_final("你好，我是逆袭。")      # 紧接着的增量：应排队而不是并发
        await asyncio.sleep(0.8)

    try:
        asyncio.run(scenario())
    finally:
        engine_mod.synthesize_stream = real_stream

    cond = max_inflight == 1
    print(f"  同时进行的 TTS 调用上限={max_inflight}（必须 1）  {'OK' if cond else '✗'}")
    ok &= cond
    # 排队中的增量应与前一段**合并**成一次念（顺序不乱、接缝更少）
    cond = calls in (["你好，我是逆袭。"], ["你好，", "我是逆袭。"]) or len(calls) == 1
    print(f"  TTS 调用序列={calls}（应合并或严格串行）  {'OK' if cond else '✗'}")
    ok &= cond
    cond = len(eng._virtualmic.pushed) > 0 and eng._virtualmic.sentences >= 1
    print(f"  推入 {len(eng._virtualmic.pushed)} 段 / 封句 {eng._virtualmic.sentences} 次  "
          f"{'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_set_voice_output() -> bool:
    ok = True
    eng = _mk("realtime")
    st: list[tuple] = []
    eng._events = EngineEvents(on_status=lambda l, m: st.append((l, m)))
    cond = eng.set_voice_output("tts") is True and eng._voice_mode() == "tts"
    print(f"  切到 B：_voice_mode={eng._voice_mode()}，状态={st[-1:] }  {'OK' if cond else '✗'}")
    ok &= cond
    st.clear()
    cond = eng.set_voice_output("realtime") is True and eng._voice_mode() == "realtime"
    print(f"  切回 A：_voice_mode={eng._voice_mode()}，状态={st[-1:] }  {'OK' if cond else '✗'}")
    ok &= cond
    # 同值重复切换 → 不重建会话、不改状态
    st.clear()
    cond = eng.set_voice_output("realtime") is True and st == []
    print(f"  重复切同一个值：状态消息 {len(st)} 条（应为 0）  {'OK' if cond else '✗'}")
    ok &= cond
    # 没跑起来时也给一句明确提示（用户点完要知道发生了什么）
    st.clear()
    eng.set_voice_output("tts")
    cond = any("下次开始翻译生效" in m for _l, m in st)
    print(f"  未运行时的提示：{st[-1:] }  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


# ---------------------------------------------------------------- 5) 界面接线


class FakeEngine:
    def __init__(self) -> None:
        self.direction = "mine"
        self.modes: list[str] = []

    def set_voice_output(self, mode: str) -> bool:
        self.modes.append(mode)
        return True


def test_gui_wiring() -> bool:
    try:
        from vlt.gui import TranslationGUI
    except Exception as exc:  # noqa: BLE001
        print(f"  （跳过：Tk 不可用 {exc}）")
        return True
    ok = True
    tmpdir = Path(tempfile.mkdtemp())
    real_cfg = gui_mod.DEFAULT_CONFIG
    cfg_path = tmpdir / "config.yaml"
    cfg_path.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    gui_mod.DEFAULT_CONFIG = cfg_path
    gui = None
    try:
        gui = TranslationGUI()
        if not hasattr(gui, "_voice_mode_var"):
            print("  ✗ 设置里没有译音音源单选项")
            return False
        cond = gui._voice_mode_var.get() in ("realtime", "tts")
        print(f"  初始值={gui._voice_mode_var.get()}（来自 config）  {'OK' if cond else '✗'}")
        ok &= cond

        fake = FakeEngine()
        gui._engines, gui._engine_dirs = [fake], ["mine"]
        gui._voice_mode_var.set("tts")
        gui._on_voice_mode_change()
        saved = cfg_path.read_text(encoding="utf-8")
        cond = (fake.modes == ["tts"] and "mode: tts" in saved
                and gui._cfg.output["audio"]["mode"] == "tts")
        print(f"  切到 B：引擎收到={fake.modes}，写盘={'mode: tts' in saved}，"
              f"状态={gui._status_label.cget('text')!r}  {'OK' if cond else '✗'}")
        ok &= cond

        gui._voice_mode_var.set("realtime")
        gui._on_voice_mode_change()
        saved = cfg_path.read_text(encoding="utf-8")
        cond = fake.modes == ["tts", "realtime"] and "mode: realtime" in saved
        print(f"  切回 A：引擎收到={fake.modes}，写盘={'mode: realtime' in saved}  "
              f"{'OK' if cond else '✗'}")
        ok &= cond

        # 没勾「译音输出」时提示要说明"不生效"，别让用户以为切了就有声
        gui._cfg.output["audio"]["enabled"] = False
        gui._refresh_voice_mode_hint()
        cond = "译音输出" in gui._voice_mode_hint.cget("text")
        print(f"  未开总开关的提示：{gui._voice_mode_hint.cget('text')[:28]}…  "
              f"{'OK' if cond else '✗'}")
        ok &= cond
    finally:
        gui_mod.DEFAULT_CONFIG = real_cfg
        shutil.rmtree(tmpdir, ignore_errors=True)
        if gui is not None:
            try:
                gui._root.destroy()
            except Exception:  # noqa: BLE001
                pass
    return ok


def main() -> int:
    print("test_voice_mode:")
    results = [
        ("配置解析", test_config_mode()),
        ("引擎路由", test_engine_routing()),
        ("判重/防叠念", test_voice_delta_dedupe()),
        ("串行防交错", test_voice_serialized()),
        ("热切换 API", test_set_voice_output()),
        ("界面接线", test_gui_wiring()),
    ]
    bad = [name for name, ok in results if not ok]
    print("ALL PASSED" if not bad else f"FAILED: {', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
