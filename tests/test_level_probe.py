"""「勾了启用 + 设置窗打开 ⇒ 显示实时电平」的验收（独立电平探针）。

## 治什么

改之前，设置窗里的电平条只有一个数据源：**运行中引擎**的 `input_gate.level_db`。
于是「想调门限」必须先点「开始翻译」—— 可门限调的正是「多小的声音该被滤掉」，
而这时候音频已经在往模型上送了。用户口径（原话）：

    「只要上面的 select 勾选了启用就会显示当前电平，
      但是也需要注意，只有设置这个窗口被打开的时候才会有」

## 这里钉住的事

1. 设置窗**可见** + 勾了「启用」→ 探针跑起来、电平条出读数，**与是否在翻译无关**；
2. 关窗 / 取消勾选 → 立刻停：线程 join、假设备 close（窗口关着不许还占着采集），
   且之后**不会**自己再爬起来；
3. 有引擎在跑 → **绝不**另开一路 loopback（断言 opener 一次都没被调用），
   电平来自引擎的 `input_gate.level_db`；
4. 读数与峰保：电平口径与引擎那条腿同源（同一份 `to_16k_mono` / `chunk_level_db`），
   峰保仍只在界面那一处做（每 100ms 掉 1.5dB），两条路观感一致；
5. 失败留痕不静默：开不了设备 / 读取异常 → 一行 `[level]` 日志 + `last_error`，
   读数「—」；开不了设备时**低频自愈**（每 RETRY_S 重试，同一理由不重复打日志）；
6. **复查采集目标**（Linux 的坑）：目标流消失/增减 → 立刻关掉当前一路重开 ——
   `pw-record --target=` 在目标消失后会静默回落到默认源（麦克风），不复查就会
   一直采着不属于 VRChat 的信号；
7. 读到第一块真数据之前 `has_data=False`，界面显示「—」—— 不许把地板值画成
   假的 `-70 dB`。

## 离线保证

全程假 opener + 假采集源（`FakeSource`），**绝不开真声卡** —— CI 机器上根本没有
音频设备，而真开一路 loopback 还会与用户正在跑的翻译抢同一个端点。
界面用**临时 HOME + 临时 config.yaml** 起真窗口（绝不碰仓库/用户真配置），
界面语言钉死 zh（CI 是英文系统 + 1024px 虚拟屏）。

跑法：.venv/Scripts/python.exe tests/test_level_probe.py
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import math
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# CI / 新克隆上没有 key，而 load_config 默认要求 key。本用例只验电平探针，与 key 真假无关。
os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "levelprobeonly0123456789abcde")

# 界面文案跟随系统语言，钉死成中文保证可复现（产品代码不依赖这个补丁）。
import vlt.i18n as _i18n  # noqa: E402
_i18n.detect_system_language = lambda: "zh"

from vlt.engine import LEVEL_FLOOR_DB  # noqa: E402
from vlt.level_probe import LevelProbe  # noqa: E402
import vlt.level_probe as level_probe_mod  # noqa: E402

# 最小配置：capture 段留空 → 门限按默认值（enabled=True、-45dBFS），正是本用例要的前提
CONFIG_BODY = """\
# 主配置
session:
  model: qwen3.8-livetranslate-flash-realtime
  base_url: wss://example.invalid/realtime
  voice: Tina
  api_key: sk-unused
directions:
  mine:
    source_lang: zh
    target_lang: en
ui:
  lang: zh
"""


# ---------------------------------------------------------------- 假设备（绝不开真声卡）

def _pcm(amp: int, frames: int = 4800, channels: int = 2) -> bytes:
    """恒定幅度的 s16le PCM 块。

    恒定块的 RMS = amp，所以电平有闭式解：`20·log10(amp/32768)`。
      amp=3277 ≈ -20dBFS（近处正常说话）｜amp=1000 ≈ -30dBFS｜amp=100 ≈ -50dBFS（远处小声）
    默认 4800 帧 ×2ch = 100ms @48kHz 立体声（真 loopback 端点的常见形状）；
    整数倍降到 16k 单声道后仍是恒定幅度 → dB 不变，正好能拿来对口径。
    """
    return np.full(frames * channels, amp, dtype="<i2").tobytes()


def _db_of(amp: int) -> float:
    return 20.0 * math.log10(amp / 32768.0)


class FakeSource:
    """假采集源：接口与 `vlt/platform/base.py:AudioSource` 一致（rate/channels/read/close）。

    `repeat=True` → 块发完后从头再来（模拟「一直有声音」）；
    否则发完就返回 None（模拟「端点静音」= read 超时，真 WASAPI 就是这样）；
    `mute=True` → 立刻起返回 None（模拟「刚刚转静音」）；
    `read_error` → 发完后开始抛（模拟读取异常）。
    """

    def __init__(self, chunks=(), *, rate: int = 48000, channels: int = 2,  # noqa: ANN001
                 repeat: bool = False, read_error: Exception | None = None) -> None:
        self.rate = rate
        self.channels = channels
        self.mute = False       # 用例翻这个开关来「转静音」—— 时机必须握在用例手里，
                                # 靠「块发完自然静音」会 flaky：探针在别的线程，等测试线程
                                # 看到 chunks>=1 时它可能已经把电平写回地板值了
        self._chunks = list(chunks)
        self._repeat = repeat
        self._read_error = read_error
        self._i = 0
        self.read_calls = 0
        self.close_calls = 0

    async def read(self, timeout: float = 1.0) -> bytes | None:
        self.read_calls += 1
        if self.mute:
            await asyncio.sleep(min(0.01, timeout))
            return None
        if self._i >= len(self._chunks):
            if self._repeat and self._chunks:
                self._i = 0
            elif self._read_error is not None:
                raise self._read_error
            else:
                await asyncio.sleep(min(0.01, timeout))
                return None                      # 超时 = 还活着但暂时没数据
        chunk = self._chunks[self._i]
        self._i += 1
        await asyncio.sleep(0.005)               # 别把 CPU 转满（真源是 10 块/秒）
        return chunk

    def close(self) -> None:
        self.close_calls += 1


def _wait_for(pred, timeout: float = 3.0, what: str = "条件") -> None:  # noqa: ANN001
    """轮询等一个条件成立（探针在别的线程里跑，靠 sleep 拍脑袋迟早 flaky）。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError(f"等了 {timeout:g}s，{what} 仍未成立")


def _probe(opener, **kw) -> LevelProbe:  # noqa: ANN001, ANN202
    """建一个探针：read 超时压到 50ms，用例里 stop() 才不用干等 200ms。

    ⚠️ 默认 `recheck=None`：本机（Linux）产品默认会去查 PipeWire 图，测试机器上没有
    那套东西，注入假源时必须显式关掉复查；要验复查的用例自己传 `recheck=`。
    """
    kw.setdefault("read_timeout", 0.05)
    kw.setdefault("join_timeout", 3.0)
    kw.setdefault("recheck", None)
    return LevelProbe(opener=opener, **kw)


# ---------------------------------------------------------------- ① 探针本体（无界面）

def test_probe_reports_level_of_known_pcm() -> None:
    """★ 读数正确：48kHz 立体声的恒定幅度块 → dB 与闭式解一致（走过 to_16k_mono）。"""
    src = FakeSource([_pcm(3277)], repeat=True)
    p = _probe(lambda: src)
    p.start()
    try:
        _wait_for(lambda: p.chunks >= 3, what="探针采到 3 块")
        assert abs(p.level_db - _db_of(3277)) < 0.05, \
            f"电平不对：{p.level_db:.2f}，期望 {_db_of(3277):.2f}（≈-20dBFS）"
        assert p.running is True and p.last_error is None
        assert src.close_calls == 0, "还在跑就不该关设备"
    finally:
        p.stop()
    assert p.running is False
    assert src.close_calls >= 1, "stop() 之后必须释放设备"
    print(f"  ✓ 读数正确：48kHz×2ch 恒定块 → {p.level_db:.1f} dBFS"
          f"（闭式解 {_db_of(3277):.1f}）；stop 后设备已 close（{src.close_calls} 次）")


def test_probe_level_matches_engine_recipe() -> None:
    """★ 与引擎同一路配方：同一幅度在 16k 单声道与 48k 立体声下算出**同一个** dB。

    这条钉的是「用户照这条电平调门限不会调错」—— 探针必须与 `engine.run_loopback`
    用同一份重采样（`to_16k_mono`）+ 同一个电平函数（`chunk_level_db`）。
    """
    from vlt.engine import chunk_level_db, to_16k_mono

    reports = []
    for amp in (3277, 1000, 100):
        wide = FakeSource([_pcm(amp, frames=4800, channels=2)], rate=48000, channels=2,
                          repeat=True)
        narrow = FakeSource([_pcm(amp, frames=1600, channels=1)], rate=16000, channels=1,
                            repeat=True)
        p_wide = _probe(lambda w=wide: w)
        p_narrow = _probe(lambda n=narrow: n)
        p_wide.start()
        p_narrow.start()
        try:
            _wait_for(lambda: p_wide.chunks >= 2 and p_narrow.chunks >= 2,
                      what="两路都采到块")
            assert abs(p_wide.level_db - p_narrow.level_db) < 0.05, \
                (f"重采样口径不一致：48k×2ch={p_wide.level_db:.2f} "
                 f"vs 16k×1ch={p_narrow.level_db:.2f}")
            # 再与纯函数对一遍（引擎用的就是这两个）
            raw = _pcm(amp, frames=4800, channels=2)
            assert abs(p_wide.level_db - chunk_level_db(to_16k_mono(raw, 48000, 2))) < 0.05
            reports.append(f"amp{amp}→{p_wide.level_db:.1f}dB")
        finally:
            p_wide.stop()
            p_narrow.stop()
    print(f"  ✓ 与引擎同一配方：{', '.join(reports)}（两种采样形状读数一致）")


def test_silence_timeout_returns_to_floor() -> None:
    """端点静音（read 超时）→ 电平回落到地板值，**不许冻在**最后一块上。

    WASAPI loopback 在没声音时压根不产数据；要是把超时当「无变化」，
    用户会看着一条不动的电平以为还在出声。
    """
    src = FakeSource([_pcm(3277)], repeat=True)   # 一直响，直到用例把 mute 翻上
    p = _probe(lambda: src)
    p.start()
    try:
        _wait_for(lambda: p.chunks >= 1, what="采到响块")
        assert p.level_db > -25, f"响块应读到 ≈-20dB：{p.level_db:.1f}"
        src.mute = True                          # 端点转静音：从这里起 read 一直超时
        _wait_for(lambda: p.level_db == LEVEL_FLOOR_DB, what="静音后回落到地板值")
        assert p.running is True, "静音不是失败：探针要还活着"
        assert p.last_error is None, f"静音不该记错误：{p.last_error!r}"
    finally:
        p.stop()
    print(f"  ✓ 静音（read 超时）→ 回落地板值 {LEVEL_FLOOR_DB:g}dB；探针仍在跑、不算失败")


def test_open_failure_logs_once_and_retries_low_frequency() -> None:
    """★ 开不了设备：一行 `[level]` 日志 + `last_error`，同一理由**不重复打**；
    但会**低频重试**（不是永久放弃）—— 这是「先开窗、后启动 VRChat」能自愈的前提。"""
    calls: list[int] = []

    def boom():
        calls.append(1)
        raise OSError("模拟：设备被独占（AUDCLNT_E_DEVICE_INVALIDATED）")

    p = _probe(boom, retry_s=0.05)               # 重试周期压到 50ms 好观察
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        p.start()
        _wait_for(lambda: len(calls) >= 3, what="失败后按 RETRY_S 低频重试")
        _wait_for(lambda: p.running is True, what="重试期间线程仍活着（在等目标）")
        _wait_for(lambda: p.has_data is False, what="没有可信数据")
        time.sleep(0.1)                          # 多留几跳，看日志会不会被刷
    out = buf.getvalue()
    assert len(calls) >= 3, f"没有低频重试（opener 只调了 {len(calls)} 次）"
    assert out.count("[level] ❌") == 1, f"同一理由只许一行日志：{out!r}"
    assert p.last_error and "设备被独占" in p.last_error, f"last_error 没记原因：{p.last_error!r}"
    assert p.level_db == LEVEL_FLOOR_DB and p.has_data is False
    p.stop()                                     # 等待中的探针也要能安全 stop
    p.stop()                                     # 且幂等
    print(f"  ✓ 开设备失败：1 行 [level] 留痕、last_error 有原因、opener 重试 "
          f"{len(calls)} 次（日志不刷屏）、不崩")


def test_open_failure_recovers_when_target_appears() -> None:
    """★ F2 自愈：先开设置窗（VRChat 还没跑）→ 环境恢复后**自动接上**，不必关窗重开。

    这是 Linux 上很常见的顺序：设置窗先开着，VRChat 后启动。旧实现开失败一次就
    永久停下、读数一直「—」。
    """
    calls: list[int] = []
    src = FakeSource([_pcm(3277)], repeat=True)

    def opener():
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("没找到 VRChat 的音频输出流（VRChat 在跑并且出声了吗？）")
        return src

    p = _probe(opener, retry_s=0.05)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        p.start()
        _wait_for(lambda: p.has_data and p.chunks >= 2, what="环境恢复后自动接上并出数")
    assert len(calls) >= 3, f"应重试到成功：opener 调了 {len(calls)} 次"
    assert p.running is True and p.last_error is None, "接上后不该还留着错误"
    assert abs(p.level_db - _db_of(3277)) < 0.05, f"接上后读数不对：{p.level_db:.2f}"
    assert src.close_calls == 0, "接上后设备不该被关"
    p.stop()
    out = buf.getvalue()
    assert out.count("[level] ❌") == 1, f"失败理由只留一行：{out!r}"
    assert "[level] ✅" in out, f"恢复要有「接上」留痕：{out!r}"
    print(f"  ✓ F2：opener 前 2 次失败、第 3 次成功 → 自动接上（读数 {p.level_db:.1f} dB）、"
          f"失败日志 1 行 + 恢复日志 1 行")


def test_recheck_reopens_when_targets_change() -> None:
    """★ F1：目标集合变化（VRChat 播放流增减）→ 关掉旧的一路、按新目标重开。

    `pw-record --target=<serial>` 在目标节点消失后不会退出、会回落到默认源（麦克风），
    必须由探针主动收尾，否则读数会变成麦克风的电平。
    """
    sig = ["3684|3833"]                          # 初始：2 路
    made: list[FakeSource] = []

    def opener():
        s = FakeSource([_pcm(3277)], repeat=True)
        made.append(s)
        return s

    p = _probe(opener, recheck=lambda: sig[0], recheck_s=0.05, retry_s=0.05)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        p.start()
        _wait_for(lambda: len(made) == 1 and made[0].read_calls >= 2,
                  what="第一路采起来")
        sig[0] = "3684"                          # 掉了一路 → 签名变了
        _wait_for(lambda: len(made) >= 2, what="目标变化后重开一路")
        assert made[0].close_calls >= 1, "★ 旧路必须关掉（否则回落到麦克风还继续采）"
        _wait_for(lambda: made[1].read_calls >= 1, what="新路在读")
    p.stop()
    out = buf.getvalue()
    assert "采集目标变化" in out and "重开采集" in out, f"变化要有留痕：{out!r}"
    assert made[1].close_calls >= 1, "stop() 后新路也要关"
    print(f"  ✓ F1：签名 2 路 → 1 路 → 关旧路（close {made[0].close_calls} 次）+ 重开一路")


def test_recheck_to_empty_keeps_waiting_then_recovers() -> None:
    """★ F1 + F2 合体：VRChat 退出（目标空）→ 关掉采集、**不再采麦克风**、读数回「—」；
    VRChat 再起 → 自动接上。这是「窗口开着、VRChat 中途退出/重进」的完整来回。"""
    sig = ["3684|3833"]
    made: list[FakeSource] = []

    def opener():
        if not sig[0]:
            raise RuntimeError("没找到 VRChat 的音频输出流（VRChat 在跑并且出声了吗？）")
        s = FakeSource([_pcm(3277)], repeat=True)
        made.append(s)
        return s

    p = _probe(opener, recheck=lambda: sig[0], recheck_s=0.05, retry_s=0.05)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        p.start()
        _wait_for(lambda: len(made) == 1 and made[0].read_calls >= 2, what="采起来")
        assert p.has_data is True
        sig[0] = ""                              # VRChat 退出：目标全没了
        _wait_for(lambda: made[0].close_calls >= 1, what="目标消失后关掉采集")
        assert p.running is True, "★ 目标没了也要继续等（低频重试），不是永久死掉"
        assert p.has_data is False, "★ 不许再显示麦克风的电平（has_data 必须归 False）"
        assert p.level_db == LEVEL_FLOOR_DB
        sig[0] = "4273"                          # VRChat 回来了
        _wait_for(lambda: len(made) >= 2 and p.has_data, what="VRChat 回来自动接上")
    p.stop()
    out = buf.getvalue()
    assert out.count("[level] ❌") == 1, f"「没目标」只留一行、不刷屏：{out!r}"
    assert "[level] ✅" in out, f"恢复要有留痕：{out!r}"
    print(f"  ✓ F1+F2：目标消失 → 关采集（close {made[0].close_calls} 次）且 has_data=False；"
          f"目标回来 → 自动接上（第 {len(made)} 路）")


def test_recheck_empty_baseline_never_shows_stale_stream() -> None:
    """★ #26-1 残余窗口：open 成功、但当次复查签名已空（VRChat 恰在这几毫秒退出，
    或这次枚举抛错被 `pick_vrchat_targets` 吞成空）→ 立刻关掉那一路、按「目标不在」处理。

    旧实现把这次空签名**当基线**记下：`pw-record` 其实已按 `node.autoconnect` 回落到
    麦克风，而此后每次 recheck 也返回空、与基线相等 → **永不重开**，界面照样跳数字。
    """
    sig = [""]                                   # 复查口径一开始就看不到目标
    opens: list[int] = []
    made: list[FakeSource] = []

    def opener():
        opens.append(1)
        # 第一次：目标还在（open 成功）—— 模拟「open 与复查之间 VRChat 退出」；
        # 之后按 sig 走真实现的口径（没目标就抛），这才轮到低频重试。
        if len(opens) == 1 or sig[0]:
            s = FakeSource([_pcm(3277)], repeat=True)
            made.append(s)
            return s
        raise RuntimeError(level_probe_mod._NO_TARGET_MSG)

    p = _probe(opener, recheck=lambda: sig[0], recheck_s=0.05, retry_s=0.05)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        p.start()
        _wait_for(lambda: made and made[0].close_calls >= 1,
                  what="空基线：刚开出来的那一路被立刻关掉")
        assert p.has_data is False, "★ 空基线期间不许把麦克风的电平当 VRChat 显示"
        assert p.level_db == LEVEL_FLOOR_DB
        assert p.running is True, "仍要留在等待里（低频自愈），不是永久死掉"
        sig[0] = "4273"                          # VRChat 回来
        _wait_for(lambda: len(made) >= 2 and p.has_data, what="目标回来后自动接上")
        assert abs(p.level_db - _db_of(3277)) < 0.05, f"接上后读数不对：{p.level_db:.2f}"
    p.stop()
    out = buf.getvalue()
    assert out.count("[level] ❌") == 1, f"同一理由只留一行、不刷屏：{out!r}"
    assert "[level] ✅" in out, f"恢复要有留痕：{out!r}"
    print(f"  ✓ #26-1：空基线 → 立刻关掉第 1 路（close {made[0].close_calls} 次）、"
          f"has_data=False、等待后自动接上（第 {len(made)} 路）；失败日志 1 行")


def test_has_data_cleared_before_reopen_finishes() -> None:
    """★ #26-2：关旧源那一刻 `has_data` 就要归 False。

    重开一路在 Linux 上是拉起 `pw-record` 的数百毫秒，这段窗口里界面判据是
    `probe.running and probe.has_data` —— 旧实现要等下一轮 `opener()` 返回才置 False，
    于是这段窗口画的是**冻结的旧 dB**，而不是「—」。
    """
    sig = ["3684"]
    made: list[FakeSource] = []
    gate = threading.Event()                     # 卡住第二路 opener，模拟「设备正在拉起」

    def opener():
        if not made:
            s = FakeSource([_pcm(3277)], repeat=True)
            made.append(s)
            return s
        gate.wait(timeout=3.0)
        s = FakeSource([_pcm(3277)], repeat=True)
        made.append(s)
        return s

    p = _probe(opener, recheck=lambda: sig[0], recheck_s=0.05, retry_s=0.05)
    with contextlib.redirect_stdout(io.StringIO()):   # 收掉探针自己的「采集目标变化」日志
        p.start()
        try:
            _wait_for(lambda: p.has_data and made and made[0].read_calls >= 1, what="第一路出数")
            sig[0] = "3684|3833"                 # 目标变化 → 关旧源、准备重开
            _wait_for(lambda: made[0].close_calls >= 1, what="旧源被关")
            # 此刻第二次 opener 还卡在 gate 里（重开尚未完成）
            assert p.has_data is False, "★ 关源后 has_data 必须已经归 False（不许画冻结旧 dB）"
            assert p.running is True, "重开期间线程仍活着"
        finally:
            gate.set()
            p.stop()
    print("  ✓ #26-2：旧源关闭即 has_data=False（重开未完成期间界面显示「—」而非旧读数）")


def test_no_fake_level_before_first_chunk() -> None:
    """★ F3：刚起来还没读到第一块时 `has_data=False`、`level_db` 仍是地板值 ——
    界面据此显示「—」，不许把地板值 clamp 成假的 `-70 dB`。"""
    src = FakeSource([])                          # 永不产块：read 一直超时返回 None
    p = _probe(lambda: src)
    p.start()
    try:
        _wait_for(lambda: src.read_calls >= 3, what="连着读了几次仍然没有块")
        assert p.running is True, "没有块不是失败：探针要还活着"
        assert p.last_error is None, f"静音不该记错误：{p.last_error!r}"
        assert p.has_data is False, "★ 没读到块就不许有读数"
        assert p.level_db == LEVEL_FLOOR_DB
    finally:
        p.stop()
    print("  ✓ F3：第一块之前 has_data=False（界面显示「—」而不是假的 -70 dB）")


def test_open_level_source_linux_branch() -> None:
    """★ 补覆盖空白：真正执行 `open_level_source()` 的 **Linux 分支**
    （`pick_vrchat_targets` + 逐路 `pw-record` + 多路混音），CI 的假 opener 用例从没走过。

    用假 `pick_vrchat_targets` + 假 backend，**不碰任何真设备**。
    """
    from vlt import platform as plat
    from vlt.platform.audio import MixedAudioSource
    from vlt.engine import LoopbackTarget

    def _t(serial: str) -> LoopbackTarget:
        return LoopbackTarget(id=serial, name=f"VRChat.exe (audio stream #{serial})",
                              sample_rate=48000, channels=2)

    opened_ids: list[str] = []

    class _Backend:
        def open_loopback(self, target, blocksize=0):  # noqa: ANN001, ANN202
            opened_ids.append(target.id)
            return FakeSource([], rate=48000, channels=2)

    saved = (level_probe_mod.pick_vrchat_targets, plat.IS_LINUX, plat.capture_backend)
    try:
        level_probe_mod.pick_vrchat_targets = lambda: [_t("1"), _t("2")]
        plat.IS_LINUX = True
        plat.capture_backend = lambda: _Backend()
        with contextlib.redirect_stdout(io.StringIO()):
            src = level_probe_mod.open_level_source()
        assert isinstance(src, MixedAudioSource) and src.count == 2, \
            "多路必须混成一路（与引擎同口径）"
        assert opened_ids == ["1", "2"], f"应逐路各开一条：{opened_ids}"

        # 单路：直接返回那一路，不无谓地包一层混音
        opened_ids.clear()
        level_probe_mod.pick_vrchat_targets = lambda: [_t("9")]
        with contextlib.redirect_stdout(io.StringIO()):
            src1 = level_probe_mod.open_level_source()
        assert not isinstance(src1, MixedAudioSource) and opened_ids == ["9"]

        # 没有目标 → 抛（由 LevelProbe 统一留痕/重试）
        level_probe_mod.pick_vrchat_targets = lambda: []
        try:
            level_probe_mod.open_level_source()
        except RuntimeError as exc:
            assert "VRChat" in str(exc), f"错误文案该指向 VRChat：{exc}"
        else:
            raise AssertionError("没有 VRChat 流时必须抛异常，不能返回 None")

        # 签名口径：与引擎腿一致（serial 排序拼接）
        level_probe_mod.pick_vrchat_targets = lambda: [_t("2"), _t("1")]
        assert level_probe_mod.vrchat_target_signature() == "1|2"
        level_probe_mod.pick_vrchat_targets = lambda: []
        assert level_probe_mod.vrchat_target_signature() == ""
    finally:
        (level_probe_mod.pick_vrchat_targets, plat.IS_LINUX, plat.capture_backend) = saved
    print("  ✓ Linux 分支真执行：多路混音 / 单路直取 / 无目标抛错 / 签名口径")


def test_read_error_leaves_trace_and_closes_device() -> None:
    """读取中途异常：留痕 + 停 + **关掉设备**（不能把流漏在那儿）。"""
    src = FakeSource([_pcm(3277)], read_error=RuntimeError("模拟：流被拔掉"))
    p = _probe(lambda: src)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        p.start()
        _wait_for(lambda: p.running is False, what="读取异常后停下")
    out = buf.getvalue()
    assert "[level]" in out and "读取系统声失败" in out, f"读取异常必须留痕：{out!r}"
    assert p.last_error and "流被拔掉" in p.last_error
    assert src.close_calls >= 1, "异常退出也必须关设备"
    assert p.chunks >= 1, "异常之前那块应该已经读到"
    p.stop()
    print(f"  ✓ 读取异常：留痕 + 停 + 设备已 close（{src.close_calls} 次）")


def test_stop_joins_thread_and_is_idempotent() -> None:
    """stop() 必须真的把线程 join 掉（不是只置个标志就跑），且重复调用不出事。"""
    src = FakeSource([_pcm(1000)], repeat=True)
    p = _probe(lambda: src)
    p.start()
    _wait_for(lambda: p.chunks >= 1, what="采到块")
    p.stop()
    th = p._thread                                        # noqa: SLF001
    assert th is not None and not th.is_alive(), "stop() 返回后采集线程必须已经退出"
    assert p.running is False
    closed = src.close_calls
    assert closed >= 1, "stop() 必须释放设备"
    p.stop()
    assert src.close_calls == closed, "重复 stop() 不该反复关设备"
    print("  ✓ stop()：线程已 join（is_alive=False）、设备已关、重复调用幂等")


# ---------------------------------------------------------------- ② 界面接线（真窗口）

class _FakeGate:
    def __init__(self, level_db: float) -> None:
        self.level_db = level_db


class _FakeEngine:
    """只当「有引擎在跑」这个事实用：电平该来自它，探针必须让位。"""

    running = True

    def __init__(self, level_db: float = -33.0) -> None:
        self.input_gate = _FakeGate(level_db)


@contextlib.contextmanager
def _gui(fake_chunks=(), **src_kw):  # noqa: ANN001, ANN202
    """临时 HOME + 临时 config.yaml 起真窗口，并把 `vlt.gui.LevelProbe` 换成
    **注入假 opener** 的工厂 —— 界面里的判定逻辑走真的，设备一律是假的。

    产出 `(gui, state)`：`state["opens"]` = opener 被调的次数（用来断言「没另开一路」），
    `state["sources"]` = 每次开出来的假源（用来断言 close）。
    """
    import vlt.config as config_mod
    import vlt.gui as gui_mod

    open_error = src_kw.pop("open_error", None)
    state: dict = {"opens": 0, "sources": []}

    def _opener():
        state["opens"] += 1
        if open_error is not None:
            raise open_error
        src = FakeSource(fake_chunks, **src_kw)
        state["sources"].append(src)
        return src

    def _factory(**kw):  # noqa: ANN003
        kw.setdefault("recheck", None)           # 测试机器没有 PipeWire，别去查真图
        return LevelProbe(opener=_opener, read_timeout=0.05, **kw)

    saved_env = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")}
    saved_cfg = (config_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG, gui_mod.LevelProbe)
    env_tmp = Path(tempfile.mkdtemp(prefix="vlt-level-env-"))
    os.environ["USERPROFILE"] = str(env_tmp)
    os.environ["HOME"] = str(env_tmp)
    cfg_path = Path(tempfile.mkdtemp(prefix="vlt-level-cfg-")) / "config.yaml"
    cfg_path.write_text(CONFIG_BODY, encoding="utf-8")
    config_mod.DEFAULT_CONFIG = cfg_path
    gui_mod.DEFAULT_CONFIG = cfg_path
    gui_mod.LevelProbe = _factory                            # type: ignore[assignment]
    gui = None
    try:
        gui = gui_mod.TranslationGUI()
        if gui._update_check_job is not None:                # 别真去连 GitHub 查更新  # noqa: SLF001
            gui._root.after_cancel(gui._update_check_job)    # noqa: SLF001
            gui._update_check_job = None                     # noqa: SLF001
        gui._root.update()                                   # noqa: SLF001
        yield gui, state
    finally:
        if gui is not None:
            try:
                gui._stop_gate_probe()                       # noqa: SLF001
                gui._root.destroy()                          # noqa: SLF001
            except Exception:  # noqa: BLE001
                pass
        config_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG, gui_mod.LevelProbe = saved_cfg
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _i18n.set_language("zh")


def _pump(gui, seconds: float = 0.3) -> None:  # noqa: ANN001
    """推着 Tk 事件循环走一会儿（`_poll` 每 50ms 一跳，窗口映射也要事件才生效）。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        gui._root.update()                                   # noqa: SLF001
        time.sleep(0.02)


def _wait_gui(gui, pred, timeout: float = 3.0, what: str = "条件") -> None:  # noqa: ANN001, ANN202
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        gui._root.update()                                   # noqa: SLF001
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(f"等了 {timeout:g}s，{what} 仍未成立")


@contextlib.contextmanager
def _quiet():
    """把界面自己的日志吞掉（只留下本用例要看的那些）。"""
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        yield buf


def _open(gui) -> None:  # noqa: ANN001
    """打开设置窗（界面自己的日志吞掉；要数日志行数的用例自己 redirect）。"""
    with _quiet():
        gui._open_settings()                                 # noqa: SLF001
        gui._root.update()                                   # noqa: SLF001


def _wait_probe(gui, state, n: int = 1):  # noqa: ANN001, ANN202
    """等探针起来**并且假设备真的开出来了**，返回 `(probe, source)`。

    ⚠️ 不能只等 `probe.running`：它在 `start()` 里就置位了，那一刻 opener 可能还没被
    线程调到 —— 只等 running 的用例会在慢机器上偶发红（本次就踩到了）。
    """
    _wait_gui(gui, lambda: gui._gate_probe is not None               # noqa: SLF001
              and gui._gate_probe.running                            # noqa: SLF001
              and state["opens"] >= n and len(state["sources"]) >= n,
              what=f"探针起来并开出第 {n} 路假设备")
    return gui._gate_probe, state["sources"][n - 1]                  # noqa: SLF001


def _label(gui) -> str:  # noqa: ANN001, ANN202
    return str(gui._gate_level_lbl.cget("text"))             # noqa: SLF001


def test_probe_idle_until_window_visible_and_checked() -> None:
    """★ 核心：窗口关着不跑；窗口打开 + 勾了「启用」就跑起来，**没在翻译也出读数**。"""
    with _gui([_pcm(3277)], repeat=True) as (gui, state):
        # ① 刚起界面：设置窗还是 withdraw 的 → 一路采集都不许开
        with _quiet():
            _pump(gui, 0.3)
        assert gui._gate_probe is None, "窗口没开就不该有探针"        # noqa: SLF001
        assert state["opens"] == 0, f"窗口没开却开了 {state['opens']} 路采集"
        assert gui._gate_probe_wanted() is False                     # noqa: SLF001
        idle_label = _label(gui)
        assert idle_label == "—", f"没采集时读数应是「—」：{idle_label!r}"

        # ② 打开设置窗（配置默认就勾着「启用」）→ 探针起来、读数变成 dB
        assert gui._gate_enabled_var.get() is True, "前提：默认启用门限"   # noqa: SLF001
        _open(gui)
        _wait_probe(gui, state, 1)
        assert state["opens"] == 1, f"应该正好开一路：{state['opens']}"
        assert not gui._engines, "前提：没在翻译"
        with _quiet():
            # ⚠️ 不能只等「不是 —」：探针一 running 界面就有读数了，而第一块可能还没读到
            # （level_db 还是地板值 -120 → 画成 "-70 dB"）。要等到注入块的已知值为止。
            _wait_gui(gui, lambda: _label(gui) == "-20 dB", what="电平条出读数（-20 dB）")
        live_label = _label(gui)
        assert live_label.endswith("dB"), f"读数不对：{live_label!r}"
        shown = float(live_label.split()[0])
        assert abs(shown - round(_db_of(3277))) <= 1.0, \
            f"读数应与 -20dBFS 相符：界面 {live_label!r}"
        # 门限 -45、电平 -20 ⇒ 已超门限（这段会被翻译），条子该涂成 ACCENT
        assert gui._gate_level_hold > -45.0                          # noqa: SLF001
    print(f"  ✓ 窗口关着不采（opener 0 次、读数 {idle_label!r}）；"
          f"打开+勾选 → 采一路、读数 {live_label!r}（没在翻译）")


def test_uncheck_stops_and_releases_device() -> None:
    """★ 取消勾选 → 立刻停、假设备被 close；重新勾选 → 再开一路。"""
    with _gui([_pcm(3277)], repeat=True) as (gui, state):
        _open(gui)
        _wait_probe(gui, state, 1)
        first = state["sources"][0]
        probe = gui._gate_probe                                      # noqa: SLF001

        gui._gate_enabled_var.set(False)                             # noqa: SLF001
        with _quiet():
            gui._on_gate_change()                                    # noqa: SLF001
        assert gui._gate_probe is None, "取消勾选后探针必须被摘掉"     # noqa: SLF001
        assert probe.running is False, "取消勾选后必须停止采集"
        assert first.close_calls >= 1, "★ 假设备没被 close（等于还占着声卡）"
        assert not probe._thread.is_alive(), "线程必须被 join 掉"     # noqa: SLF001
        with _quiet():
            _pump(gui, 0.3)                    # 多推几跳：不许自己爬起来
        assert state["opens"] == 1, f"取消勾选后又开了新的一路：{state['opens']}"
        off_label = _label(gui)
        assert off_label == "—", f"没采集时读数应回到「—」：{off_label!r}"

        gui._gate_enabled_var.set(True)                              # noqa: SLF001
        with _quiet():
            gui._on_gate_change()                                    # noqa: SLF001
            _wait_probe(gui, state, 2)
        assert state["opens"] == 2, f"重新勾选应再开一路：{state['opens']}"
    print(f"  ✓ 取消勾选 → 停 + join + 设备 close（{first.close_calls} 次）、读数回「—」；"
          f"重新勾选 → 再开一路（opener 共 {state['opens']} 次）")


def test_close_window_stops_and_never_reopens() -> None:
    """★ 关设置窗 → 立刻停并释放设备；窗口关着时**绝不许**还开着采集。"""
    with _gui([_pcm(3277)], repeat=True) as (gui, state):
        _open(gui)
        _wait_probe(gui, state, 1)
        probe = gui._gate_probe                                      # noqa: SLF001
        src = state["sources"][0]

        with _quiet():
            gui._close_settings()                                    # noqa: SLF001
        assert gui._gate_probe is None, "关窗后探针必须被摘掉"        # noqa: SLF001
        assert probe.running is False and src.close_calls >= 1, \
            f"关窗后必须停止采集并释放设备（running={probe.running} close={src.close_calls}）"
        assert not probe._thread.is_alive(), "关窗后线程必须已经退出"  # noqa: SLF001
        assert gui._gate_probe_wanted() is False                     # noqa: SLF001

        # 窗口关着推 1 秒（≈10 跳 `_poll` 的兜底同步）：不许偷偷再开一路
        with _quiet():
            _pump(gui, 1.0)
        assert state["opens"] == 1, f"窗口关着却又开了采集：{state['opens']} 次"
        assert gui._gate_probe is None                               # noqa: SLF001
    print(f"  ✓ 关窗 → 停 + 线程退出 + 设备 close（{src.close_calls} 次）；"
          f"关着推 1s 也没再开（opener 仍 {state['opens']} 次）")


def test_engine_wins_and_no_second_stream() -> None:
    """★ 有引擎在跑 → **一路都不许另开**；电平来自引擎的 `input_gate.level_db`。"""
    with _gui([_pcm(3277)], repeat=True) as (gui, state):
        gui._engines = [_FakeEngine(level_db=-33.0)]                 # noqa: SLF001
        gui._engine_dirs = ["theirs"]                                # noqa: SLF001
        try:
            _open(gui)
            with _quiet():
                _pump(gui, 0.6)          # 多推几跳，给「偷偷开第二路」暴露的机会
                assert state["opens"] == 0, \
                    f"★ 正在翻译时又开了 {state['opens']} 路 loopback（会与引擎抢端点）"
                assert gui._gate_probe is None, "有引擎时不该有探针"   # noqa: SLF001
                assert gui._gate_probe_wanted() is False              # noqa: SLF001
                assert gui._gate_level_db() == -33.0, \
                    f"电平应来自引擎：{gui._gate_level_db()!r}"       # noqa: SLF001
                _wait_gui(gui, lambda: _label(gui) == "-33 dB", what="界面显示引擎的电平")

                # 停止翻译 → 电平交回探针（这时才允许开那一路）
                gui._engines, gui._engine_dirs = [], []              # noqa: SLF001
                gui._sync_gate_level_probe()                         # noqa: SLF001
                _wait_probe(gui, state, 1)
                assert state["opens"] == 1, f"应正好开一路：{state['opens']}"
                _wait_gui(gui, lambda: _label(gui) == "-20 dB", what="界面显示探针的电平")
        finally:
            gui._engines, gui._engine_dirs = [], []                  # noqa: SLF001
    print("  ✓ 翻译中：opener 0 次、电平取自引擎（-33 dB）；停翻译后探针接管（-20 dB）")


def test_open_failure_shows_dash_and_logs_once() -> None:
    """★ 端到端的失败路径：开不了设备 → 读数「—」+ 只留一行日志（不刷屏）。

    注意与「不重试」的区别：探针会在后台按 `RETRY_S`（默认 5s）低频重试，所以
    1s 的窗口里 opener 只该被调 1 次；关窗重开会把等待中的探针整个丢掉再建一个。
    """
    with _gui(open_error=OSError("模拟：没有可采集的系统输出")) as (gui, state):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            gui._open_settings()                                     # noqa: SLF001
            gui._root.update()                                       # noqa: SLF001
            _wait_gui(gui, lambda: gui._gate_probe is not None        # noqa: SLF001
                      and gui._gate_probe.last_error is not None, what="探针失败留痕")  # noqa: SLF001
            _pump(gui, 1.0)                  # 兜底同步跑 ~10 跳：不许重试刷日志
        finally:
            sys.stdout = old
        out = buf.getvalue()
        assert state["opens"] == 1, \
            f"1s 内不该重试（RETRY_S=5s）：opener 被调 {state['opens']} 次"
        assert out.count("[level] ❌") == 1, f"失败日志应正好一行：{out!r}"
        probe = gui._gate_probe                                      # noqa: SLF001
        assert probe is not None and probe.last_error, "失败原因要留着（last_error）"
        fail_label = _label(gui)
        assert fail_label == "—", f"失败时读数必须是「—」：{fail_label!r}"
        assert gui._gate_level_db() is None, "失败时不许假装有电平"     # noqa: SLF001

        # 用户重开设置窗 → 会重新建一个探针再试一次
        with _quiet():
            gui._close_settings()                                    # noqa: SLF001
            _open(gui)
            _wait_gui(gui, lambda: state["opens"] == 2, what="重开设置窗后重建探针再试")
    print(f"  ✓ 开设备失败：读数「—」、[level] ❌ 只 1 行、1s 内 opener 只 1 次；"
          f"重开设置窗重建（共 {state['opens']} 次）")


def test_gui_dash_until_first_chunk() -> None:
    """★ F3 的界面侧：探针起来了但还没读到块 → 读数「—」（不是假的 -70 dB）。"""
    with _gui() as (gui, state):                 # 假源永不产块：read 一直超时
        _open(gui)
        _wait_probe(gui, state, 1)
        with _quiet():
            _pump(gui, 0.5)
        assert gui._gate_probe.has_data is False                     # noqa: SLF001
        assert gui._gate_level_db() is None, "没读到块就不该有电平"     # noqa: SLF001
        dash_label = _label(gui)
        assert dash_label == "—", f"第一块之前读数应是「—」：{dash_label!r}"
    print("  ✓ F3（界面）：探针在跑但没读到块 → 读数「—」")


def test_peak_hold_semantics_unchanged() -> None:
    """★ 峰保语义不变：仍只在界面那一处做，每 100ms 掉 1.5dB；无来源时回「—」。

    探针的 `level_db` 与引擎的 `input_gate.level_db` 一样是「最近一块的瞬时值」，
    两边各做一层峰保会双重衰减 —— 这条用例把「只有一层」钉住。

    ⚠️ 断言期间**不能**泵事件循环：`_poll` 每 100ms 也会刷一次电平，掺进来就数不准了。
    """
    with _gui([_pcm(3277)], repeat=True) as (gui, state):
        _open(gui)
        _wait_probe(gui, state, 1)

        class _Stub:                    # 直接喂已知电平，避开线程时序
            running = True
            has_data = True
            level_db = -20.0

        stub = _Stub()
        gui._gate_probe.stop()                                        # noqa: SLF001
        gui._gate_probe = stub                                        # noqa: SLF001
        gui._gate_level_hold = LEVEL_FLOOR_DB                         # noqa: SLF001
        gui._refresh_gate_level()                                     # noqa: SLF001
        assert abs(gui._gate_level_hold - (-20.0)) < 1e-6, \
            f"第一跳应直接跳到当前电平：{gui._gate_level_hold}"         # noqa: SLF001
        assert _label(gui) == "-20 dB", f"读数应是 -20 dB：{_label(gui)!r}"

        stub.level_db = LEVEL_FLOOR_DB      # 声音停了 → 峰保按 1.5dB/100ms 衰减
        for i in range(1, 5):
            gui._refresh_gate_level()                                 # noqa: SLF001
            want = max(-20.0 - 1.5 * i, LEVEL_FLOOR_DB)
            assert abs(gui._gate_level_hold - want) < 1e-6, \
                f"第 {i} 跳峰保应是 {want:.1f}：{gui._gate_level_hold}"  # noqa: SLF001
        decayed = _label(gui)
        assert decayed == "-26 dB", f"读数应跟着峰保走：{decayed!r}"

        gui._gate_probe = None                                        # noqa: SLF001
        gui._refresh_gate_level()                                     # noqa: SLF001
        gone = _label(gui)
        assert gone == "—", f"没有来源时必须回到「—」：{gone!r}"
        assert gui._gate_level_hold == LEVEL_FLOOR_DB                  # noqa: SLF001
    print(f"  ✓ 峰保仍只在界面一处做：跳到当前值后每 100ms 掉 1.5dB（-20 → {decayed}）；"
          f"无来源回 {gone!r}")


def test_hint_text_and_five_languages() -> None:
    """提示文案改口 + 五语齐全（zh 原文为 key，四套词表都得有这一条）。

    还钉一条容易漂的：「启用」在各语种里的写法必须与该语种**已有的勾选框文案**一致
    （同一种语言里两处对「启用」的写法不同 = 用户找不到该勾哪个）。
    """
    import importlib

    from vlt.i18n import t

    zh = "只有响度超过门限的声音才会被翻译；改完立刻生效（勾选「启用」后这里显示实时电平）"
    src = (ROOT / "vlt" / "gui.py").read_text(encoding="utf-8")
    assert zh in src, "gui.py 里的提示文案没改成「勾选「启用」后…」"
    assert "开始翻译后这里显示实时电平" not in src, "旧文案还留在 gui.py 里"

    check_zh = "启用 —— 低于门限的声音不翻译（滤掉远处说话小声的玩家）"
    want_word = {"en": "Enable", "ja": "有効", "ko": "사용", "ru": "Включить"}
    for code, word in want_word.items():
        cat = importlib.import_module(f"vlt.locales.{code}").STRINGS
        assert zh in cat, f"{code}.py 缺这条提示的词条"
        assert cat[zh] != zh, f"{code}.py 这条是中文原文（没翻）"
        assert word in cat[zh], f"{code} 的提示里没提到「{word}」：{cat[zh]!r}"
        assert word in cat[check_zh], \
            f"{code} 的勾选框文案里没有「{word}」—— 两处用词对不上：{cat[check_zh]!r}"
    try:
        for code, word in want_word.items():
            _i18n.set_language(code)
            assert word in t(zh), f"{code} 下 t() 没取到词条"
    finally:
        _i18n.set_language("zh")
    assert t(zh) == zh, "zh 下应原样返回中文原文"
    print("  ✓ 提示文案改口 + 五语齐全（en/ja/ko/ru 的「启用」用词与勾选框各自一致）")


# ---------------------------------------------------------------- 入口

def main() -> int:
    tests = [
        test_probe_reports_level_of_known_pcm,
        test_probe_level_matches_engine_recipe,
        test_silence_timeout_returns_to_floor,
        test_open_failure_logs_once_and_retries_low_frequency,
        test_open_failure_recovers_when_target_appears,
        test_recheck_reopens_when_targets_change,
        test_recheck_to_empty_keeps_waiting_then_recovers,
        test_recheck_empty_baseline_never_shows_stale_stream,
        test_has_data_cleared_before_reopen_finishes,
        test_no_fake_level_before_first_chunk,
        test_open_level_source_linux_branch,
        test_read_error_leaves_trace_and_closes_device,
        test_stop_joins_thread_and_is_idempotent,
        test_probe_idle_until_window_visible_and_checked,
        test_uncheck_stops_and_releases_device,
        test_close_window_stops_and_never_reopens,
        test_engine_wins_and_no_second_stream,
        test_open_failure_shows_dash_and_logs_once,
        test_gui_dash_until_first_chunk,
        test_peak_hold_semantics_unchanged,
        test_hint_text_and_five_languages,
    ]
    print("test_level_probe:")
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
