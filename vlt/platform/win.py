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


# ---------------------------------------------------------------- 桌面叠加窗（issue #11）
#
# 桌面（非 VR）模式下的字幕窗：Tk 负责画，这里只提供 Tk 拿不到的那几件 Win32 事实 ——
# 顶层 HWND、目标窗口客户区、鼠标穿透/不抢焦点的扩展样式、工作区（主屏那一份，
# 以及**目标窗口所在显示器**那一份 —— 多屏时只有后者才不会把副屏字幕夹回主屏）。
#
# ⚠️ 共享模块（`vlt/output/desktop_overlay.py`）**只能通过 `vlt/platform/__init__.py`
#    的门面调这些函数**：另一侧平台没有对等实现，门面会返回安全默认值，
#    所以 Linux 侧一个文件都不用改（见 vlt/platform/__init__.py 的说明）。
#
# ⚠️ 所有函数都**不抛异常**：调用点在 50ms 一跳的 tick 里，抛出去会把整条翻译腿打断。
#    取不到就返回 None / False / 原值，由调用方决定怎么降级（并留一行日志）。

_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT = 0x00000020       # 鼠标穿透
_WS_EX_TOOLWINDOW = 0x00000080        # 不进 alt-tab
_WS_EX_LAYERED = 0x00080000           # 分层窗口（色键 / 整窗透明度都要它）
_WS_EX_NOACTIVATE = 0x08000000        # 显示时不抢焦点
_SPI_GETWORKAREA = 0x0030
_SM_CXSCREEN, SM_CYSCREEN = 0, 1
# 窗口不与任何显示器相交（移出屏幕外/正在销毁）时，`MonitorFromWindow` 仍返回**最近**
# 的那块屏 —— 比返回 NULL 再回落主屏更贴近用户的直觉（字幕就在那块屏附近）。
_MONITOR_DEFAULTTONEAREST = 2


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


def _get_ex_style(hwnd: int) -> int | None:
    """读窗口扩展样式（GWL_EXSTYLE）；失败返回 None。"""
    try:
        user32 = ctypes.windll.user32
        fn = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        fn.restype = ctypes.c_ssize_t
        fn.argtypes = [ctypes.c_void_p, ctypes.c_int]
        return int(fn(ctypes.c_void_p(int(hwnd)), _GWL_EXSTYLE)) & 0xFFFFFFFF
    except Exception:  # noqa: BLE001 — 32/64 位差异、句柄失效都走这里
        return None


def _set_ex_style(hwnd: int, style: int) -> bool:
    """写窗口扩展样式。**读回来确认**才算成功 —— SetWindowLongPtr 返回 0 既可能是
    「失败」也可能是「原值就是 0」，靠返回值判断会误报。"""
    try:
        user32 = ctypes.windll.user32
        fn = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        fn.restype = ctypes.c_ssize_t
        fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        fn(ctypes.c_void_p(int(hwnd)), _GWL_EXSTYLE, ctypes.c_ssize_t(int(style)))
    except Exception:  # noqa: BLE001
        return False
    return _get_ex_style(hwnd) == (int(style) & 0xFFFFFFFF)


def top_level_hwnd(widget_id: int) -> int:
    """Tk 的 `winfo_id()` 给的是**子窗口** HWND，顶层要再 `GetParent()` 一次。

    拿不到父窗口（本来就是顶层 / 句柄无效）就返回原值 —— 调用方要的是「能设扩展样式的
    那个窗口」，两种情况都满足。
    """
    try:
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        parent = user32.GetParent(ctypes.c_void_p(int(widget_id)))
        return int(parent) if parent else int(widget_id)
    except Exception:  # noqa: BLE001
        return int(widget_id)


def find_window_by_title(substr: str, exclude: Any = ()) -> int | None:
    """按标题子串（不区分大小写）找**可见的顶层窗口**；找不到返回 None。

    ⚠️ 标题**完全相等**的优先：本程序主窗口标题是「VRChat 实时同传」，也含 "VRChat" ——
    只取 Z 序第一个命中的话，游戏窗口没起来时会贴到**我们自己的界面**上。
    仍然保留子串匹配（用户可能把 VRChat 窗口改名 / 多开），只是让精确命中优先。

    `exclude`：要排除的窗口句柄（可迭代）。调用方把**自己的**窗口（本程序主窗
    与字幕窗）传进来 —— 精确匹配只能挡住「VRChat 实时同传」这种，字幕窗和未来
    别的自建窗不该靠标题去赌。
    """
    needle = (substr or "").strip().lower()
    if not needle:
        return None
    try:
        skip = {int(v) for v in (exclude or ()) if v}
    except TypeError:
        skip = {int(exclude)} if exclude else set()          # 传了单个 int 也认
    user32 = ctypes.windll.user32
    user32.IsWindowVisible.restype = ctypes.c_bool
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]

    exact: list[int] = []
    partial: list[int] = []

    def _cb(hwnd, _lparam) -> bool:
        try:
            if not hwnd or int(hwnd) in skip or not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
            low = title.lower()
            if low == needle:
                exact.append(int(hwnd))
                return False                    # 精确命中，不用再找了
            if needle in low:
                partial.append(int(hwnd))
        except Exception:  # noqa: BLE001 — 单个窗口读不到标题不影响其余
            pass
        return True

    try:
        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_cb)
        user32.EnumWindows(proc, 0)
    except Exception:  # noqa: BLE001
        return None
    return (exact or partial or [None])[0]


def window_client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口**客户区**在屏幕上的矩形 (left, top, right, bottom)；失败返回 None。

    用客户区而不是 GetWindowRect：字幕要贴在画面里，标题栏/边框那块不算。
    """
    try:
        user32 = ctypes.windll.user32
        user32.GetClientRect.restype = ctypes.c_bool
        user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
        user32.ClientToScreen.restype = ctypes.c_bool
        user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(_POINT)]
        rc = _RECT()
        if not user32.GetClientRect(ctypes.c_void_p(int(hwnd)), ctypes.byref(rc)):
            return None
        w, h = int(rc.right) - int(rc.left), int(rc.bottom) - int(rc.top)
        if w <= 0 or h <= 0:                    # 最小化 / 还没布局完
            return None
        pt = _POINT(int(rc.left), int(rc.top))
        if not user32.ClientToScreen(ctypes.c_void_p(int(hwnd)), ctypes.byref(pt)):
            return None
        return (int(pt.x), int(pt.y), int(pt.x) + w, int(pt.y) + h)
    except Exception:  # noqa: BLE001
        return None


def is_window(hwnd: int) -> bool:
    """句柄还是一个真窗口吗（游戏退了 / 换了场景就会变 False）。"""
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.IsWindow.restype = ctypes.c_bool
        user32.IsWindow.argtypes = [ctypes.c_void_p]
        return bool(user32.IsWindow(ctypes.c_void_p(int(hwnd))))
    except Exception:  # noqa: BLE001
        return False


def set_click_through(hwnd: int, on: bool) -> bool:
    """开/关鼠标穿透（`WS_EX_LAYERED | WS_EX_TRANSPARENT`）。

    ⚠️ 必须**读改写**：直接写死一个常量会把 Tk 的 `-transparentcolor` / `-alpha`
    依赖的 `WS_EX_LAYERED` 冲掉（字幕会变成一块实心矩形）。关穿透时也只摘
    `WS_EX_TRANSPARENT`，保留 LAYERED。
    """
    style = _get_ex_style(hwnd)
    if style is None:
        return False
    style |= _WS_EX_LAYERED
    style = (style | _WS_EX_TRANSPARENT) if on else (style & ~_WS_EX_TRANSPARENT)
    return _set_ex_style(hwnd, style)


def set_tool_window(hwnd: int) -> bool:
    """不抢焦点 + 不进 alt-tab（`WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`）。

    抢焦点的后果很具体：字幕窗一刷新就把输入焦点从游戏里夺走，用户打字/按键全丢。
    """
    style = _get_ex_style(hwnd)
    if style is None:
        return False
    return _set_ex_style(hwnd, style | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW)


def screen_work_area() -> tuple[int, int, int, int]:
    """主屏**工作区** (left, top, right, bottom)：不含任务栏，字幕不该被它挡住。

    `SPI_GETWORKAREA` 优先；拿不到（策略限制等）退 `GetSystemMetrics` 的整屏尺寸；
    再不行给一个 1920x1080 的保守值 —— 调用方会拿它做夹取，返回 (0,0,0,0) 会把
    字幕夹到左上角一个点，比给个粗略值更糟。
    """
    try:
        user32 = ctypes.windll.user32
        user32.SystemParametersInfoW.restype = ctypes.c_bool
        user32.SystemParametersInfoW.argtypes = [
            ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        rc = _RECT()
        if user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(rc), 0):
            if int(rc.right) > int(rc.left) and int(rc.bottom) > int(rc.top):
                return (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))
    except Exception:  # noqa: BLE001
        pass
    try:
        user32 = ctypes.windll.user32
        w = int(user32.GetSystemMetrics(SM_CXSCREEN))
        h = int(user32.GetSystemMetrics(SM_CYSCREEN))
        if w > 0 and h > 0:
            return (0, 0, w, h)
    except Exception:  # noqa: BLE001
        pass
    return (0, 0, 1920, 1080)


def monitor_work_area(hwnd: int) -> tuple[int, int, int, int]:
    """`hwnd` **所在那块显示器**的工作区；拿不到就回落到 `screen_work_area()`。

    多显示器时这条是必需的：`SPI_GETWORKAREA` 只有**主屏**那一份工作区，副屏上的
    字幕会被它夹回主屏 —— 左侧副屏的坐标本来就是负的，一夹就直接飞到主屏左上角。
    所以贴窗时按「目标窗口待着的那块屏」取工作区，才是用户眼里的那块屏。

    ⚠️ 返回值**可能是负坐标**（副屏在主屏左侧/上方）：不要做任何 `max(0, ...)` 钳制。
    """
    if not hwnd:
        return screen_work_area()
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetMonitorInfoW.restype = ctypes.c_bool
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
        mon = user32.MonitorFromWindow(ctypes.c_void_p(int(hwnd)),
                                       _MONITOR_DEFAULTTONEAREST)
        if not mon:
            return screen_work_area()
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)      # 忘了填这个 GetMonitorInfoW 直接失败
        if not user32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi)):
            return screen_work_area()
        left, top = int(mi.rcWork.left), int(mi.rcWork.top)
        right, bottom = int(mi.rcWork.right), int(mi.rcWork.bottom)
        if right <= left or bottom <= top:
            return screen_work_area()
        return (left, top, right, bottom)
    except Exception:  # noqa: BLE001 — 句柄失效 / 远程桌面 / 老系统都走这里
        return screen_work_area()
