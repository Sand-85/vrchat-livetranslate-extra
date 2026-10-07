"""输入门限（只作用于 loopback「VRChat 输出 = 别人说话」）验收。

## 治什么

VRChat 把人声混成**一路**输出，远处玩家声音小、本来就听不清，以往照样被送给模型 ——
白花钱，还容易被翻成乱话。现在按每 100ms 块的 **RMS 响度**（dBFS）判：
低于门限的块不上送，模型听不到 = 不翻译。

三条「不切坏句子」的保证，每条都有用例守着：

① **hold**：超阈值后回落 500ms 内仍继续送 —— 说话句中的停顿、句尾渐弱不被切碎；
② **preroll**：开闸时补发越阈值之前的 250ms —— 句首辅音/起音不丢；
③ **只作用于 loopback**：麦克风腿（自己说话）绝不走这个门限（接线用例守着）。

已知代价（写在用例里，不遮）：远处小声的人说完、近处的人紧接着开口时，
小声尾巴的最近 250ms 会被当作句首 preroll 补发一次 —— 250ms 不足以成句，
换来的是近处说话开头不掉字。

## 离线可跑

判定用注入的 `now`（纯逻辑）；接线用例用假的 run_loopback / run_mic 捕获传参，
不连网络、不需要音频设备、不需要 API key。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

# GUI 用例断言「门限默认启用」（gate_enabled is True）与模板一致。
# 开发者本机 config.yaml 若是 gate_enabled: false，就会假红 → 统一走沙箱配置
# （内容 = config.example.yaml，gate_enabled: true，与 CI 一致）。
from _cfgbox import sandbox_config  # noqa: E402
sandbox_config(reset=True)

CHUNK = 0.1          # 每块时长（秒）：3200B @16kHz s16le mono


def _tone(amp: int, marker: int) -> bytes:
    """自造 100ms PCM 块：固定幅度 amp，第 2 个采样塞 marker 便于辨认是哪一块。

    恒定幅度块的 RMS = amp → 电平 dBFS = 20·log10(amp/32768)：
      amp=3277 ≈ -20dB（近处正常说话）｜amp=100 ≈ -50dB（远处小声）｜amp=0 = 底噪。
    """
    arr = np.full(1600, amp, dtype="<i2")
    arr[1] = marker
    return arr.tobytes()


def _mark(chunk: bytes) -> int:
    """读回块里的 marker（第 2 个采样，小端 int16）——用来断言块的顺序与身份。"""
    return int.from_bytes(chunk[2:4], "little")


def _cfg(**capture):  # noqa: ANN003, ANN202
    from vlt.config import AppConfig, Direction

    base = {"model": "x", "base_url": "x", "voice": "x", "api_key": "x",
            "workspace_id": "", "reconnect_backoff": [0.05],
            "max_new_sessions_per_minute": 100, "final_silence_s": 0.1}
    return AppConfig(
        session_base=base,
        directions={"mine": Direction(source_lang="zh", target_lang="en", output_audio=False)},
        chatbox={}, merger={}, overlay={}, output={"capture": dict(capture)},
    )


# ---------------------------------------------------------------- 电平判据

def test_chunk_level_db_basics() -> None:
    """电平口径：空块/全零 = 地板值；满量程 ≈ 0 dBFS；半幅 ≈ -6 dB。"""
    from vlt.engine import LEVEL_FLOOR_DB, chunk_level_db

    assert chunk_level_db(b"") == LEVEL_FLOOR_DB, "空块应返回地板值（不能是 -inf）"
    silence = np.zeros(1600, dtype="<i2").tobytes()
    assert chunk_level_db(silence) == LEVEL_FLOOR_DB, "全零块应是地板值"
    full = np.full(1600, 32767, dtype="<i2").tobytes()
    assert abs(chunk_level_db(full)) < 0.01, f"满量程应为 ~0dBFS：{chunk_level_db(full)}"
    half = np.full(1600, 16384, dtype="<i2").tobytes()
    assert abs(chunk_level_db(half) + 6.02) < 0.05, f"半幅应为 ~-6dBFS：{chunk_level_db(half)}"
    print("  RMS 电平口径（空/满/半幅）OK")


def test_rms_reflects_energy_not_single_spike() -> None:
    """RMS 口径的取舍（如实记录，别把优势说过头）。

    100ms 块里，**占空比**决定 RMS 比峰值低多少：占 1/1600 的尖刺只把 RMS 压到
    峰值 -32dB，而不是压到"没有"。所以：

    - 远处玩家小声说话 = **整块**都小 → RMS 一直低 → 被门限拦住（本功能的主用途）；
    - 单个满幅尖刺（按键/爆音）→ RMS ≈ -33 dBFS，**仍高于默认门限 -45**
      → 会开一次闸（100ms 块 + 500ms hold ≈ 0.6s）。这是已知代价：门限治"小声"，
      不治"瞬态噪声"；若被爆音频繁误开，把门限调高（例如 -35）即可。
    """
    import math

    from vlt.engine import chunk_level_db

    spike = np.zeros(1600, dtype="<i2")
    spike[800] = 30000                    # 一个采样点的尖刺
    db = chunk_level_db(spike.tobytes())
    peak_db = 20 * math.log10(30000 / 32768.0)
    expect = peak_db - 10 * math.log10(1600)
    assert abs(db - expect) < 0.5, f"RMS 应只按占空比衰减：实测 {db:.1f} vs 预期 {expect:.1f}"
    assert db > -45, "单点尖刺的 RMS 高于默认门限（已知代价：尖刺能开一次闸）"
    # 整块小声（远处说话）：RMS 就是低 —— 这正是被拦住的对象
    assert chunk_level_db(_tone(100, 0)) < -45
    print(f"  占空比口径：单点尖刺 {db:.1f} dBFS（峰值 {peak_db:.1f}）；整块小声被拦 OK")


# ---------------------------------------------------------------- 配置校验

def test_gate_settings_defaults_and_valid_values() -> None:
    """字段缺省 = 默认值；合法值原样采用。"""
    from vlt.engine import input_gate_settings

    assert input_gate_settings(None) == (True, -45.0, 500.0, 250)
    assert input_gate_settings({}) == (True, -45.0, 500.0, 250)
    assert input_gate_settings({"gate_enabled": False, "gate_db": -60,
                               "gate_hold_ms": 0, "gate_preroll_ms": 0}) == (False, -60.0, 0.0, 0)
    print("  缺省 / 合法取值 OK")


def test_gate_settings_invalid_values_fall_back_with_warning() -> None:
    """非法值必须**留痕**并回落默认值（绝不静默带病运行）。"""
    from vlt.engine import input_gate_settings

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = input_gate_settings({"gate_enabled": "yes", "gate_db": "loud",
                                   "gate_hold_ms": -1, "gate_preroll_ms": "x"})
    out = buf.getvalue()
    assert got == (True, -45.0, 500.0, 250), f"非法值没有回落默认：{got}"
    for key in ("capture.gate_db", "capture.gate_hold_ms", "capture.gate_preroll_ms",
                "capture.gate_enabled"):
        assert key in out, f"{key} 非法时没有留痕：{out!r}"
    # 超出物理范围也要拦（+5dBFS 比满量程还响，肯定是被手改坏了）
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        assert input_gate_settings({"gate_db": 5})[1] == -45.0
    assert "capture.gate_db" in buf2.getvalue()
    print("  非法值留痕 + 回落默认 OK")


# ---------------------------------------------------------------- 门限行为

def test_quiet_chunks_are_never_sent() -> None:
    """核心：低于门限的块一块都不上送，且只统计不报错。"""
    from vlt.engine import _LevelGate

    g = _LevelGate(enabled=True, threshold_db=-45.0, hold_ms=500.0, preroll_ms=250)
    for i in range(5):
        assert g.feed(_tone(100, i), now=i * CHUNK, dur_s=CHUNK) == [], \
            f"第 {i} 块小声音频被上送了"
    assert g.dropped_chunks == 5
    assert not g.is_open, "一直小声时不该开闸"
    assert g.level_db < -45, "电平应被记录（界面要用它显示实时电平）"
    print("  低于门限的块全部拦下 OK")


def test_gate_opens_and_replays_preroll_in_order() -> None:
    """★ 开闸：按**原顺序**补发 preroll + 当前块（不丢句首、不乱序）。"""
    from vlt.engine import _LevelGate

    g = _LevelGate(enabled=True, threshold_db=-45.0, hold_ms=500.0, preroll_ms=250)
    quiet = [_tone(100, m) for m in range(1, 4)]           # 3 块 ≈ -50dB
    for i, c in enumerate(quiet):
        assert g.feed(c, now=i * CHUNK, dur_s=CHUNK) == []
    assert len(g._preroll) == 2, f"preroll 250ms 只该留最近 2 块：{len(g._preroll)}"  # noqa: SLF001
    loud = _tone(3277, 9)                                  # ≈ -20dB
    out = g.feed(loud, now=0.3, dur_s=CHUNK)
    assert [_mark(c) for c in out] == [2, 3, 9], \
        f"开闸补发顺序不对：{[_mark(c) for c in out]}"
    assert g.is_open and g.opened == 1 and g.replay_count == 1
    nxt = _tone(3277, 10)
    assert g.feed(nxt, now=0.4, dur_s=CHUNK) == [nxt], "开闸后应逐块透传"
    print("  开闸补发 preroll（顺序正确）OK")


def test_hold_keeps_hold_of_mid_sentence_dip() -> None:
    """hold：句中停顿/渐弱（< hold 时长）仍继续送；超过 hold 才拦。"""
    from vlt.engine import _LevelGate

    g = _LevelGate(enabled=True, threshold_db=-45.0, hold_ms=500.0, preroll_ms=250)
    loud = _tone(3277, 1)
    assert g.feed(loud, now=0.0, dur_s=CHUNK) == [loud]
    dip = _tone(100, 2)
    assert g.feed(dip, now=0.2, dur_s=CHUNK) == [dip], "hold 期内（0.2s）不该停送"
    assert g.is_open
    assert g.feed(dip, now=0.4, dur_s=CHUNK) == [dip], "hold 期内（0.4s）不该停送"
    assert g.feed(dip, now=0.6, dur_s=CHUNK) == [], "超过 hold（0.6s）后必须停送"
    assert not g.is_open
    print("  hold 保住句中停顿、超时后收闸 OK")


def test_gate_disabled_passes_everything() -> None:
    """关掉门限 = 完全回到旧行为（一块不漏），但电平读数照旧更新。"""
    from vlt.engine import _LevelGate

    g = _LevelGate(enabled=False, threshold_db=-45.0, hold_ms=500.0, preroll_ms=250)
    for i in range(3):
        c = _tone(10, i)
        assert g.feed(c, now=i * CHUNK, dur_s=CHUNK) == [c]
    assert g.dropped_chunks == 0
    assert g.level_db < -60, "关掉门限也要记录电平（界面显示实时电平用）"
    print("  门限关闭时全透传 OK")


def test_preroll_budget_and_zero() -> None:
    """preroll 缓存不超过预算；设 0 时不补发（行为可预测）。"""
    from vlt.engine import _LevelGate

    g = _LevelGate(enabled=True, threshold_db=-45.0, hold_ms=500.0, preroll_ms=250)
    for i in range(20):
        g.feed(_tone(100, i), now=i * CHUNK, dur_s=CHUNK)
    assert g._preroll_ms <= 250 + 1e-3, f"preroll 超预算：{g._preroll_ms}ms"  # noqa: SLF001
    prere = len(g._preroll)                                                   # noqa: SLF001
    loud = _tone(3277, 99)
    assert len(g.feed(loud, now=2.0, dur_s=CHUNK)) == prere + 1, \
        f"补发块数应等于 preroll 存量 + 1：存量 {prere}"

    z = _LevelGate(enabled=True, threshold_db=-45.0, hold_ms=500.0, preroll_ms=0)
    for i in range(3):
        z.feed(_tone(100, i), now=i * CHUNK, dur_s=CHUNK)
    out = z.feed(_tone(3277, 99), now=0.4, dur_s=CHUNK)
    assert len(out) == 1 and _mark(out[0]) == 99, "preroll=0 时不该补发任何旧块"
    print("  preroll 预算 / 关零 OK")


def test_near_and_far_players_scenario() -> None:
    """★ 场景验收（用户要的效果）：远处小声的玩家整段不上送；近处的人一句不落。"""
    from vlt.engine import _LevelGate

    g = _LevelGate(enabled=True, threshold_db=-45.0, hold_ms=500.0, preroll_ms=250)
    far = [_tone(90, m) for m in range(1, 16)]            # 1.5s 远处小声（≈-51dB）
    near = [_tone(3277, m) for m in range(16, 26)]        # 1.0s 近处正常（≈-20dB）
    sent: list[bytes] = []
    t = 0.0
    for c in far + near:
        sent += g.feed(c, now=t, dur_s=CHUNK)
        t += CHUNK
    marks = [_mark(c) for c in sent]
    leaked = [m for m in marks if 1 <= m <= 13]
    assert not leaked, f"远处小声的音频被上送了（markers={leaked}）"
    assert set(range(16, 26)) <= set(marks), "近处玩家的话有块被漏掉"
    # 已知代价：开闸时补发小声尾巴的最近 250ms（此处 marker 14/15），换近处句首不掉字
    assert marks[:2] == [14, 15], f"没按预期补发句首 preroll：{marks[:4]}"
    ratio = (len(far) - 2) / len(far) * 100
    print(f"  近/远场景：远处 1.5s 中 {ratio:.0f}% 的音频被拦（仅补发 250ms 句首）OK")


# ---------------------------------------------------------------- 接线（只挂在 loopback 腿）

def test_engine_wires_gate_only_to_loopback() -> None:
    """接线：只有 loopback 腿拿到 gate；麦克风腿不传（自己说话不该被门限拦）。"""
    from vlt import engine as E

    seen: dict[str, dict] = {}

    async def fake_loopback(session, tele, **kw):  # noqa: ANN001, ANN003
        seen["loopback"] = kw

    async def fake_mic(session, tele, **kw):  # noqa: ANN001, ANN003
        seen["mic"] = kw

    orig_lb, orig_mic = E.run_loopback, E.run_mic
    E.run_loopback, E.run_mic = fake_loopback, fake_mic
    try:
        def build(src: str):  # noqa: ANN202
            return E.Engine(cfg=_cfg(gate_db=-55), direction="theirs", source=src,
                            sinks=set(), events=E.EngineEvents())

        async def feed(eng) -> None:  # noqa: ANN001
            await eng._feed_audio()   # noqa: SLF001

        eng_lb, eng_mic = build("loopback"), build("mic")
        asyncio.run(feed(eng_lb))
        asyncio.run(feed(eng_mic))
    finally:
        E.run_loopback, E.run_mic = orig_lb, orig_mic

    assert seen["loopback"].get("gate") is eng_lb.input_gate, "loopback 腿没拿到输入门限"
    assert "gate" not in seen["mic"], "麦克风腿**不该**拿到输入门限"
    assert eng_lb.input_gate.threshold_db == -55.0, "门限没按 capture.gate_db 建"
    assert eng_lb.input_gate.enabled is True
    print("  接线：门限只挂 loopback 腿 OK")


def test_real_signatures_accept_gate() -> None:
    """★ 对**真实函数签名**下断言（上面那条用例用假替身，正好测不出签名漂移）。

    #12 就是这么漏的：`Engine._feed_audio()` 传 `gate=`，而 `run_loopback()` 的形参里
    根本没有它 —— 一跑就 TypeError，采集腿整条死掉（两个平台都一样）；而接线用例里
    的替身是 `fake_*(**kw)`，什么关键字都照收。所以这里直接量真签名：
    loopback 链路上的每一环都必须能接住 gate，麦克风腿必须不接。
    """
    import inspect

    import vlt.engine as E

    chain = (E.run_loopback,                      # 入口（两个平台共用）
             E._run_loopback_linux,               # noqa: SLF001 —— Linux 转发
             E._pump_vrchat_capture,              # noqa: SLF001 —— Linux 混音后判定
             E._pump_capture)                     # noqa: SLF001 —— Windows 共用泵
    for fn in chain:
        params = inspect.signature(fn).parameters
        assert "gate" in params, f"{fn.__name__} 的签名里没有 gate：{list(params)}"
    assert "gate" not in inspect.signature(E.run_mic).parameters, \
        "麦克风腿（自己说话）不该接输入门限"
    print("  真实签名：loopback 链路每一环都能接 gate、麦克风腿不接 OK")


def test_engine_gate_disabled_and_legacy_config() -> None:
    """老配置（capture 段没有 gate_*）→ 用默认值；显式关掉 → enabled=False。"""
    from vlt.engine import Engine, EngineEvents

    legacy = Engine(cfg=_cfg(), direction="theirs", source="loopback",
                    sinks=set(), events=EngineEvents())
    assert (legacy.input_gate.enabled, legacy.input_gate.threshold_db) == (True, -45.0)
    off = Engine(cfg=_cfg(gate_enabled=False), direction="theirs", source="loopback",
                 sinks=set(), events=EngineEvents())
    assert off.input_gate.enabled is False
    print("  老配置缺省 / 显式关闭 OK")


def test_gui_gate_controls_and_config_write() -> None:
    """GUI：设置窗的门限控件齐全；改滑块 → 就地写进 `capture` 段（注释与键顺序保住）。

    个人 `config.yaml` 会**先备份、测完还原**（同 tests/test_config_save.py 的纪律）——
    用户自己改过的配置不能被测试抹掉。
    """
    import yaml

    from vlt.config import DEFAULT_CONFIG
    from vlt.gui import TranslationGUI

    if not DEFAULT_CONFIG.exists():        # CI / 新克隆：照程序的规矩先生成一份
        DEFAULT_CONFIG.write_text(
            (ROOT / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    before = DEFAULT_CONFIG.read_text(encoding="utf-8")
    n_comments = sum(1 for ln in before.splitlines() if ln.strip().startswith("#"))
    gui = TranslationGUI()
    try:
        assert gui._gate_level_canvas is not None, "设置窗里没有实时电平条"   # noqa: SLF001
        assert gui._gate_scale is not None and gui._gate_check is not None   # noqa: SLF001
        assert gui._gate_enabled_var.get() is True, "默认应启用（与 capture 默认一致）"  # noqa: SLF001
        gui._gate_var.set(-52)                                              # noqa: SLF001
        gui._gate_enabled_var.set(False)                                    # noqa: SLF001
        gui._on_gate_change()                                              # noqa: SLF001
        txt = gui._gate_val_lbl.cget("text")                               # noqa: SLF001
        assert "52" in txt, f"值标签没跟着滑块动：{txt!r}"
        if gui._gate_save_job is not None:      # 别等 300ms 的延后落盘        # noqa: SLF001
            gui._root.after_cancel(gui._gate_save_job)                     # noqa: SLF001
            gui._gate_save_job = None                                     # noqa: SLF001
        gui._save_gate_cfg()                                               # noqa: SLF001
        after = DEFAULT_CONFIG.read_text(encoding="utf-8")
        data = yaml.safe_load(after)
        cap = data.get("capture") or {}
        assert cap.get("gate_db") == -52, f"gate_db 没写进 capture 段：{cap}"
        assert cap.get("gate_enabled") is False, f"gate_enabled 没写：{cap}"
        assert cap.get("gate_hold_ms") == 500, "界面不该覆盖文件里的 hold（精细参数）"
        n_after = sum(1 for ln in after.splitlines() if ln.strip().startswith("#"))
        assert n_after == n_comments, f"写入破坏了注释：{n_comments} → {n_after} 行"
    finally:
        DEFAULT_CONFIG.write_text(before, encoding="utf-8")
        try:
            gui._root.destroy()
        except Exception:  # noqa: BLE001
            pass
    print("  GUI：控件齐全 + 就地写入 capture 段（注释保住）OK")


if __name__ == "__main__":
    print("test_input_gate:")
    test_chunk_level_db_basics()
    test_rms_reflects_energy_not_single_spike()
    test_gate_settings_defaults_and_valid_values()
    test_gate_settings_invalid_values_fall_back_with_warning()
    test_quiet_chunks_are_never_sent()
    test_gate_opens_and_replays_preroll_in_order()
    test_hold_keeps_hold_of_mid_sentence_dip()
    test_gate_disabled_passes_everything()
    test_preroll_budget_and_zero()
    test_near_and_far_players_scenario()
    test_engine_wires_gate_only_to_loopback()
    test_real_signatures_accept_gate()
    test_engine_gate_disabled_and_legacy_config()
    test_gui_gate_controls_and_config_write()
    print("ALL PASSED")
