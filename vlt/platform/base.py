"""平台抽象层的协议定义（纯协议 + 无副作用，不 import 任何平台库）。

## 为什么要有这一层

Windows 与 Linux 做「同一件事」用的是完全不同的机制：

| 能力 | Windows | Linux |
|---|---|---|
| 麦克风采集 | sounddevice（WASAPI） | sounddevice（ALSA → PipeWire） |
| 系统声采集（听别人说话） | pyaudiowpatch（WASAPI loopback） | `pw-record` 抓 `<sink>` 的 monitor |
| 译音虚拟声卡 | PortAudio 打开 VB-Cable / VoiceMeeter | PipeWire 运行期声明 + `pw-cat` 写入 |
| 手腕屏 | pyopenvr 的 IVROverlay | 自建 OpenXR overlay（XR_EXTX_overlay + EGL_MNDX） |
| 可写目录 | `%APPDATA%` | `$XDG_DATA_HOME` |
| 打开文件夹 | `os.startfile` | `xdg-open` |

这一层的目标是：**让共享代码（engine / gui / app）看不到平台差异**，
并让平台独占的实现各自独立成模块，从而能在打包时被 `--exclude-module` 干净剔除
（见 `scripts/build_exe.py` 的 `EXCLUDE_WIN` 与 AppImage 构建脚本的反向排除）。
「Windows 产物里不允许出现 pipewire / openxr 相关内容」这条靠
「模块边界 + 构建排除 + 自动校验」结构性保证，不靠人记。

## 边界约定（重要，别破坏）

平台模块**只提供原始数据，不做业务解释**：

* 设备列表一律返回与 sounddevice / pyaudiowpatch **同形状的 dict**
  （如 `{"name", "max_input_channels", "max_output_channels", "default_samplerate"}`）。
  `DeviceInfo` 的转换、名称解析、回退链等纯逻辑留在 `vlt/devices.py` ——
  那边有现成的离线测试（`tests/test_devices.py`），换平台不该动它，也不该让测试
  依赖真实硬件。
* 这样也避免了 `devices.py` ↔ `platform` 的循环导入。

## 音频流的统一形状

* **采集侧是拉模型**：`AudioSource.read()` 阻塞到凑够字节或超时。
  Windows 用 PortAudio 回调 + 内部队列适配，Linux 直接读子进程 stdout ——
  于是 `engine.py` 里那段 `await asyncio.wait_for(queue.get(), timeout=1.0)` 的
  循环一行都不用改。
* **播放侧是推模型**：`AudioSink.write()` 推入即返回，内部自己缓冲
  （虚拟声卡的抖动缓冲/整句丢弃逻辑在 `vlt/output/virtualmic.py`，与平台无关）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# PortAudio 的初始化/销毁是**进程级且线程绑定**的资源（WASAPI 走 COM 单元）。
# 历史教训（都是实测出来的）：
#   1) 用后台线程做枚举 → sounddevice 在扫描线程 Pa_Initialize、却在主线程
#      Pa_Terminate（atexit 钩子）→ 退出时报
#      `Tcl_AsyncDelete: async handler deleted by the wrong thread`，更早的写法直接段错误
#   2) 多个线程并发 PyAudio()/terminate() → 进程退出时段崩溃
# 所以：**设备枚举一律在主线程同步做**（实测冷启动 348ms、预热后 23ms，完全可接受），
# 这个锁只是防止将来有人又把它挪回线程里。
#
# 放在 base.py 而不是各平台模块里：两个平台的「麦克风枚举」都走 sounddevice →
# 必须是**同一把锁**，否则各持一把等于没锁。
PA_LOCK = threading.Lock()


@dataclass
class LoopbackTarget:
    """「抓哪个系统输出」的平台内标识。

    Windows：pyaudiowpatch 的设备 index；Linux：PipeWire 的 sink 名。
    两者都存字符串 —— Linux 侧根本没有「设备 index」这个概念，
    而 Windows 侧把 index 转成字符串也无损。
    """
    id: str
    name: str
    sample_rate: int
    channels: int


@runtime_checkable
class AudioSource(Protocol):
    """一路采集流（麦克风或系统声）。

    `read()` 是**协程**：内部把回调（Windows）或子进程 stdout（Linux）统一成
    队列，引擎侧只管 `await`。超时返回 `None`（「还活着但暂时没数据」），
    由调用方决定继续等还是认为流已结束。

    `rate` / `channels` 描述 `read()` 吐出来的 PCM 格式，供调用方做重采样
    （`vlt/engine.py:to_16k_mono`）。
    """

    rate: int
    channels: int

    async def read(self, timeout: float = 1.0) -> bytes | None: ...

    def close(self) -> None: ...


@runtime_checkable
class AudioSink(Protocol):
    """一路播放流（译音写往虚拟声卡）。"""

    def write(self, pcm: bytes) -> None: ...

    def buffered_ms(self) -> float: ...

    def close(self) -> None: ...


@runtime_checkable
class DeviceBackend(Protocol):
    """设备枚举。返回的 dict 形状必须与对应平台库一致（见模块说明）。"""

    def query_devices(self) -> list[dict]: ...

    def query_loopback_devices(self) -> list[dict]: ...


@runtime_checkable
class CaptureBackend(DeviceBackend, Protocol):
    """采集流工厂。

    `open_mic` 的设备口径按平台定：**Linux 传名字、Windows 传 PortAudio 索引**
    （名字怎么变索引由 Windows 的 `open_mic` 自己解析，调用方只管把用户配的
    设备名交进来）。`open_loopback` 用 `LoopbackTarget` 抹平「Windows 是设备 index、
    Linux 是 sink 名」的差异。两者都返回 `AudioSource`，调用方不感知底层是
    PortAudio 还是子进程。
    """

    def default_output_index(self) -> int: ...

    def device_info_by_index(self, index: int) -> dict: ...

    def open_mic(self, device_name: str | None, *, rate: int = 16000,
                 channels: int = 1, blocksize: int) -> AudioSource: ...

    def open_loopback(self, target: LoopbackTarget, *, blocksize: int) -> AudioSource: ...


@runtime_checkable
class DesktopWindow(Protocol):
    """**原生桌面叠加窗**的窗口契约（Linux 的 Wayland / X11 两个后端共同遵守）。

    共享模块（`vlt/output/desktop_overlay.py`）只认这套鸭子接口：门面
    （`vlt.platform.create_desktop_window`）拿到的对象、以及将来任何新后端，
    都要把下面的能力补齐 —— 缺哪个，对应的调用点就会炸给它看。

    与 Tk 那条腿的关系：Tk 窗是**回落路径**（Windows / 没有原生能力的会话），
    行为对等但实现完全不同，不走本契约。

    ⚠️ 所有方法都在 GUI 线程被高频调用（`tick()` 每 50ms 一跳），实现里不许阻塞；
    `close()` 幂等（重复调用不报错）。
    """

    #: 建起来了吗（建窗失败时必须为 False，调用方据此丢弃对象、回落 Tk）
    available: bool

    def set_panel(self, image: Any, alpha: float | None = None) -> None:
        """贴一帧 RGBA 面板。`alpha` 是整层乘子（0~1）；**None = 保持当前值**。"""

    def move(self, x: int, y: int) -> None:
        """移到屏幕坐标（X11 语义；Wayland 后端自己换算到输出局部坐标系）。"""

    def set_size(self, size: tuple[int, int]) -> None:
        """改面板像素尺寸（配置热重载用）。"""

    def set_alpha(self, alpha: float) -> None:
        """整层透明度（0~1）。"""

    def set_click_through(self, on: bool) -> None:
        """鼠标穿透开关（拖动时临时关掉）。"""

    def set_draggable(self, on: bool) -> None:
        """解锁拖动；拖完由建窗时给的 `on_drag_end(x, y)` 回调报落点。"""

    def tick(self) -> None:
        """泵事件 + 出图（GUI 每 50ms 一跳）。"""

    @property
    def position(self) -> tuple[int, int]:
        """当前屏幕坐标。"""

    def close(self) -> None:
        """销毁窗口；幂等。"""
