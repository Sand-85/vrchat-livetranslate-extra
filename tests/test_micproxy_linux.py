"""Linux 麦克风代理（`LinuxMicProxy`）纯逻辑测试：**绝不碰用户的 PipeWire 音频图**。

## ⚠️ 打桩纪律（与 `test_micproxy.py` / `test_virtualmic.py` 同一条）

`LinuxMicProxy.start()` 会**真的声明一对 PipeWire 节点**（`pw-loopback`），动的是用户
正在用的音频图。本文件里：

  · 除「测试进程守卫」那一条外，其余用例都只在**构造出来、没 start()** 的实例上验证纯逻辑
    （档位门 / 混音算式 / 缓冲参数 / close 幂等）—— 构造函数不碰任何外部资源，
    所以没装 PipeWire 的机器上这些用例也照跑（CI 就是）；
  · 「测试进程守卫」那条**故意**调 `start()`，用来钉住 `_test_process_guard()`：
    测试进程里必须返回 False，并且**不留任何句柄**（这条坑本仓库实测踩过四次）。

## 本文件同时守「两端输出语义一字不差」

`LinuxMicProxy._mix_block()` **复用父类 `MicProxy._out_callback()` 的算式**（档位优先、
欠载计数、不足补静音），只把 outdata 换成自备的 bytearray。所以这里对 `_mix_block` 的
断言，等价于在守「Linux `pw-cat` 管道与 Windows PortAudio 回调的输出语义一致」。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vlt import platform  # noqa: E402
from vlt.output.micproxy import (  # noqa: E402
    MODE_PASSTHROUGH,
    MODE_TRANSLATED,
    _TranslatedBuffer,
)
from vlt.output.micproxy_linux import LinuxMicProxy  # noqa: E402

#: 20ms 的 48kHz 立体声 s16le —— `PwCatVirtualMic._chunk` 就是这么大
CHUNK = int(48000 * 0.02) * 2 * 2


def _mk(**cfg_kw) -> LinuxMicProxy:
    """构造一个**未 start()** 的 LinuxMicProxy（构造函数不碰 PipeWire）。"""
    audio_cfg = {"sample_rate": 48000, "buffer_ms": 100, "max_buffer_ms": 2000,
                 "proxy": {"enabled": True, "passthrough_buffer_ms": 150}}
    audio_cfg.update(cfg_kw)
    return LinuxMicProxy(audio_cfg=audio_cfg)


def _mk_with_translated(p: LinuxMicProxy, buffer_ms: int = 100) -> None:
    """手动挂上译音抖动缓冲（start() 才会建的东西）—— 与 Windows 侧用例同一手法。"""
    p._translated = _TranslatedBuffer(
        device_index=0, device_name="vlt_mic_sink", sample_rate=48000,
        buffer_ms=buffer_ms, max_buffer_ms=2000)


# ---------------------------------------------------------------- 平台门面

def test_platform_facade_returns_linux_proxy() -> None:
    """Linux 上 `platform.create_mic_proxy()` 必须给 LinuxMicProxy；close 幂等。"""
    if not platform.IS_LINUX:
        print("  [skip] 非 Linux：门面应返回 Windows 版 MicProxy")
        return
    p = platform.create_mic_proxy({"sample_rate": 48000, "buffer_ms": 300}, None, None)
    assert type(p).__name__ == "LinuxMicProxy", f"门面给错了实现：{type(p).__name__}"
    p.close()                      # 没 start 过 → 空操作
    p.close()                      # 幂等
    print("  平台门面：create_mic_proxy → LinuxMicProxy；close 幂等 OK")


# ---------------------------------------------------------------- 测试进程守卫

def test_start_refused_in_test_process() -> None:
    """★ 测试进程里 `start()` 必须被守卫拒掉，且**不留任何句柄**。

    `VirtualMicCable.start()` 第一件事就是 `_test_process_guard()`（在任何 Popen 之前），
    所以这里连 PipeWire 都不会碰一下。
    """
    p = _mk()
    assert p.start() is False, "测试进程里必须拒绝声明虚拟声卡"
    assert p._cable is None, "被拒后不许留下 cable 句柄"
    assert p._out is None, "被拒后不许留下输出管道句柄"
    p.close()
    print("  测试进程守卫：start() 被拒 + 零句柄 + close 安全 OK")


# ---------------------------------------------------------------- 混音算式（两种档位）

def test_mix_passthrough_pads_and_counts_underrun() -> None:
    """原声档：环形缓冲有数据就原样排出；不够就补静音并计欠载；输出**恒为 chunk 字节**。"""
    p = _mk()
    assert p.mode == MODE_PASSTHROUGH, "出厂档位必须是原声档"

    out = p._mix_block(CHUNK)
    assert len(out) == CHUNK and out == b"\x00" * CHUNK, "空缓冲应补满静音"
    assert p._underruns == 1, f"空缓冲应计一次欠载，实际 {p._underruns}"

    p._ring.push(b"\x11" * CHUNK)
    out = p._mix_block(CHUNK)
    assert out == b"\x11" * CHUNK, "有数据时应原样排出"
    assert p._underruns == 1, "这一块没欠载，不该计数"

    p._ring.push(b"\x22" * 100)               # 只有 100B，远不够一块
    out = p._mix_block(CHUNK)
    assert out[:100] == b"\x22" * 100, "先给已有的"
    assert out[100:] == b"\x00" * (CHUNK - 100), "剩下的补静音"
    assert p._underruns == 2, "补了静音就要计欠载"
    print("  原声档：原样排出 / 补静音 / 欠载计数 OK")


def test_mix_translated_uses_jitter_buffer() -> None:
    """译音档：走译音抖动缓冲；没攒够起播线 → 全静音；起播后出数据。"""
    p = _mk()
    _mk_with_translated(p, buffer_ms=100)
    p.set_translation_active(True)
    assert p.set_mode(MODE_TRANSLATED) is True

    out = p._mix_block(CHUNK)
    assert out == b"\x00" * CHUNK, "没攒够起播线 → 全静音（且不该去动直通缓冲）"

    p._ring.push(b"\x77" * CHUNK)              # 直通缓冲里有东西：译音档下不该被读走
    p._translated.push(b"\x33" * 20000)        # 攒够 100ms 起播线（19200B）
    out = p._mix_block(CHUNK)
    assert out == b"\x33" * CHUNK, "起播后应出译音数据，而不是直通缓冲里的数据"
    print("  译音档：抖动缓冲（未起播补静音 / 起播后出数据 / 不串直通）OK")


# ---------------------------------------------------------------- 档位门

def test_mode_gate_and_translation_fallback() -> None:
    """译音档只在翻译运行时允许；翻译一停必须回落原声档（Windows 侧同一条语义）。"""
    p = _mk()
    _mk_with_translated(p)

    assert p.set_mode(MODE_TRANSLATED) is False, "翻译没跑就不许切译音档"
    assert p.mode == MODE_PASSTHROUGH, "被拒后应保持原声档"

    p.set_translation_active(True)
    assert p.set_mode(MODE_TRANSLATED) is True
    assert p.mode == MODE_TRANSLATED

    p.set_translation_active(False)
    assert p.mode == MODE_PASSTHROUGH, "停翻译必须强制回落原声档"
    assert p.set_mode(MODE_TRANSLATED) is False, "回落之后同样不许切回去"
    print("  档位门：未翻译拒绝 / 运行中允许 / 停翻译回落 OK")


# ---------------------------------------------------------------- 缓冲参数

def test_reopen_with_updates_both_buffers() -> None:
    """设置页改缓冲 → `reopen_with` 更新两个软件侧参数（不重开输出流，无静音间隙）。"""
    p = _mk()
    _mk_with_translated(p, buffer_ms=300)
    p.reopen_with(400, 800)
    assert p._passthrough_ms == 400, f"直通缓冲没更新：{p._passthrough_ms}"
    assert p._translated is not None and p._translated._buffer_ms == 800, \
        "译音缓冲没更新"
    print("  reopen_with：两端缓冲参数都更新 OK")


def test_reopen_mic_swaps_input_without_touching_cable_or_output() -> None:
    """★ Linux：`reopen_mic` 只重启麦克风采集线程 —— pw-loopback 声明与 pw-cat 输出都不动。

    Linux 走的是 `LinuxMicProxy`（覆写了 `start/close`），但麦克风线程与 `reopen_mic` 都在
    基类 `MicProxy` 里，所以换麦不会重声明虚拟声卡、也不影响引擎手里的 `translated_sink`。
    这条用例把「两端共享同一段换麦逻辑」钉在 Linux 侧（Windows 侧见 test_micproxy.py）。
    """
    import time

    class _Rec:
        def __init__(self) -> None:
            self.names: list = []

        def open_mic(self, name, *, rate, channels, blocksize):   # noqa: ARG002
            self.names.append(name)

            class _Src:
                rate, channels = 16000, 1

                async def read(self, timeout: float = 1.0):       # noqa: ARG002
                    import asyncio
                    await asyncio.sleep(0.01)
                    return None

                def close(self) -> None:
                    pass

            return _Src()

    class _Handle:
        def __init__(self) -> None:
            self.closed = 0
            self.stopped = 0

        def close(self) -> None:
            self.closed += 1

        def stop(self) -> None:
            self.stopped += 1

    p = _mk()
    p._opened = True                          # 假装已 start()（不真声明 PipeWire 节点）
    p._cable = _Handle()                      # noqa: SLF001
    p._out = _Handle()                        # noqa: SLF001
    sink_before = p.translated_sink
    rec = _Rec()
    orig = platform.capture_backend
    platform.capture_backend = lambda: rec    # type: ignore[assignment]
    try:
        p.reopen_mic("Mic L")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and rec.names[:1] != ["Mic L"]:
            time.sleep(0.02)
        assert rec.names[:1] == ["Mic L"], f"Linux 侧没按新名开麦：{rec.names}"
        assert p._cable.stopped == 0 and p._cable.closed == 0, "换麦不该动 pw-loopback 声明"
        assert p._out.closed == 0, "换麦不该动 pw-cat 输出管道"
        assert p.translated_sink is sink_before, "换麦不该换 translated_sink（引擎桥接不变）"
    finally:
        p._stop.set()
        if p._thread is not None:
            p._thread.join(timeout=2.0)
        platform.capture_backend = orig       # type: ignore[assignment]
    print("  reopen_mic（Linux）：只重启采集线程、pw-loopback / pw-cat 不动 OK")


if __name__ == "__main__":
    print("test_micproxy_linux:")
    test_platform_facade_returns_linux_proxy()
    test_start_refused_in_test_process()
    test_mix_passthrough_pads_and_counts_underrun()
    test_mix_translated_uses_jitter_buffer()
    test_mode_gate_and_translation_fallback()
    test_reopen_with_updates_both_buffers()
    test_reopen_mic_swaps_input_without_touching_cable_or_output()
    print("ALL PASSED")
