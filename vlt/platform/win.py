"""Windows 平台实现（sounddevice / pyaudiowpatch / Win32 API）。

⚠️ 本模块是 **Windows 独占**：打包 Windows 版时它被收进产物；Linux 版通过
`--exclude-module vlt.platform.win` 剔除。反过来 Linux 版打进的是
`vlt/platform/linux.py`，Windows 产物里不含它的任何字样。

因此**不要**在共享代码里直接 `import vlt.platform.win`，一律走
`vlt/platform/__init__.py` 的门面（见那边的说明）。
"""
from __future__ import annotations

import ctypes
from typing import Any
import threading
import time
from pathlib import Path

from .audio import QueueAudioSource, SoundDeviceMicSource
from .base import PA_LOCK, AudioSource, LoopbackTarget

# ---------------------------------------------------------------- 设备枚举

def query_devices() -> list[dict]:
    """sounddevice 的设备表（麦克风 + 播放）。

    失败**不在这里吞**：调用方（`vlt/devices.py`）负责把异常翻成「空列表」，
    这样「枚举不到设备」与「库坏了」在日志里还分得开。
    """
    import sounddevice as sd
    with PA_LOCK:                    # PortAudio 串行（并发 init/destroy 会段错误）
        return [dict(d) for d in sd.query_devices()]


def query_loopback_devices() -> list[dict]:
    """WASAPI loopback 设备表（= 每个输出设备的一份「录音副本」）。

    这是 Windows 独有的能力，Linux 上对应的是 PipeWire 的 monitor source。
    """
    import pyaudiowpatch as pyaudio
    with PA_LOCK:
        p = pyaudio.PyAudio()
        try:
            return [dict(d) for d in p.get_loopback_device_info_generator()]
        finally:
            p.terminate()


def default_output_index() -> int:
    """系统默认**输出**设备在 pyaudiowpatch 里的 index；取不到返回 -1。

    用来在 loopback 列表里优先挑「默认输出设备」对应的那一份 ——
    用户把 VRChat 的声音切到别的声卡时，这一步决定我们抓的是不是同一路。
    """
    import pyaudiowpatch as pyaudio
    with PA_LOCK:
        p = pyaudio.PyAudio()
        try:
            return int(p.get_host_api_info_by_type(pyaudio.paWASAPI)
                       .get("defaultOutputDevice", -1))
        finally:
            p.terminate()


def device_info_by_index(index: int) -> dict:
    """按 index 取设备信息（拿名字用）；取不到返回 {}。"""
    import pyaudiowpatch as pyaudio
    with PA_LOCK:
        p = pyaudio.PyAudio()
        try:
            return dict(p.get_device_info_by_index(index))
        except Exception:  # noqa: BLE001 — 拿不到名字不是致命错，调用方有兜底
            return {}
        finally:
            p.terminate()


# ---------------------------------------------------------------- 字体

# 雅黑优先（原作者实测调好的观感），其余按「有中日韩字形」的顺序兜底。
# 打不开字体时 Pillow 会抛 OSError: cannot open resource —— 那会让整条手腕屏腿挂掉，
# 所以宁可回落到任意一个可用字体，也不要把路径写死。
_CJK_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",         # 微软雅黑（简体，默认）
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyhl.ttc",
    "C:/Windows/Fonts/NotoSansJP-VF.ttf",  # 装过 Noto 的话，日文更好看
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
)


def find_cjk_font() -> str | None:
    for cand in _CJK_FONT_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


# ---------------------------------------------------------------- 界面语言

# Windows 主语言 ID → 界面语言。表里没有的（德语/法语等已知但未支持的语言）按 en 接待；
# 这是本平台自己的口径，`vlt/platform/linux.py` 有对等的一张表（读环境变量）。
_PRIMARY_LANG: dict[int, str] = {0x04: "zh", 0x09: "en", 0x11: "ja", 0x12: "ko", 0x19: "ru"}


def detect_ui_language() -> str:
    """读 Windows 用户默认 UI 语言（`GetUserDefaultUILanguage`）。

    返回 LANGID（如 0x0804=zh-CN、0x0409=en-US），低 10 位是主语言 ID。
    取不到值/异常 → "en"：用户口径「不是支持的语言就显示英文」——
    外国用户按英文接待远比按中文合理；中文环境的 LANGID 恒为 0x04，
    检测正常时绝不会掉进这条兜底。
    """
    try:
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        primary = int(langid) & 0x3FF
    except Exception:  # noqa: BLE001 — 任何意外：不认识 → 英文
        return "en"
    return _PRIMARY_LANG.get(primary, "en")


# ---------------------------------------------------------------- 采集

class PyaudioLoopbackSource(QueueAudioSource):
    """WASAPI loopback 采集（pyaudiowpatch）。

    整段读线程逻辑是从 `vlt/engine.py` **原样搬过来的**，两处踩过的坑都保留：

    1. 必须用 `get_read_available()` **非阻塞轮询**，不能用阻塞的 `stream.read()`：
       WASAPI loopback 在端点没有音频在播时 `read()` 会一直不返回（实测 3 秒 0 帧、
       读线程永久卡住）。
    2. 收尾时「关流 / PortAudio terminate」与「卡住的读」相撞 → **访问违规**
       （用户实测点「停止翻译」闪退，退出码 139，faulthandler 抓到 pyaudiowpatch read）。
       所以关流前必须先把读线程 join 掉 —— 这件事现在由 `QueueAudioSource.close()`
       的顺序保证（置 stop → join → 才 `_teardown`）。
    """

    label = "loopback"

    def __init__(self, loop, *, device_index: int, name: str, rate: int,
                 channels: int) -> None:
        ch = min(2, channels or 2)
        super().__init__(loop, rate=rate, channels=ch)
        self.device_name = name
        import pyaudiowpatch as pyaudio

        self._chunk_max = int(rate * 0.1)
        self._pa = pyaudio.PyAudio()
        try:
            self._stream = self._pa.open(
                format=pyaudio.paInt16, channels=ch, rate=rate,
                frames_per_buffer=int(rate * 0.1), input=True,
                input_device_index=device_index)
        except Exception:
            self._pa.terminate()
            raise

    def _pump(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                avail = self._stream.get_read_available()
            except Exception:  # noqa: BLE001
                break
            if avail <= 0:
                time.sleep(0.01)
                continue
            try:
                data = self._stream.read(min(avail, self._chunk_max),
                                         exception_on_overflow=False)
            except Exception:  # noqa: BLE001
                break
            self._emit(data)

    def _teardown(self) -> None:
        try:
            self._stream.stop_stream()
            self._stream.close()
        finally:
            self._pa.terminate()


def open_mic(device_name: str | None, *, rate: int = 16000, channels: int = 1,
             blocksize: int) -> AudioSource:
    """麦克风采集（与 Linux 共用同一个实现）。

    ⚠️ **Windows 按 PortAudio 索引打开**（`device=<int>`），这是 v0.3.x 的口径，
    不要退化成「直接把名字丢给 sounddevice」：`sd.RawInputStream(device="名字")`
    用的是 PortAudio 自己的取名规则，与我们的 `resolve_device_name`（全名精确 →
    忽略大小写 → 去重子串）口径不同 —— 同名端点（两块同型号声卡 / 多个虚拟声卡）
    并存时，可能打开到与旧版不同的物理端点。

    索引由 `resolve_device_name(..., "input")` 从 **sounddevice 自己的设备表**解析，
    正是 PortAudio 要的那个；解析不到则回落默认输入设备（IndexError 交给我们）。
    """
    import asyncio
    import logging

    index: int | None = None
    if device_name:
        from ..devices import resolve_device_name   # 局部导入，避免与 devices 循环导入
        index = resolve_device_name(device_name, "input")
        if index is None:
            logging.getLogger(__name__).warning(
                "[mic] 未找到设备 %r，回退系统默认输入设备", device_name)

    src = SoundDeviceMicSource(asyncio.get_running_loop(), index,
                               rate=rate, channels=channels, blocksize=blocksize)
    src.start()
    return src


def open_loopback(target: LoopbackTarget, *, blocksize: int) -> AudioSource:
    """打开一路 WASAPI loopback 采集。`target.id` 是设备 index 的字符串形式。"""
    import asyncio

    src = PyaudioLoopbackSource(asyncio.get_running_loop(),
                                device_index=int(target.id), name=target.name,
                                rate=target.sample_rate, channels=target.channels)
    src.start()
    return src


# ---------------------------------------------------------------- 手腕屏后端

def create_wrist_overlay(cfg: Any, config_path: Any = None, dry_run: bool = False) -> Any:
    """手腕屏后端：Windows 用 pyopenvr 的 `IVROverlay`（`vlt/output/overlay.py`）。

    这个工厂放在平台模块里，是为了让共享代码（engine/gui）**不出现任何后端名字** ——
    Windows 产物里就不该有 openxr 的字样，反之亦然（见 vlt/platform/base.py 的说明）。
    """
    from ..output.openvr_overlay import WristOverlay
    return WristOverlay(cfg, config_path=config_path, dry_run=dry_run)
