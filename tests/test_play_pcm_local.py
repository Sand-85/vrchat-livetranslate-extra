#!/usr/bin/env python
"""TTS「试听」的播放路径（`_play_pcm_local`）：Linux 走 `pw-cat`，不再经过 PortAudio。

## 为什么（2026-10）

Linux 上 PortAudio 的最后一个用途就是 TTS 试听（`sd.play`）。麦克风改走 `pw-record` 后，
把试听也迁到 `pw-cat --playback` 就能让 Linux 产物**整个不打包 sounddevice/PortAudio**
（`--exclude-module sounddevice`），少一个「缺可选包就静默失效」的隐藏依赖。

本文件钉住：Linux 分支的 argv（`pw-cat --playback --format=s16 --rate=24000 --channels=1
--raw -`）、阻塞语义（`input=pcm`）、非零退出**抛错不静默**、空 PCM 不动作。全程打桩，不出声。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _Res:
    def __init__(self, rc: int = 0, err: bytes = b"") -> None:
        self.returncode = rc
        self.stderr = err


def _patch(calls: dict, *, rc: int = 0, err: bytes = b""):
    import vlt.platform as P
    import vlt.ui_text as U  # noqa: F401  （确保模块已加载）

    orig = (subprocess.run, P.IS_LINUX, P.child_env)

    def _run(argv, **kw):                 # noqa: ANN001, ANN202
        calls["argv"] = argv
        calls["kw"] = kw
        return _Res(rc, err)

    subprocess.run = _run                 # type: ignore[assignment]
    P.IS_LINUX = True                     # type: ignore[assignment]
    P.child_env = lambda: {"PATH": "/usr/bin"}   # type: ignore[assignment]
    return orig


def _restore(orig) -> None:               # noqa: ANN001
    import vlt.platform as P
    subprocess.run, P.IS_LINUX, P.child_env = orig


def test_linux_plays_via_pw_cat() -> None:
    """Linux：argv 正确、以 stdin 喂 PCM（阻塞等价 sounddevice 的 blocking=True）。"""
    import vlt.ui_text as U
    calls: dict = {}
    orig = _patch(calls)
    try:
        U._play_pcm_local(b"\x00\x00" * 240)      # 240 采样 @24k = 10ms
    finally:
        _restore(orig)
    argv = calls.get("argv") or []
    assert argv[:1] == ["pw-cat"], f"Linux 应走 pw-cat：{argv}"
    assert "--playback" in argv and "--raw" in argv, f"缺少播放/裸 PCM 开关：{argv}"
    assert "--rate=24000" in argv and "--channels=1" in argv and "--format=s16" in argv, argv
    assert calls["kw"].get("input") == b"\x00\x00" * 240, "PCM 没经 stdin 喂进去"
    assert calls["kw"].get("timeout") == 60, "应设超时兜底"
    assert "sounddevice" not in (calls["kw"].get("env") or {}), "不该把 sounddevice 相关塞进 env"
    print(f"  Linux 试听走 pw-cat OK：{argv}")


def test_linux_playback_failure_raises() -> None:
    """非零退出 → 抛错（不静默）—— 调用方会翻成「试听失败」。"""
    import vlt.ui_text as U
    calls: dict = {}
    orig = _patch(calls, rc=1, err=b"failed to open")
    try:
        try:
            U._play_pcm_local(b"\x00\x00" * 24)
        except RuntimeError as exc:
            assert "pw-cat" in str(exc) and "failed to open" in str(exc), exc
        else:
            raise AssertionError("pw-cat 非零退出时必须抛错，不能静默")
    finally:
        _restore(orig)
    print("  pw-cat 非零退出 → 抛错（不静默）OK")


def test_empty_pcm_is_noop() -> None:
    """空 PCM：直接返回，不 spawn 任何进程。"""
    import vlt.ui_text as U
    calls: dict = {}
    orig = _patch(calls)
    try:
        U._play_pcm_local(b"")
    finally:
        _restore(orig)
    assert "argv" not in calls, f"空 PCM 不该起进程：{calls}"
    print("  空 PCM → 不动作 OK")


if __name__ == "__main__":
    print("test_play_pcm_local:")
    test_linux_plays_via_pw_cat()
    test_linux_playback_failure_raises()
    test_empty_pcm_is_noop()
    print("ALL PASSED")
