"""Linux 麦克风代理：把「VRChat 里手动切麦克风」内化成程序内的一个二选一路由开关。

与 Windows 版（`vlt/output/micproxy.py`）**同构** —— 档位语义、环形缓冲、译音抖动缓冲、
欠载计数、麦克风直通线程、状态汇报全部继承，差别只在「输出驱动」这一层：

    Windows：sounddevice / PortAudio 回调（`sd.RawOutputStream`）
    Linux  ：`pw-cat --playback --target=vlt_mic_sink` 管道 + 写线程

## 为什么 Linux 要换输出驱动

Linux 的虚拟声卡不是用户事先装好的声卡，而是**运行时声明出来的 PipeWire 节点**
（`vlt_mic_sink` 的 `media.class=Audio/Sink/Internal`，见 `vlt/platform/linux.py`）——
按名字用 PortAudio 打开既不可靠也没必要；`pw-cat --playback --target=<节点名>` 才是
PipeWire 原生写法（这条路 `vlt/output/virtualmic.py:PwCatVirtualMic` 已经趟过）。

## 复用清单（踩过坑的逻辑只留一份）

    · 档位 / 欠载 / 缓冲容量 / 麦克风直通线程 / 译音抖动缓冲
      → 全部继承 Windows 版 `MicProxy`（本类只覆盖 start / close / 输出驱动）
    · **混音那一份算式**：直接调父类的 `_out_callback()`，只把「PortAudio 的 outdata」
      换成自备的 bytearray —— 保证两端「档位优先、欠载怎么算、不足怎么补静音」一字不差
    · 虚拟声卡节点的声明与回收 → `platform.linux.VirtualMicCable`（与译音输出同一条路）
    · pw-cat 管道 → `PwCatVirtualMic(provider=...)`；provider 模式**跳过起播兜时**
      （父类那套「先攒够 buffer_ms 才出声」是给 TTS 的，原声直通要立刻出声）

## 与「译音输出」勾选框的关系

代理启用时虚拟声卡由代理**独占**（一路 `pw-loopback` 声明节点 + 一路 `pw-cat` 写管道）。
引擎**绝不能再自建**译音输出 —— 同名节点会打架。所以引擎拿到的是
`proxy.translated_sink`（鸭子类型垫片）；它的生命周期归代理管，引擎停翻译时
不许 close（父类 `TranslatedSink.close()` 已保证这一点）。

「译音输出」勾选框的语义与 Windows 侧一致（见 `gui_voice.toggle_voice_mode`）：
不勾 = 译音不汇进虚拟麦；此时切到译音档会**只给一行警告**（「译音档会无声」），仍然切。

## 平台

仅 Linux 使用（由 `vlt/platform/linux.py:create_mic_proxy()` 构造）。
Windows 走 `vlt/output/micproxy.py` 的原版；打包时本模块会被
`--exclude-module` 剔除（见 `scripts/build_exe.py` 与 `scripts/check_platform_purity.py`）。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .micproxy import MODE_TRANSLATED, UNDERRUN_REPORT_S, MicProxy, _TranslatedBuffer
from .virtualmic import PwCatVirtualMic


class LinuxMicProxy(MicProxy):
    """Linux 版麦克风代理：`pw-loopback` 声明虚拟麦 + `pw-cat` 写管道驱动输出。"""

    def __init__(self, *, audio_cfg: dict, mic_name: str | None = None,
                 on_status: Callable[..., None] = lambda *_a, **_k: None) -> None:
        super().__init__(audio_cfg=audio_cfg, mic_name=mic_name, on_status=on_status)
        self._cable: Any = None
        self._out: PwCatVirtualMic | None = None
        # 诊断用（只打印一次 / 低频提醒）：「译音档选了、但引擎一直没数据进来」是最难
        # 自己判断的故障（用户听到的只是安静），所以这里两行日志专门让它自证。
        self._translated_seen = False
        self._translated_warn_at = 0.0

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> bool:
        """声明虚拟声卡 → 拉起 pw-cat 写管道 → 启动麦克风直通线程。

        任何一步失败都只降级（返回 False）并留痕，绝不抛异常 —— 与 Windows 版同一条纪律。
        """
        if self._opened or self._closed:
            return self._opened
        self._stop.clear()

        # 延迟 import：本模块只在 Linux 上被构造，但 `vlt/platform/linux.py` 同样是
        # 平台独占模块，放函数里 import 可让「模块级 import 图」保持平台干净。
        from ..platform.linux import VirtualMicCable

        cable = VirtualMicCable()
        if not cable.start():
            self._on_status("error",
                            "虚拟声卡声明失败 → 麦克风代理不可用（其余功能不受影响）")
            return False
        self._cable = cable

        # 译音抖动缓冲：借用父类那份（起播兜时 / 整句丢弃 / 超限丢最旧），只当数据缓冲用。
        self._translated = _TranslatedBuffer(
            device_index=0, device_name=cable.sink_name, sample_rate=self._sample_rate,
            buffer_ms=self._translated_ms, max_buffer_ms=self._max_buffer_ms,
            on_status=self._on_status,
        )

        self._out = PwCatVirtualMic(
            cable.sink_name, sample_rate=self._sample_rate,
            buffer_ms=self._translated_ms, max_buffer_ms=self._max_buffer_ms,
            on_status=self._on_status, provider=self._mix_block,
        )
        if not self._out.open():
            self._out = None
            self._translated = None
            cable.stop()
            self._cable = None
            return False

        self._thread = threading.Thread(target=self._mic_thread_run, daemon=True,
                                        name="vlt-micproxy")
        self._thread.start()
        self._opened = True
        return True

    def close(self) -> None:
        """幂等。顺序：停麦克风线程 → 停写线程 / 关 pw-cat → 回收虚拟声卡节点。

        ⚠️ 顺序不能反：先回收节点会让 pw-cat 的管道目标消失，正在播的那句被截断。
        """
        if self._closed:
            return
        self._closed = True
        self._opened = False
        self._stop.set()

        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=2.0)
        self._thread = None

        out = self._out
        self._out = None
        if out is not None:
            out.close()

        cable = self._cable
        self._cable = None
        if cable is not None:
            cable.stop()

    # ---------------------------------------------------------------- 输出（混音）

    def _mix_block(self, need: int) -> bytes:
        """给 `PwCatVirtualMic` 的 provider：产出恰好 `need` 字节。

        **直接复用父类 `_out_callback` 的算式**（译音档走抖动缓冲 / 原声档走环形缓冲 /
        不足补静音 / 计欠载），只把 outdata 换成自备的 bytearray —— Linux 与 Windows
        两条路的输出语义因此一字不差。

        `need` 恒为 20ms 立体声 s16le（`PwCatVirtualMic._chunk`），故帧数 = need/4。
        """
        buf = bytearray(need)
        self._out_callback(buf, need // 4, None, None)

        # ── 诊断：译音档到底有没有东西可放（这类故障听起来只是「安静」，最难自己判断）──
        if self._mode == MODE_TRANSLATED:
            if not self._translated_seen and any(buf):
                self._translated_seen = True
                print("[proxy] ✅ 译音已开始写入虚拟麦（对方此时应当听到译音）", flush=True)
            elif not self._translated_seen:
                now = time.monotonic()
                if now - self._translated_warn_at >= UNDERRUN_REPORT_S:
                    self._translated_warn_at = now
                    print("[proxy] ⚠️ 已切到「译音」档，但还没有任何译音数据进来 —— "
                          "检查主界面「译音输出」是否勾选（不勾则引擎根本不往这里送）；"
                          "若刚切过来，等下一句译文即可", flush=True)
        return bytes(buf)
