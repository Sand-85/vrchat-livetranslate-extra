#!/usr/bin/env python
"""Linux「VRChat 音频」采集的离线验收（末影猫口径，2026-09）。

## 要钉住的行为

Linux 上「别人说话」这条腿的采集目标**只能是 VRChat 自己的音频输出流**，而且要
**把 VRChat 开的每一路都抓全**：

  1. `find_vrchat_output_streams()` 只认 `Stream/Output/Audio` 且名字含 `vrchat`
     的节点 —— 既不会把 `Audio/Sink`（系统输出设备）误当 VRChat，
     也不会把别的应用流（Google Chrome…）误当 VRChat；并且**返回全部**匹配（实测 2 路）。
  2. 每路都用 `object.serial` 标识 —— `pw-record --target=` 只认「序列号或名称」，
     而这些节点的 `node.name` 全是 `VRChat.exe`，按名字区分不了多个流
     （实测传 node id 会被当成名字，落空后回落到默认目标）。
  3. 找不到 VRChat 时返回空 → 采集腿**等待**，**绝不回落**系统默认输出
     （抓默认 sink 会把音乐/浏览器/系统提示音当成「游戏内语音」）。
  4. 多路各开一条 `pw-record`，用 `MixedAudioSource` **相加限幅**混成一路。
  5. 采集腿是「**等 → 采 → 流变化 → 再等**」的循环：VRChat 没跑就空转等待，
     中途退出或播放流增减都会结束当前采集并重新接上。
  6. Linux 设置窗口只剩「麦克风」一个下拉（VRChat 音频 / 译音输出两个下拉隐藏）。

全程离线：假 pw-dump / 假后端 / 假采集源，不碰真实 PipeWire、不声明虚拟声卡。

跑法：.venv/bin/python tests/test_vrchat_capture.py（GUI 用例在无显示器机器上要 xvfb-run）
"""
from __future__ import annotations

import array
import asyncio
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.engine import LoopbackTarget  # noqa: E402


# ---------------------------------------------------------------- 假数据 / 小工具


def _node(nid: int, media_class: str, **props) -> dict:
    return {"id": nid, "type": "PipeWire:Interface:Node",
            "info": {"props": {"media.class": media_class, **props}}}


def _core(rate: int = 48000) -> dict:
    return {"id": 0, "type": "PipeWire:Interface:Core",
            "info": {"props": {"default.clock.rate": rate}}}


def _vrchat_stream(nid: int, serial: int, media_name: str, *, corked: bool = False) -> dict:
    return _node(nid, "Stream/Output/Audio",
                 **{"node.name": "VRChat.exe", "application.name": "VRChat.exe",
                    "media.name": media_name, "object.serial": serial,
                    "audio.channels": 2, "pulse.corked": corked})


#: 真实机器上 `pw-dump` 的简化快照：VRChat **两路**播放流 + 系统 sink + 别的应用流。
FAKE_DUMP = [
    _core(),
    _vrchat_stream(199, 14002, "audio stream #1"),
    _vrchat_stream(147, 14061, "audio stream #5", corked=True),
    _node(131, "Stream/Input/Audio",
          **{"node.name": "VRChat.exe", "application.name": "VRChat.exe"}),
    _node(50, "Audio/Sink", **{"node.name": "wivrn.sink", "node.description": "WiVRn"}),
    _node(60, "Stream/Output/Audio",
          **{"node.name": "Google Chrome", "application.name": "Google Chrome"}),
]


def _stream_row(nid: int, serial: int, name: str) -> dict:
    return {"index": nid, "name": name, "node_name": "VRChat.exe", "serial": str(serial),
            "defaultSampleRate": 48000, "maxInputChannels": 2,
            "media_class": "Stream/Output/Audio"}


class _FakeDevBackend:
    """假的设备后端：只提供 VRChat 流查询（复刻 Windows 缺此能力的形状）。"""

    def __init__(self, streams: list[dict] | None) -> None:
        self._streams = streams or []

    def find_vrchat_output_streams(self) -> list[dict]:
        return list(self._streams)


class _FakeSource:
    """假的采集源：每次 read 吐一块同值样本，直到被 close。"""

    rate = 16000
    channels = 1

    def __init__(self, value: int = 1, samples: int = 1600) -> None:
        self._chunk = b"".join(int(value).to_bytes(2, "little", signed=True)
                               for _ in range(samples))
        self.reads = 0
        self.closed = False

    async def read(self, timeout: float = 1.0) -> bytes | None:
        if self.closed:
            return None
        await asyncio.sleep(0.005)
        self.reads += 1
        return self._chunk

    def close(self) -> None:
        self.closed = True


class _ScriptedSource:
    """按脚本逐块吐样本值的采集源（用完就静音）——复刻「远处小声 → 近处大声」的时序。

    `values` 里每个数是一块 100ms（1600 样点）的样本值：整块恒值 ⇒ 块的 RMS = 该值，
    电平 = 20·log10(值/32768) dBFS。脚本放完返回 None（=「还活着但暂时没声音」）。
    """

    rate = 16000
    channels = 1

    def __init__(self, values: list[int], samples: int = 1600) -> None:
        self._values = list(values)
        self._samples = samples
        self._i = 0
        self.closed = False

    async def read(self, timeout: float = 1.0) -> bytes | None:
        if self.closed or self._i >= len(self._values):
            return None
        v = self._values[self._i]
        self._i += 1
        await asyncio.sleep(0)
        return b"".join(int(v).to_bytes(2, "little", signed=True)
                        for _ in range(self._samples))

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self) -> None:
        self.bytes = 0
        self.pcm = b""

    async def send_audio(self, pcm: bytes) -> None:
        self.bytes += len(pcm)
        self.pcm += pcm


class _FakeCaptureBackend:
    """每路 target 各给一个源（按 target.id 区分，模拟多条 pw-record）。

    `values[tid]` 给**定值** → 每块都吐同一个样本值（`_FakeSource`）；
    给**列表** → 按脚本逐块吐（`_ScriptedSource`），用来构造「小声 → 大声」的时序。
    """

    def __init__(self, values: dict[str, int | list[int]] | None = None) -> None:
        self._values = values or {}
        self.sources: dict[str, _FakeSource | _ScriptedSource] = {}
        self.opened: list[LoopbackTarget] = []

    def open_loopback(self, target, *, blocksize):  # noqa: ANN001, ANN201
        self.opened.append(target)
        src = self._sources_for(target.id)
        return src

    def _sources_for(self, tid: str) -> _FakeSource | _ScriptedSource:
        if tid not in self.sources:
            v = self._values.get(tid, 1)
            self.sources[tid] = (_ScriptedSource(v) if isinstance(v, list)
                                 else _FakeSource(v))
        return self.sources[tid]


def _patch(obj, name, value):
    """临时替换属性，返回还原函数（避免 unittest.mock 依赖）。"""
    orig = getattr(obj, name)
    setattr(obj, name, value)
    return lambda: setattr(obj, name, orig)


# ---------------------------------------------------------------- ① 定位 VRChat 输出流


def test_find_all_vrchat_streams() -> None:
    """认出 VRChat 的**全部** `Stream/Output/Audio`，且带上 serial。"""
    from vlt.platform import linux

    restore = _patch(linux, "_pw_dump", lambda force=False: FAKE_DUMP)
    try:
        rows = linux.find_vrchat_output_streams()
    finally:
        restore()

    assert len(rows) == 2, f"应认出两路 VRChat 播放流：{rows}"
    assert [r["index"] for r in rows] == [147, 199], f"应按 node id 稳定排序：{rows}"
    ser = {r["index"]: r["serial"] for r in rows}
    assert ser == {147: "14061", 199: "14002"}, f"serial 不对：{ser}"
    assert all(r["node_name"] == "VRChat.exe" and r["media_class"] == "Stream/Output/Audio"
               for r in rows), f"形状不对：{rows}"
    assert all(r["maxInputChannels"] == 2 and r["defaultSampleRate"] == 48000 for r in rows)
    print("  认出全部 VRChat 播放流（2 路，带 serial）OK")


def test_find_absent_returns_empty() -> None:
    """没有 VRChat 流 / 没有 PipeWire 时返回空列表（调用方据此等待）。"""
    from vlt.platform import linux

    dump = [o for o in FAKE_DUMP if "VRChat" not in str(o.get("info", {}))]
    restore = _patch(linux, "_pw_dump", lambda force=False: dump)
    try:
        assert linux.find_vrchat_output_streams() == []
    finally:
        restore()

    def _boom(force=False):  # noqa: ANN001, ARG001
        raise linux.PipeWireUnavailable("no pipewire")

    restore = _patch(linux, "_pw_dump", _boom)
    try:
        assert linux.find_vrchat_output_streams() == []
    finally:
        restore()
    print("  无 VRChat / 无 PipeWire → 空列表 OK")


# ---------------------------------------------------------------- ② 目标解析（用 serial，且不回落）


def test_pick_targets_uses_serial() -> None:
    """Linux：把每路流映射成 LoopbackTarget，`id` 用 **serial**（不是 node.name）。"""
    import vlt.engine as E
    from vlt import platform

    backend = _FakeDevBackend([
        _stream_row(147, 14061, "VRChat.exe (audio stream #5)"),
        _stream_row(199, 14002, "VRChat.exe (audio stream #1)"),
    ])
    r1 = _patch(platform, "IS_LINUX", True)
    r2 = _patch(platform, "device_backend", lambda: backend)
    try:
        targets = E.pick_vrchat_targets()
    finally:
        r2()
        r1()

    assert [t.id for t in targets] == ["14061", "14002"], \
        f"id 必须是 serial（按名字区分不了两个 VRChat.exe）：{targets}"
    assert targets[0].sample_rate == 48000 and targets[0].channels == 2

    # 找不到 → 空列表（**不是**默认 sink，也不是第一个 sink）
    backend2 = _FakeDevBackend(None)
    r1 = _patch(platform, "IS_LINUX", True)
    r2 = _patch(platform, "device_backend", lambda: backend2)
    try:
        assert E.pick_vrchat_targets() == [], "找不到 VRChat 时不应回落任何设备"
    finally:
        r2()
        r1()
    print("  serial 定向 + 找不到不回落 OK")


def test_pick_targets_none_off_linux() -> None:
    """非 Linux 平台没有这个概念 → 空（Windows 走原来的 pick_loopback_target）。"""
    import vlt.engine as E
    from vlt import platform

    restore = _patch(platform, "IS_LINUX", False)
    try:
        assert E.pick_vrchat_targets() == []
    finally:
        restore()
    print("  非 Linux → 空 OK")


# ---------------------------------------------------------------- ③ 多路混音


def test_mixed_source_sums_and_clips() -> None:
    """多路混音 = 逐样本相加并限幅（不是取其一，也不爆音）。"""
    from vlt.platform.audio import MixedAudioSource

    a, b, c = _FakeSource(1000), _FakeSource(2000), _FakeSource(30000)

    def _samples(pcm: bytes) -> set:
        arr = array.array("h")
        arr.frombytes(pcm)
        return set(arr)

    async def go():
        m = MixedAudioSource([a, b])
        return await m.read()
    mixed = asyncio.run(go())
    assert _samples(mixed) == {3000}, f"两路应逐样本相加：{_samples(mixed)}"
    assert len(mixed) == len(a._chunk), "混音后长度应与最长那路一致"

    async def go_clip():
        m2 = MixedAudioSource([c, _FakeSource(30000)])
        return await m2.read()
    clipped = asyncio.run(go_clip())
    assert _samples(clipped) == {32767}, f"相加超范围应限幅到 32767：{_samples(clipped)}"

    # 一路静音（全 0）时结果不变
    async def go_quiet():
        m3 = MixedAudioSource([_FakeSource(7), _FakeSource(0)])
        return await m3.read()
    assert _samples(asyncio.run(go_quiet())) == {7}

    m4 = MixedAudioSource([a, b])
    m4.close()
    assert a.closed and b.closed, "close() 应关掉每一路"
    print("  多路相加限幅、单路透传、逐路收尾 OK")


# ---------------------------------------------------------------- ④ 等待 / 再等待 / 流变化


def test_wait_returns_targets_when_vrchat_appears() -> None:
    """等 VRChat：一开始没有 → 报「等待」；出现后返回**全部**路。"""
    import vlt.engine as E

    calls = {"n": 0}
    targets = [LoopbackTarget(id="14061", name="s#5", sample_rate=48000, channels=2),
               LoopbackTarget(id="14002", name="s#1", sample_rate=48000, channels=2)]

    def _find():  # noqa: ANN202
        calls["n"] += 1
        return targets if calls["n"] >= 3 else []

    statuses: list[tuple[str, str]] = []
    restore = _patch(E, "pick_vrchat_targets", _find)
    try:
        got = asyncio.run(E._wait_for_vrchat(threading.Event(),
                                             lambda k, m: statuses.append((k, m)),
                                             poll_s=0.01))
    finally:
        restore()

    assert got == targets, f"应返回全部目标：{got}"
    assert any("等待 VRChat" in m for _, m in statuses), f"没报「等待」：{statuses}"
    print("  等 VRChat 出现 OK（等待有提示，出现即返回全部路）")


def test_wait_stops_immediately() -> None:
    """停止翻译时，等待循环必须立刻退出（返回 None），不能卡住收尾。"""
    import vlt.engine as E

    stop = threading.Event()
    stop.set()
    restore = _patch(E, "pick_vrchat_targets", lambda: [])
    try:
        got = asyncio.run(E._wait_for_vrchat(stop, poll_s=0.01))
    finally:
        restore()
    assert got is None, f"stop 置位后应立即返回 None：{got}"
    print("  停止时等待循环立刻退出 OK")


def test_capture_ends_when_vrchat_exits() -> None:
    """VRChat 退出（流全消失）→ 采集主动收尾；两路都要被关掉。"""
    import vlt.engine as E
    from vlt import platform

    targets = [LoopbackTarget(id="14061", name="s#5", sample_rate=48000, channels=2),
               LoopbackTarget(id="14002", name="s#1", sample_rate=48000, channels=2)]
    cap = _FakeCaptureBackend({"14061": 0, "14002": 3000})
    session = _FakeSession()

    checks = {"n": 0}

    def _find():  # noqa: ANN202
        checks["n"] += 1
        return targets if checks["n"] == 1 else []      # 第二次回查：VRChat 没了

    r1 = _patch(platform, "capture_backend", lambda: cap)
    r2 = _patch(E, "pick_vrchat_targets", _find)
    r3 = _patch(E, "VRCHAT_RECHECK_S", 0.05)
    try:
        sent, stopped = asyncio.run(
            E._pump_vrchat_capture(session, None, threading.Event(), targets))
    finally:
        r3()
        r2()
        r1()

    assert stopped is False, "不该报成「被停止」——是 VRChat 退出了"
    assert sent > 0 and session.bytes == sent, f"应把 PCM 送给会话：{sent}/{session.bytes}"
    assert len(cap.opened) == 2, f"两路都要各开一条记录：{cap.opened}"
    assert all(s.closed for s in cap.sources.values()), "每一路都必须被关掉"
    # 混音上游是 3000 + 0 → 送出去的样本应能读到 3000
    assert session.pcm[:2] == (3000).to_bytes(2, "little", signed=True), "送出的应是混音结果"
    print("  VRChat 退出 → 两路一起收尾 OK")


def test_capture_reopens_when_stream_set_changes() -> None:
    """播放流**增减**（不是全没）也算「变化」→ 收尾重开，避免漏掉新开的流。"""
    import vlt.engine as E
    from vlt import platform

    two = [LoopbackTarget(id="14061", name="s#5", sample_rate=48000, channels=2),
           LoopbackTarget(id="14002", name="s#1", sample_rate=48000, channels=2)]
    one = [two[1]]
    cap = _FakeCaptureBackend()
    checks = {"n": 0}

    def _find():  # noqa: ANN202
        checks["n"] += 1
        return two if checks["n"] == 1 else one          # 少了一路

    r1 = _patch(platform, "capture_backend", lambda: cap)
    r2 = _patch(E, "pick_vrchat_targets", _find)
    r3 = _patch(E, "VRCHAT_RECHECK_S", 0.05)
    try:
        _sent, stopped = asyncio.run(
            E._pump_vrchat_capture(_FakeSession(), None, threading.Event(), two))
    finally:
        r3()
        r2()
        r1()

    assert stopped is False, "流集合变化应结束本段采集（外层会重开），而不是被当成停止"
    assert all(s.closed for s in cap.sources.values()), "收尾时每一路都要关"
    print("  播放流增减 → 收尾重开 OK")


def test_loopback_linux_returns_to_waiting() -> None:
    """整条腿：检测到 → 采集 → 流变化 → 回到等待（VRChat 再开还能续上）。"""
    import vlt.engine as E

    targets = [LoopbackTarget(id="14061", name="s#5", sample_rate=48000, channels=2),
               LoopbackTarget(id="14002", name="s#1", sample_rate=48000, channels=2)]
    stop = threading.Event()
    statuses: list[tuple[str, str]] = []
    waits = {"n": 0}
    forwarded: dict = {}

    async def _fake_wait(stop_event, on_status=None, *, poll_s=2.0):  # noqa: ANN001, ANN202, ARG001
        waits["n"] += 1
        if waits["n"] == 1:
            return targets
        stop_event.set()                # 第二次等：用户点了「停止翻译」
        return None

    async def _fake_pump(session, tele, stop_event, targets, gate=None):  # noqa: ANN001, ANN202
        forwarded["gate"] = gate
        return 42, False

    gate = E._LevelGate(True, -45.0, 500.0, 250)          # noqa: SLF001
    r1 = _patch(E, "_wait_for_vrchat", _fake_wait)
    r2 = _patch(E, "_pump_vrchat_capture", _fake_pump)
    try:
        asyncio.run(E._run_loopback_linux(_FakeSession(), None, 0.0, stop,
                                          lambda k, m: statuses.append((k, m)),
                                          gate=gate))
    finally:
        r2()
        r1()

    kinds = " | ".join(m for _, m in statuses)
    assert forwarded["gate"] is gate, "Linux 采集腿没把输入门限转给采集泵"
    assert any("检测到 VRChat 音频" in m for _, m in statuses), f"没报检测到：{kinds}"
    assert any("VRChat 音频输出消失" in m for _, m in statuses), f"变化后没回到等待：{kinds}"
    assert waits["n"] == 2, f"应在流变化后**再次等待**：{waits}"
    print("  采集腿「等 → 采 → 变化 → 再等」OK（门限一路带到采集泵）")


# ---------------------------------------------------------------- ⑥ 输入门限（在混音之后判）

CHUNK = 3200            # 100ms @16kHz s16le 单声道 = 1600 样点


def test_run_loopback_forwards_gate_to_linux_leg() -> None:
    """run_loopback 在 Linux 上必须把 gate 转给 _run_loopback_linux。

    #12 的现场：调用点传了 gate，而 `run_loopback()` 的形参里没有它 →
    一启动就 TypeError（上层显示「运行错误」），这条腿整条不工作。
    """
    import vlt.engine as E
    from vlt import platform

    seen: dict = {}

    async def fake_linux(session, tele, seconds, stop_event, on_status, **kw):  # noqa: ANN001, ANN003
        seen.update(kw)

    gate = E._LevelGate(True, -45.0, 500.0, 250)          # noqa: SLF001
    r1 = _patch(platform, "IS_LINUX", True)
    r2 = _patch(E, "_run_loopback_linux", fake_linux)
    try:
        asyncio.run(E.run_loopback(_FakeSession(), None, gate=gate))
    finally:
        r2()
        r1()
    assert seen.get("gate") is gate, f"Linux 早退分支没把输入门限带下去：{seen}"
    print("  run_loopback → _run_loopback_linux 带上 gate OK")


def test_capture_gate_blocks_quiet_and_keeps_preroll_on_mix() -> None:
    """★ 门限判在**多路混音之后**：小声整段不上送，越阈值才开闸并补句首。

    两路各按脚本吐 5 块（100ms/块）：
      3 块小声（各 50 → 混音后 100 ≈ −50 dBFS，低于默认 −45）→ 全部拦下；
      2 块大声（各 3277 → 混音后 6554 ≈ −14 dBFS）→ 第 1 块开闸：补发最近 250ms 的
      preroll（前 2 块）再上送当前块，第 2 块起逐块透传。
    """
    import vlt.engine as E
    from vlt import platform

    targets = [LoopbackTarget(id="14061", name="s#5", sample_rate=16000, channels=1),
               LoopbackTarget(id="14002", name="s#1", sample_rate=16000, channels=1)]
    script = [50, 50, 50, 3277, 3277]

    def run(gate):  # noqa: ANN001, ANN202
        cap = _FakeCaptureBackend({"14061": list(script), "14002": list(script)})
        session = _FakeSession()
        checks = {"n": 0}

        def _find():  # noqa: ANN202
            checks["n"] += 1
            return targets if checks["n"] == 1 else []      # 第 2 次回查：流没了 → 收尾

        r1 = _patch(platform, "capture_backend", lambda: cap)
        r2 = _patch(E, "pick_vrchat_targets", _find)
        r3 = _patch(E, "VRCHAT_RECHECK_S", 0.05)
        try:
            sent, stopped = asyncio.run(
                E._pump_vrchat_capture(session, None, threading.Event(), targets,
                                       gate=gate))
        finally:
            r3()
            r2()
            r1()
        return sent, stopped, session, cap

    gate = E._LevelGate(True, -45.0, 500.0, 250)              # noqa: SLF001
    sent, stopped, session, cap = run(gate)
    assert stopped is False, "流变化不该被当成「停止」"
    assert len(cap.opened) == 2, f"两路都要各开一条记录：{cap.opened}"
    assert sent == session.bytes and sent > 0, \
        f"送出的字节数应与会话收到的一致：{sent}/{session.bytes}"
    assert gate.dropped_chunks == 3, f"3 块小声应全被拦下：{gate.dropped_chunks}"
    assert (gate.opened, gate.replay_count) == (1, 1), \
        f"应只开闸一次并补发一次 preroll：开闸 {gate.opened} / 补发 {gate.replay_count}"
    assert len(session.pcm) == 4 * CHUNK, \
        f"上送块数应为 4（补发 2 + 当前 1 + 大声 1）：{len(session.pcm) // CHUNK}"

    # 对照：不传门限（gate=None）→ 同一段脚本一块不漏（「无门限」的行为没被改动）
    _sent, _stopped, bare, _cap = run(None)
    assert len(bare.pcm) == 5 * CHUNK, \
        f"无门限时应原样上送全部 5 块：{len(bare.pcm) // CHUNK}"
    print("  门限接在混音之后：小声全拦、开闸补 preroll、无门限不受影响 OK")


# ---------------------------------------------------------------- ⑤ Linux 界面只剩麦克风


def test_linux_ui_hides_loopback_and_output_combos() -> None:
    """设置窗口：Linux 只留「麦克风」下拉；Windows 保持三个。"""
    import vlt.config as cfg_mod
    import vlt.gui as gui_mod
    from vlt import platform
    from vlt.gui import TranslationGUI

    saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME", "DASHSCOPE_API_KEY")}
    tmp = Path(tempfile.mkdtemp(prefix="vlt-vrc-cfg-"))
    os.environ["USERPROFILE"] = str(tmp)
    os.environ["HOME"] = str(tmp)
    os.environ.pop("DASHSCOPE_API_KEY", None)

    cfg = tmp / "config.yaml"
    cfg.write_text("ui:\n  lang: zh\n", encoding="utf-8")
    orig_cfg = cfg_mod.DEFAULT_CONFIG
    orig_gui_cfg = gui_mod.DEFAULT_CONFIG
    cfg_mod.DEFAULT_CONFIG = cfg
    gui_mod.DEFAULT_CONFIG = cfg

    gui = None
    try:
        gui = TranslationGUI()
        if gui._update_check_job is not None:
            gui._root.after_cancel(gui._update_check_job)
            gui._update_check_job = None
        gui._root.update()

        if platform.IS_LINUX:
            assert gui._linux_fixed_audio is True
            assert gui._mic_combo is not None, "麦克风下拉必须保留"
            assert gui._loopback_combo is None, "Linux 不该有「VRChat 音频」下拉"
            assert gui._audio_out_combo is None, "Linux 不该有「译音输出」下拉"
            texts: list[str] = []
            _walk_texts(gui._settings_win, texts)
            assert "VRChat 音频:" not in texts, f"设置里还留着 VRChat 音频下拉：{texts[-8:]}"
            assert "译音输出:" not in texts, f"设置里还留着译音输出下拉：{texts[-8:]}"
            print("  Linux 设置只剩「麦克风」下拉 OK")
        else:
            assert gui._linux_fixed_audio is False
            assert gui._loopback_combo is not None and gui._audio_out_combo is not None, \
                "非 Linux 必须保留三个下拉"
            print("  非 Linux 三个下拉保持原样 OK")
    finally:
        if gui is not None:
            try:
                gui._root.destroy()
            except Exception:  # noqa: BLE001
                pass
        cfg_mod.DEFAULT_CONFIG = orig_cfg
        gui_mod.DEFAULT_CONFIG = orig_gui_cfg
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _walk_texts(win, out: list[str]) -> None:
    for w in win.winfo_children():
        try:
            keys = w.keys()
        except Exception:  # noqa: BLE001
            continue
        if "text" in keys:
            out.append(str(w.cget("text")))
        _walk_texts(w, out)


if __name__ == "__main__":
    print("test_vrchat_capture:")
    test_find_all_vrchat_streams()
    test_find_absent_returns_empty()
    test_pick_targets_uses_serial()
    test_pick_targets_none_off_linux()
    test_mixed_source_sums_and_clips()
    test_wait_returns_targets_when_vrchat_appears()
    test_wait_stops_immediately()
    test_capture_ends_when_vrchat_exits()
    test_capture_reopens_when_stream_set_changes()
    test_loopback_linux_returns_to_waiting()
    test_run_loopback_forwards_gate_to_linux_leg()
    test_capture_gate_blocks_quiet_and_keeps_preroll_on_mix()
    test_linux_ui_hides_loopback_and_output_combos()
    print("ALL PASSED")
