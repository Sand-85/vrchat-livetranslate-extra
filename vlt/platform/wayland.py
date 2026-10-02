"""Wayland 原生桌面叠加窗（Linux 独占）：ctypes 直调 libwayland-client 的 layer-shell 窗。

## 为什么需要它（Tk 那条腿在 Wayland 上为什么不行）

桌面字幕窗原先用 Tk（`vlt/output/desktop_overlay.py`）。Windows 上 Tk 的
`-transparentcolor` 能把整窗底色抠成透明；但 **X11 的 Tk 根本没有这个属性**，
而 Wayland 会话里 Tk 走 XWayland，经 `xwayland-satellite` 时更糟：

* 合成缓冲固定 `Xrgb8888`（**没有 alpha 通道**，源码 `src/server/decoration.rs`）；
* `_NET_WM_WINDOW_OPACITY` 没人处理（全仓库无此属性）；
* X Shape 蒙版也没有处理 → 鼠标穿透/圆角抠形全部无效。

于是「色键透明」与「整窗透明度」双双失效 —— 字幕在 niri 上就是一块不透明矩形，
还会被按平铺窗口管理。要让像素**真的**透过去，唯一的路是让字幕窗自己成为原生
Wayland 客户端，用 `wl_shm`（ARGB8888，预乘 alpha）出图。

## 为什么是 layer-shell

普通 `xdg_toplevel` 由合成器决定位置与堆叠，Wayland 协议层不允许客户端自己定位。
`zwlr_layer_shell_v1`（overlay 层）一次给齐三件事：

1. **置顶**：overlay 层在所有窗口之上（含全屏窗口）；
2. **定位**：`anchor=top|left` + `set_margin(top, 0, 0, left)` 把面放到输出上的任意坐标；
3. **输入语义**：`wl_surface.set_input_region(空)` = 协议级鼠标穿透，不赌合成器是否实现 EWMH。

## 覆盖范围（运行时探测，不写白名单）

niri / sway / Hyprland / KWin 6.x / labwc / Cage / gamescope 等实现 layer-shell 的合成器
走本后端；GNOME(Mutter) / Weston 这类没实现的由 `vlt/platform/linux.py` 探测后自动
回落 Tk 路径（并打日志说明）。

## 协议绑定为什么是手写的

libwayland-client 只导出**通用**入口（`wl_proxy_marshal_array_flags` 等）和核心协议的
**接口数据**（`wl_surface_interface` 一类是导出的 data 符号）；请求帮助函数
（`wl_surface_attach` 一类）都是头文件里的 `static inline`，编译进各个客户端，不在 .so 里。
非核心协议（layer-shell / relative-pointer / xdg-output）更是没有任何现成绑定。纯 ctypes
下唯一的办法就是按 XML 手写 `wl_interface` / `wl_message` 结构 —— 下面的
`_build_layer_shell()` / `_build_relative_pointer()` / `_build_xdg_output()` 与
wayland-protocols / wlr-protocols 的 XML 一一对应（改的时候对着 XML 核签名；
`tests/test_wayland_window.py` 有签名断言守着）。

⚠️ **多屏位置必须读 xdg-output**：wlroots 系合成器（sway 等）的 `wl_output.geometry`
x/y **恒为 (0,0)**（实测：双屏 3280 宽布局里两块都报 (0,0)）—— 拿它挑输出/算边距会认错屏；
`zxdg_output_v1.logical_position/logical_size` 才是真实位置。niri/smithay 两者都对，
但统一走 xdg-output、geometry 只作兜底。

⚠️ **数组式 marshalling（`wl_proxy_marshal_array_flags`）的槽位数必须与 XML 一字不差**。
踩过：`wl_shm.create_pool` 签名是 `(new_id, fd, size)`，少写 1 个 new_id 槽 → libwayland
把 `size` 当 fd 去 `dup`，报 `dup failed: Bad file descriptor`（strace 看到 `fcntl(4096,…)`）。
new_id 槽的值填空（NULL），对象归 libwayland 创建。

## 线程约定

与 Tk 一致：**只有 GUI 主线程**碰这个对象（`DesktopOverlay.tick()` 在 Tk 主循环里），
事件泵用的是非阻塞的 `wl_display_prepare_read/read_events/cancel_read`，绝不阻塞界面。
"""
from __future__ import annotations

import ctypes
import mmap
import os
import select
import time
from dataclasses import dataclass
from typing import Any, Callable

from PIL import Image

from .overlay_pixels import clamp01, premultiplied_bgra

_LOG = "[desktop:wayland]"

# ================================================================ 纯逻辑（离线可测）


@dataclass(frozen=True)
class OutputInfo:
    """一块输出的全局逻辑几何（坐标与 `wl_output.geometry` 同系）。"""

    x: int
    y: int
    width: int
    height: int
    scale: int
    name: str


def pick_output(pos: tuple[int, int], size: tuple[int, int],
                outputs: list[OutputInfo]) -> OutputInfo | None:
    """挑面板该落在哪块输出。

    1. 有相交：取**相交面积最大**的那块（面板整体落在某块里时就是它）；
    2. 都不相交：取面板中心**最近**的那块（负坐标副屏也按距离算）；
    3. 空列表：None（调用方先跳过这一帧，等 output 事件）。
    """
    if not outputs:
        return None
    x, y = int(pos[0]), int(pos[1])
    w, h = int(size[0]), int(size[1])
    cx, cy = x + w / 2.0, y + h / 2.0

    def area(o: OutputInfo) -> int:
        ix = max(0, min(x + w, o.x + o.width) - max(x, o.x))
        iy = max(0, min(y + h, o.y + o.height) - max(y, o.y))
        return ix * iy

    def center_dist(o: OutputInfo) -> float:
        return (cx - (o.x + o.width / 2.0)) ** 2 + (cy - (o.y + o.height / 2.0)) ** 2

    best = max(outputs, key=lambda o: (area(o), -center_dist(o)))
    if area(best) > 0:
        return best
    return min(outputs, key=center_dist)


def layer_margins(pos: tuple[int, int], output: OutputInfo) -> tuple[int, int]:
    """anchor=top|left 时 layer 面的 (top, left) 边距 = 全局坐标 − 输出原点。

    可以为负（面板有一半在输出外时）——协议允许负 margin，剪裁交给合成器。
    """
    return (int(pos[1]) - output.y, int(pos[0]) - output.x)


def logical_size(physical: tuple[int, int], scale: int) -> tuple[int, int]:
    """物理像素 → 逻辑尺寸（向上取整；`set_size` 收的是逻辑尺寸）。"""
    s = max(1, int(scale))
    return (max(1, -(-int(physical[0]) // s)), max(1, -(-int(physical[1]) // s)))


def buffer_dims(physical: tuple[int, int], scale: int) -> tuple[int, int]:
    """按 scale 补齐到整数倍（协议要求 buffer 尺寸是 buffer_scale 的整数倍）。

    差出来的 1~scale-1 像素在右侧/下侧留空（面板贴 0,0，多出来的是透明边），
    比直接发奇数尺寸撞 `invalid_size` 协议错误安全。
    """
    s = max(1, int(scale))
    return (-(-int(physical[0]) // s) * s, -(-int(physical[1]) // s) * s)


def drag_target(anchor_pos: tuple[int, int],
                anchor_local: tuple[float, float],
                local: tuple[float, float]) -> tuple[int, int]:
    """「本地坐标法」的拖动目标位置 = 锚点位置 + 本地坐标差。

    **为什么本地坐标差能当位移用**：niri 系的 click-grab 在按下时**冻结焦点坐标**
    （niri 的 `ClickGrab` 有意不更新 grab location，见其源码注释），所以
    `wl_pointer.motion` 的 surface-local 坐标始终是「全局指针 − 按下时的面原点」，
    两次相减就是真实位移。相对指针（sway/wlroots 系）则直接给位移增量 ——
    两条源由 `_make_pointer` 里的状态机选择，这里只做数学。

    坐标用 float（协议里是 wl_fixed，1/256 像素），结果四舍五入到整数像素。
    """
    return (int(round(anchor_pos[0] + local[0] - anchor_local[0])),
            int(round(anchor_pos[1] + local[1] - anchor_local[1])))


# ================================================================ 协议常量（对照 XML）

# --- 核心协议（wayland.xml / wayland-client-protocol.h）
_WL_DISPLAY_GET_REGISTRY = 1
_WL_REGISTRY_BIND = 0
_WL_COMPOSITOR_CREATE_SURFACE = 0
_WL_COMPOSITOR_CREATE_REGION = 1
_WL_SURFACE_DESTROY = 0
_WL_SURFACE_ATTACH = 1
_WL_SURFACE_DAMAGE = 2
_WL_SURFACE_SET_INPUT_REGION = 5
_WL_SURFACE_COMMIT = 6
_WL_SURFACE_SET_BUFFER_SCALE = 8
_WL_SURFACE_DAMAGE_BUFFER = 9
_WL_SHM_CREATE_POOL = 0
_WL_SHM_POOL_CREATE_BUFFER = 0
_WL_SHM_POOL_DESTROY = 1
_WL_BUFFER_DESTROY = 0
_WL_REGION_DESTROY = 0
_WL_SEAT_GET_POINTER = 0

_WL_SHM_FORMAT_ARGB8888 = 0

# --- wlr-layer-shell-unstable-v1（对照 wlr-protocols 的 XML）
_ZLSS_GET_LAYER_SURFACE = 0
_ZLSS_SET_SIZE = 0
_ZLSS_SET_ANCHOR = 1
_ZLSS_SET_EXCLUSIVE_ZONE = 2
_ZLSS_SET_MARGIN = 3
_ZLSS_SET_KEYBOARD_INTERACTIVITY = 4
_ZLSS_ACK_CONFIGURE = 6
_ZLSS_DESTROY = 7

_LAYER_OVERLAY = 3
_ANCHOR_TOP = 1
_ANCHOR_LEFT = 4
_KEYBOARD_INTERACTIVITY_NONE = 0

# --- relative-pointer-unstable-v1（⚠️ XML 里 destroy 在前：opcode 0=destroy、1=get）
_REL_MANAGER_GET = 1
_REL_MANAGER_DESTROY = 0
_REL_POINTER_DESTROY = 0

# --- xdg-output-unstable-v1（多屏真实位置；wlroots 的 wl_output.geometry x/y 恒为 0）
# ⚠️ XML 里同样是 destroy 在前：opcode 0=destroy、1=get_xdg_output
_XDG_MANAGER_GET = 1

_BTN_LEFT = 0x110          # linux/input-event-codes.h
_WL_POINTER_STATE_PRESSED = 1
_WL_SEAT_CAPABILITY_POINTER = 1

#: 我们实现/绑定的 layer-shell 版本（XML 是 4；v5 只多了 exclusive_edge，用不到）
_LAYER_SHELL_VERSION = 4
#: 建窗时等 configure 的上限（正常合成器毫秒级）
_CONFIGURE_TIMEOUT_S = 1.0
_PUMP_SLEEP_S = 0.002
#: 拖动源选择的观察窗：按下后给它一点时间等 relative-pointer 事件（sway/wlroots 系第一帧就有）。
#: 等不到就切「本地坐标法」——niri 系只发 wl_pointer.motion（实测：全程 0 条 relative_motion），
#: 而它的 click-grab 冻结了焦点坐标，本地差 = 真实位移（见 `drag_target`）。
_RELATIVE_GRACE_S = 0.08


# ================================================================ ctypes 基础设施


class _WlArgument(ctypes.Union):
    """`union wl_argument`：按类型装参数槽（值/指针共 8 字节）。"""

    _fields_ = [
        ("i", ctypes.c_int32),
        ("u", ctypes.c_uint32),
        ("f", ctypes.c_int32),
        ("s", ctypes.c_char_p),
        ("o", ctypes.c_void_p),
        ("n", ctypes.c_uint32),
        ("a", ctypes.c_void_p),
        ("h", ctypes.c_int32),
    ]


class _WlMessage(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("signature", ctypes.c_char_p),
        ("types", ctypes.POINTER(ctypes.c_void_p)),
    ]


class _WlInterface(ctypes.Structure):
    pass


_WlInterface._fields_ = [
    ("name", ctypes.c_char_p),
    ("version", ctypes.c_int),
    ("method_count", ctypes.c_int),
    ("methods", ctypes.POINTER(_WlMessage)),
    ("event_count", ctypes.c_int),
    ("events", ctypes.POINTER(_WlMessage)),
]

#: 手写的 wl_interface/wl_message 结构必须活到进程结束（GC 掉 = 野指针）
_KEEP: list[Any] = []

iface_layer_shell: _WlInterface | None = None
iface_layer_surface: _WlInterface | None = None
iface_rel_manager: _WlInterface | None = None
iface_rel_pointer: _WlInterface | None = None
iface_xdg_manager: _WlInterface | None = None
iface_xdg_output: _WlInterface | None = None
iface_xdg_popup: _WlInterface | None = None      # 仅 types 表占位，从不实例化


def _mk_msg(name: str, signature: str, types: list[int] | None = None) -> _WlMessage:
    """造一条 `wl_message`；`types` 给接口地址（非接口参数填 0 占位）。"""
    msg = _WlMessage()
    msg.name = name.encode()
    msg.signature = signature.encode()
    if types is None:
        msg.types = None
    else:
        arr = (ctypes.c_void_p * len(types))(*[int(t or 0) for t in types])
        msg.types = ctypes.cast(arr, ctypes.POINTER(ctypes.c_void_p))
        _KEEP.append(arr)
    return msg


def _empty_iface(name: str) -> _WlInterface:
    iface = _WlInterface()
    iface.name = name.encode()
    iface.version = 1
    iface.method_count = 0
    iface.methods = None
    iface.event_count = 0
    iface.events = None
    return iface


def _core_iface(lib: Any, name: str) -> int:
    """取核心协议接口 data 符号的**地址**（in_dll 拿到的就是符号所在内存）。"""
    return ctypes.addressof(ctypes.c_char.in_dll(lib, name))


def _build_layer_shell(lib: Any) -> None:
    global iface_layer_shell, iface_layer_surface, iface_xdg_popup
    surface_iface = _core_iface(lib, "wl_surface_interface")
    output_iface = _core_iface(lib, "wl_output_interface")

    iface_xdg_popup = _empty_iface("xdg_popup")
    _KEEP.append(iface_xdg_popup)

    iface_layer_surface = _WlInterface()
    iface_layer_surface.name = b"zwlr_layer_surface_v1"
    iface_layer_surface.version = _LAYER_SHELL_VERSION
    ls_methods = (_WlMessage * 9)(
        _mk_msg("set_size", "uu", [0, 0]),
        _mk_msg("set_anchor", "u", [0]),
        _mk_msg("set_exclusive_zone", "i", [0]),
        _mk_msg("set_margin", "iiii", [0, 0, 0, 0]),
        _mk_msg("set_keyboard_interactivity", "u", [0]),
        _mk_msg("get_popup", "o", [ctypes.addressof(iface_xdg_popup)]),
        _mk_msg("ack_configure", "u", [0]),
        _mk_msg("destroy", "", []),
        _mk_msg("set_layer", "u", [0]),
    )
    ls_events = (_WlMessage * 2)(
        _mk_msg("configure", "uuu", [0, 0, 0]),
        _mk_msg("closed", "", []),
    )
    iface_layer_surface.method_count = 9
    iface_layer_surface.methods = ls_methods
    iface_layer_surface.event_count = 2
    iface_layer_surface.events = ls_events

    iface_layer_shell = _WlInterface()
    iface_layer_shell.name = b"zwlr_layer_shell_v1"
    iface_layer_shell.version = _LAYER_SHELL_VERSION
    zl_methods = (_WlMessage * 2)(
        # (new_id layer_surface, surface, output?, uint layer, string namespace)
        _mk_msg("get_layer_surface", "no?ous",
                [ctypes.addressof(iface_layer_surface), surface_iface, output_iface, 0, 0]),
        _mk_msg("destroy", "", []),
    )
    iface_layer_shell.method_count = 2
    iface_layer_shell.methods = zl_methods
    iface_layer_shell.event_count = 0
    iface_layer_shell.events = None

    _KEEP.extend([ls_methods, ls_events, zl_methods, iface_layer_surface, iface_layer_shell])


def _build_relative_pointer(lib: Any) -> None:
    global iface_rel_manager, iface_rel_pointer
    pointer_iface = _core_iface(lib, "wl_pointer_interface")

    iface_rel_pointer = _WlInterface()
    iface_rel_pointer.name = b"zwp_relative_pointer_v1"
    iface_rel_pointer.version = 1
    rp_methods = (_WlMessage * 1)(_mk_msg("destroy", "", []))
    rp_events = (_WlMessage * 1)(
        _mk_msg("relative_motion", "uuffff", [0, 0, 0, 0, 0, 0]))
    iface_rel_pointer.method_count = 1
    iface_rel_pointer.methods = rp_methods
    iface_rel_pointer.event_count = 1
    iface_rel_pointer.events = rp_events

    iface_rel_manager = _WlInterface()
    iface_rel_manager.name = b"zwp_relative_pointer_manager_v1"
    iface_rel_manager.version = 1
    rm_methods = (_WlMessage * 2)(
        _mk_msg("destroy", "", []),                                        # opcode 0
        _mk_msg("get_relative_pointer", "no",                               # opcode 1
                [ctypes.addressof(iface_rel_pointer), pointer_iface]),
    )
    iface_rel_manager.method_count = 2
    iface_rel_manager.methods = rm_methods
    iface_rel_manager.event_count = 0
    iface_rel_manager.events = None

    _KEEP.extend([rp_methods, rp_events, rm_methods, iface_rel_pointer, iface_rel_manager])


def _build_xdg_output(lib: Any) -> None:
    """xdg-output：多屏的**真实**逻辑位置/尺寸（wlroots 的 wl_output.geometry x/y 恒为 0）。"""
    global iface_xdg_manager, iface_xdg_output
    output_iface = _core_iface(lib, "wl_output_interface")

    iface_xdg_output = _WlInterface()
    iface_xdg_output.name = b"zxdg_output_v1"
    iface_xdg_output.version = 3
    xo_methods = (_WlMessage * 1)(_mk_msg("destroy", "", []))
    xo_events = (_WlMessage * 5)(
        _mk_msg("logical_position", "ii", [0, 0]),
        _mk_msg("logical_size", "ii", [0, 0]),
        _mk_msg("done", "", []),
        _mk_msg("name", "s", [0]),
        _mk_msg("description", "s", [0]),
    )
    iface_xdg_output.method_count = 1
    iface_xdg_output.methods = xo_methods
    iface_xdg_output.event_count = 5
    iface_xdg_output.events = xo_events

    iface_xdg_manager = _WlInterface()
    iface_xdg_manager.name = b"zxdg_output_manager_v1"
    iface_xdg_manager.version = 3
    xm_methods = (_WlMessage * 2)(
        _mk_msg("destroy", "", []),                                  # opcode 0
        _mk_msg("get_xdg_output", "no",                              # opcode 1
                [ctypes.addressof(iface_xdg_output), output_iface]),
    )
    iface_xdg_manager.method_count = 2
    iface_xdg_manager.methods = xm_methods
    iface_xdg_manager.event_count = 0
    iface_xdg_manager.events = None

    _KEEP.extend([xo_methods, xo_events, xm_methods, iface_xdg_output, iface_xdg_manager])


def read_interface(iface: _WlInterface) -> dict:
    """测试用：把 `wl_interface` 结构读回成可断言的字典。"""
    def msgs(ptr: Any, count: int) -> list[tuple[str, str]]:
        out = []
        for i in range(count):
            m = ptr[i]
            out.append((m.name.decode(), m.signature.decode()))
        return out

    return {"name": iface.name.decode(), "version": iface.version,
            "methods": msgs(iface.methods, iface.method_count) if iface.method_count else [],
            "events": msgs(iface.events, iface.event_count) if iface.event_count else []}


def _load_lib() -> Any:
    lib = ctypes.CDLL("libwayland-client.so.0")
    lib.wl_display_connect.argtypes = [ctypes.c_char_p]
    lib.wl_display_connect.restype = ctypes.c_void_p
    lib.wl_display_disconnect.argtypes = [ctypes.c_void_p]
    lib.wl_display_disconnect.restype = None
    lib.wl_display_get_fd.argtypes = [ctypes.c_void_p]
    lib.wl_display_get_fd.restype = ctypes.c_int
    lib.wl_display_roundtrip.argtypes = [ctypes.c_void_p]
    lib.wl_display_roundtrip.restype = ctypes.c_int
    lib.wl_display_prepare_read.argtypes = [ctypes.c_void_p]
    lib.wl_display_prepare_read.restype = ctypes.c_int
    lib.wl_display_read_events.argtypes = [ctypes.c_void_p]
    lib.wl_display_read_events.restype = ctypes.c_int
    lib.wl_display_cancel_read.argtypes = [ctypes.c_void_p]
    lib.wl_display_cancel_read.restype = ctypes.c_int
    lib.wl_display_dispatch_pending.argtypes = [ctypes.c_void_p]
    lib.wl_display_dispatch_pending.restype = ctypes.c_int
    lib.wl_display_flush.argtypes = [ctypes.c_void_p]
    lib.wl_display_flush.restype = ctypes.c_int
    lib.wl_proxy_marshal_array_flags.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.POINTER(_WlArgument)]
    lib.wl_proxy_marshal_array_flags.restype = ctypes.c_void_p
    lib.wl_proxy_add_listener.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.wl_proxy_add_listener.restype = ctypes.c_int
    lib.wl_proxy_get_version.argtypes = [ctypes.c_void_p]
    lib.wl_proxy_get_version.restype = ctypes.c_uint32
    lib.wl_proxy_destroy.argtypes = [ctypes.c_void_p]
    lib.wl_proxy_destroy.restype = None
    _build_layer_shell(lib)
    _build_relative_pointer(lib)
    _build_xdg_output(lib)
    return lib


def _addr(proxy: Any) -> int:
    return int(proxy) if proxy else 0


def _ptr(proxy: Any) -> ctypes.c_void_p:
    return ctypes.c_void_p(int(proxy)) if proxy else ctypes.c_void_p(None)


def _fixed_to_float(v: int) -> float:
    return ctypes.c_int32(v).value / 256.0


def _cb(*argtypes: Any) -> Any:
    return ctypes.CFUNCTYPE(None, *argtypes)


def marshal_request(lib: Any, proxy: Any, opcode: int, args: list[tuple[str, Any]],
                    interface: int | None = None, version: int = 0,
                    flags: int = 0) -> Any:
    """数组式 marshalling 入口（窗口与测试共用）。

    ⚠️ `args` 是**与 XML 一字不差的槽位表**（含 new_id 空槽，值为 `("n", None)`）。
    少一个槽 → libwayland 会顺移读取后面的参数（实测：`create_pool` 少写 new_id 槽，
    把 size 当 fd 去 dup，报 `dup failed: Bad file descriptor`）。
    """
    arr = (_WlArgument * max(1, len(args)))()
    for i, (kind, val) in enumerate(args):
        if kind == "s":
            arr[i].s = val if (val is None or isinstance(val, bytes)) \
                else str(val).encode()
        elif kind == "o" or kind == "n":
            arr[i].o = ctypes.c_void_p(int(val)) if val else None
        elif kind == "h":
            arr[i].h = ctypes.c_int32(int(val)).value
        elif kind == "f":
            arr[i].f = ctypes.c_int32(int(val)).value
        elif kind == "u":
            arr[i].u = ctypes.c_uint32(int(val) & 0xFFFFFFFF).value
        elif kind == "i":
            arr[i].i = ctypes.c_int32(int(val)).value
        else:
            raise TypeError(f"未知参数类型 {kind!r}")
    return lib.wl_proxy_marshal_array_flags(
        _ptr(proxy), int(opcode),
        ctypes.c_void_p(int(interface)) if interface else None,
        int(version), int(flags), arr)


# ---- 监听器结构（回调对象由 `_listen()` 统一持引用 —— GC 掉就是野指针）


class _RegistryListener(ctypes.Structure):
    _fields_ = [
        ("global_", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                        ctypes.c_char_p, ctypes.c_uint32)),
        ("global_remove", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)),
    ]


class _ShmListener(ctypes.Structure):
    _fields_ = [("format", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32))]


class _BufferListener(ctypes.Structure):
    _fields_ = [("release", _cb(ctypes.c_void_p, ctypes.c_void_p))]


class _OutputListener(ctypes.Structure):
    _fields_ = [
        ("geometry", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32,
                         ctypes.c_int32, ctypes.c_int32, ctypes.c_int32,
                         ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int32)),
        ("mode", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                     ctypes.c_int32, ctypes.c_int32)),
        ("done", _cb(ctypes.c_void_p, ctypes.c_void_p)),
        ("scale", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32)),
    ]


class _SeatListener(ctypes.Structure):
    _fields_ = [("capabilities", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32))]


class _PointerListener(ctypes.Structure):
    _fields_ = [
        ("enter", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                      ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32)),
        ("leave", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)),
        ("motion", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                       ctypes.c_int32, ctypes.c_int32)),
        ("button", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.c_uint32, ctypes.c_uint32)),
        ("axis", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                     ctypes.c_int32)),
        ("frame", _cb(ctypes.c_void_p, ctypes.c_void_p)),
    ]


class _RelPointerListener(ctypes.Structure):
    _fields_ = [
        ("relative_motion", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                ctypes.c_uint32, ctypes.c_int32, ctypes.c_int32,
                                ctypes.c_int32, ctypes.c_int32)),
    ]


class _XdgOutputListener(ctypes.Structure):
    _fields_ = [
        ("logical_position", _cb(ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_int32, ctypes.c_int32)),
        ("logical_size", _cb(ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_int32, ctypes.c_int32)),
        ("done", _cb(ctypes.c_void_p, ctypes.c_void_p)),
        ("name", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)),
        ("description", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)),
    ]


class _LayerSurfaceListener(ctypes.Structure):
    _fields_ = [
        ("configure", _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                          ctypes.c_uint32, ctypes.c_uint32)),
        ("closed", _cb(ctypes.c_void_p, ctypes.c_void_p)),
    ]


# ================================================================ 窗口本体


class LayerShellWindow:
    """一块 layer-shell overlay 面的最小封装（对共享模块遵守
    `vlt/platform/base.py:DesktopWindow` 的窗口契约）。

    生命周期：`__init__` 连显示 + 绑全局 + 建面（等 configure，有界）；
    之后 `tick()` 泵事件、`set_panel()` 出新图、`move()`/`set_*()` 改状态；`close()` 幂等。
    """

    #: 建不起来时保持 False（调用方据此回落 Tk）
    available = False

    def __init__(self, size: tuple[int, int], alpha: float = 1.0,
                 click_through: bool = True,
                 on_drag_end: Callable[[int, int], None] | None = None,
                 _lib: Any = None) -> None:
        self._size = (max(1, int(size[0])), max(1, int(size[1])))
        self._alpha = clamp01(alpha)
        self._click_through = bool(click_through)
        self._draggable = False
        self._on_drag_end = on_drag_end
        self._pos = (0, 0)
        self._panel: Image.Image | None = None
        self._dirty = False
        self._dead = False

        self._lib: Any = None
        self._display: Any = None
        self._registry: Any = None
        self._compositor: Any = None
        self._shm: Any = None
        self._layer_shell: Any = None
        self._rel_manager: Any = None
        self._xdg_manager: Any = None
        self._seat: Any = None
        self._pointer: Any = None
        self._rel_pointer: Any = None
        self._surface: Any = None
        self._layer_surface: Any = None

        self._globals: dict[str, tuple[int, int]] = {}   # 接口名 → (global 名, 版本)
        self._shm_formats: list[int] = []
        self._outputs: dict[int, dict] = {}              # global 名 → 状态
        self._output_proxies: dict[int, int] = {}        # 代理地址 → global 名
        self._output: OutputInfo | None = None
        self._output_global = 0
        self._configured = False

        self._shm_fd: int | None = None
        self._shm_map: Any = None
        self._pool: Any = None
        self._buffers: list[dict] = []                   # {proxy, addr, busy}
        self._next_buf = 0
        self._stride = 0
        self._buf_dims = (0, 0)

        self._drag_active = False
        # 拖动源状态机："" = 观察中（等 relative-pointer）/ "relative" = 用相对增量 /
        # "local" = 用 wl_pointer.motion 的本地坐标差（niri 系没有 relative 事件）
        self._drag_mode = ""
        self._drag_anchor_pos: tuple[int, int] = (0, 0)
        self._drag_anchor_local: tuple[float, float] | None = None
        self._drag_started = 0.0
        self._buttons_seen = 0        # 收到的指针按键总数（穿透验证 / 排障用）

        self._listeners: list[Any] = []                  # listener 结构 + 回调的引用池
        self._buffer_listeners: list[Any] = []           # shm 缓冲的 release 监听（重建时整批换掉）

        try:
            self._setup(_lib)
            self.available = True
        except Exception as exc:  # noqa: BLE001 — 建不起来就保持 available=False
            print(f"{_LOG} ⚠️ 原生 Wayland 窗建不起来（回落 Tk）："
                  f"{type(exc).__name__}: {exc}", flush=True)
            self._teardown()

    # ---------- 建立 / 销毁 ----------

    def _setup(self, lib: Any) -> None:
        self._lib = lib if lib is not None else _load_lib()
        if not os.environ.get("WAYLAND_DISPLAY"):
            raise RuntimeError("没有 WAYLAND_DISPLAY（X11 会话？）")
        self._display = self._lib.wl_display_connect(None)
        if not self._display:
            raise RuntimeError("wl_display_connect 失败")

        # 1) 先拿 registry + 全局对象表（输出在 on_global 里顺手绑定）
        reg_listener = self._registry_listener()
        self._registry = self._req(
            self._display, _WL_DISPLAY_GET_REGISTRY, [("n", None)],
            interface=_core_iface(self._lib, "wl_registry_interface"), version=1)
        if not self._registry:
            raise RuntimeError("get_registry 失败")
        self._attach_listener(self._registry, reg_listener)
        if self._lib.wl_display_roundtrip(_ptr(self._display)) == -1:
            raise RuntimeError("roundtrip 失败（取全局对象列表）")

        for need in ("wl_compositor", "wl_shm", "zwlr_layer_shell_v1"):
            if need not in self._globals:
                raise RuntimeError(f"合成器缺 {need}")
        self._compositor = self._bind_global("wl_compositor", "wl_compositor_interface", cap=6)
        self._shm = self._bind_global("wl_shm", "wl_shm_interface", cap=1)
        self._layer_shell = self._bind_global("zwlr_layer_shell_v1", None,
                                              cap=_LAYER_SHELL_VERSION, iface=iface_layer_shell)
        if "zwp_relative_pointer_manager_v1" in self._globals:
            self._rel_manager = self._bind_global("zwp_relative_pointer_manager_v1", None,
                                                  cap=1, iface=iface_rel_manager)
        if "zxdg_output_manager_v1" in self._globals:
            self._xdg_manager = self._bind_global("zxdg_output_manager_v1", None,
                                                  cap=3, iface=iface_xdg_manager)
            # registry 顺序不保证 manager 先到：给已绑上的输出补挂 xdg_output
            for gname, st in self._outputs.items():
                if "xdg_addr" not in st:
                    self._make_xdg_output(int(gname), st["addr"], st)
        if "wl_seat" in self._globals:
            self._seat = self._bind_global("wl_seat", "wl_seat_interface", cap=1)
            self._attach_listener(self._seat, self._seat_listener())
        self._attach_listener(self._shm, self._shm_listener())

        # 2) 再一轮：把输出的 geometry/mode/scale 收齐
        if self._lib.wl_display_roundtrip(_ptr(self._display)) == -1:
            raise RuntimeError("roundtrip 失败（收输出几何）")
        if _WL_SHM_FORMAT_ARGB8888 not in self._shm_formats:
            raise RuntimeError(f"wl_shm 不支持 ARGB8888（支持：{self._shm_formats}）")

        out = pick_output(self._pos, self._size, self._output_infos())
        if out is not None:
            self._ensure_surface(out, wait_configure=True)

    def _bind_global(self, iface_name: str, core_sym: str | None,
                     cap: int, iface: Any = None) -> Any:
        name, version = self._globals[iface_name]
        ver = max(1, min(int(version), int(cap)))
        target = (ctypes.addressof(iface) if iface is not None
                  else _core_iface(self._lib, core_sym or ""))
        return self._req(self._registry, _WL_REGISTRY_BIND,
                         [("u", name), ("s", iface_name), ("u", ver), ("n", None)],
                         interface=target, version=ver)

    def _registry_listener(self) -> Any:
        def on_global(_data: Any, _reg: Any, name: int, iface: bytes, version: int) -> None:
            iface_s = iface.decode() if isinstance(iface, bytes) else str(iface)
            if iface_s == "wl_output":
                self._bind_output(name, version)
            else:
                self._globals[iface_s] = (int(name), int(version))

        def on_global_remove(_data: Any, _reg: Any, name: int) -> None:
            st = self._outputs.pop(int(name), None)
            if st is not None:
                xa = st.get("xdg_addr")
                if xa:
                    try:
                        self._lib.wl_proxy_destroy(ctypes.c_void_p(int(xa)))
                    except Exception:  # noqa: BLE001
                        pass
                addr = self._output_proxies.pop(st["addr"], None)
                if addr is not None:
                    try:
                        self._lib.wl_proxy_destroy(ctypes.c_void_p(addr))
                    except Exception:  # noqa: BLE001
                        pass

        listener = _RegistryListener(
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
                ctypes.c_uint32)(on_global),
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)(on_global_remove))
        self._listeners.append(listener)
        return listener

    def _shm_listener(self) -> Any:
        def on_format(_data: Any, _shm: Any, fmt: int) -> None:
            self._shm_formats.append(int(fmt))

        listener = _ShmListener(_cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)(on_format))
        self._listeners.append(listener)
        return listener

    def _seat_listener(self) -> Any:
        def on_caps(_data: Any, seat: Any, caps: int) -> None:
            if (int(caps) & _WL_SEAT_CAPABILITY_POINTER) and self._pointer is None:
                self._make_pointer(seat)

        listener = _SeatListener(_cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)(on_caps))
        self._listeners.append(listener)
        return listener

    def _attach_listener(self, proxy: Any, listener: Any) -> None:
        self._lib.wl_proxy_add_listener(
            _ptr(proxy), ctypes.cast(ctypes.pointer(listener), ctypes.c_void_p), None)

    # ---------- 输出 ----------

    def _bind_output(self, name: int, version: int) -> None:
        # 输出不走 self._globals（on_global 直接调进来），这里单独 bind
        ver = max(1, min(int(version), 2))
        proxy = self._req(self._registry, _WL_REGISTRY_BIND,
                          [("u", name), ("s", "wl_output"), ("u", ver), ("n", None)],
                          interface=_core_iface(self._lib, "wl_output_interface"),
                          version=ver)
        st = {"x": 0, "y": 0, "width": 0, "height": 0, "scale": 1, "done": False,
              "addr": _addr(proxy),
              # xdg-output 的真实逻辑位置/尺寸（优先；wlroots 的 geometry x/y 恒为 0）
              "lx": None, "ly": None, "lw": None, "lh": None, "name": None}
        self._outputs[int(name)] = st
        self._output_proxies[st["addr"]] = int(name)

        def find(addr: int) -> dict | None:
            g = self._output_proxies.get(addr)
            return self._outputs.get(g) if g is not None else None

        def on_geometry(_d: Any, out: Any, x: int, y: int, _pw: int, _ph: int, _sub: int,
                        _mk: bytes, _md: bytes, _tr: int) -> None:
            st_ = find(_addr(out))
            if st_ is not None:
                st_["x"], st_["y"] = int(x), int(y)

        def on_mode(_d: Any, out: Any, _flags: int, w: int, h: int) -> None:
            st_ = find(_addr(out))
            if st_ is not None:
                st_["width"], st_["height"] = int(w), int(h)

        def on_done(_d: Any, out: Any) -> None:
            st_ = find(_addr(out))
            if st_ is not None:
                st_["done"] = True

        def on_scale(_d: Any, out: Any, factor: int) -> None:
            st_ = find(_addr(out))
            if st_ is not None:
                st_["scale"] = max(1, int(factor))

        listener = _OutputListener(
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32,
                ctypes.c_int32, ctypes.c_int32, ctypes.c_int32, ctypes.c_char_p,
                ctypes.c_char_p, ctypes.c_int32)(on_geometry),
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32,
                ctypes.c_int32)(on_mode),
            _cb(ctypes.c_void_p, ctypes.c_void_p)(on_done),
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32)(on_scale))
        self._listeners.append(listener)
        self._attach_listener(proxy, listener)

        if self._xdg_manager is not None:
            self._make_xdg_output(int(name), proxy, st)

    def _make_xdg_output(self, gname: int, wl_output: Any, st: dict) -> None:
        """给一块输出挂 xdg-output：**多屏真实位置/尺寸**。

        wlroots 系（sway/Hyprland）的 `wl_output.geometry` x/y 恒为 (0,0)（实测），
        `zxdg_output_v1.logical_position/logical_size` 才是能区分多屏的坐标。
        """
        if self._xdg_manager is None or iface_xdg_output is None:
            return
        ver = min(3, int(self._lib.wl_proxy_get_version(_ptr(self._xdg_manager))))
        xo = self._req(self._xdg_manager, _XDG_MANAGER_GET,
                       [("n", None), ("o", wl_output)],
                       interface=ctypes.addressof(iface_xdg_output), version=ver)
        if not xo:
            return
        st["xdg_addr"] = _addr(xo)

        def on_pos(_d: Any, _o: Any, x: int, y: int) -> None:
            st["lx"], st["ly"] = int(x), int(y)

        def on_size(_d: Any, _o: Any, w: int, h: int) -> None:
            st["lw"], st["lh"] = int(w), int(h)

        def on_name(_d: Any, _o: Any, s: bytes) -> None:
            if s:
                st["name"] = s.decode() if isinstance(s, (bytes, bytearray)) else str(s)

        listener = _XdgOutputListener(
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32)(on_pos),
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32)(on_size),
            _cb(ctypes.c_void_p, ctypes.c_void_p)(lambda *a: None),
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)(on_name),
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)(lambda *a: None))
        self._listeners.append(listener)
        self._attach_listener(xo, listener)

    @staticmethod
    def _effective_geom(st: dict) -> tuple[int, int, int, int] | None:
        """输出的**有效**全局逻辑几何：xdg-output 优先，geometry/mode 兜底。

        ⚠️ wlroots 系（sway/Hyprland）的 `wl_output.geometry` x/y **恒为 (0,0)** ——
        只有 xdg-output 的 `logical_position/logical_size` 能区分多屏（实测：双屏
        3280 宽布局里两块都报 (0,0)，拿它挑输出会认错屏）。
        """
        scale = max(1, int(st.get("scale", 1)))
        x = st.get("lx")
        y = st.get("ly")
        w = st.get("lw")
        h = st.get("lh")
        if x is None:
            x = int(st.get("x", 0))
        if y is None:
            y = int(st.get("y", 0))
        if not w:
            w = int(st.get("width", 0)) // scale
        if not h:
            h = int(st.get("height", 0)) // scale
        x, y, w, h = int(x), int(y), int(w), int(h)
        if w <= 0 or h <= 0:
            return None
        return (x, y, w, h)

    def _output_infos(self) -> list[OutputInfo]:
        out: list[OutputInfo] = []
        for gname, st in self._outputs.items():
            geom = self._effective_geom(st)
            if geom is None:
                continue
            x, y, w, h = geom
            out.append(OutputInfo(x=x, y=y, width=w, height=h,
                                  scale=max(1, int(st.get("scale", 1))),
                                  name=str(st.get("name") or gname)))
        out.sort(key=lambda o: (o.y, o.x))
        return out

    def _global_for_output(self, output: OutputInfo) -> int:
        for gname, st in self._outputs.items():
            if self._effective_geom(st) == (output.x, output.y,
                                            output.width, output.height):
                return int(gname)
        return 0

    # ---------- 面 ----------

    def _ensure_surface(self, output: OutputInfo, wait_configure: bool) -> None:
        """（重）建 layer 面并绑到指定输出。换输出必须重建 —— output 在 role 创建时绑死。"""
        self._destroy_surface()
        self._output = output
        gname = self._global_for_output(output)
        self._output_global = gname
        out_proxy = 0
        st = self._outputs.get(gname)
        if st is not None:
            out_proxy = st["addr"]

        self._surface = self._req(
            self._compositor, _WL_COMPOSITOR_CREATE_SURFACE, [("n", None)],
            interface=_core_iface(self._lib, "wl_surface_interface"),
            version=int(self._lib.wl_proxy_get_version(_ptr(self._compositor))))
        ls_ver = min(_LAYER_SHELL_VERSION,
                     int(self._lib.wl_proxy_get_version(_ptr(self._layer_shell))))
        self._layer_surface = self._req(
            self._layer_shell, _ZLSS_GET_LAYER_SURFACE,
            [("n", None), ("o", self._surface), ("o", out_proxy or None),
             ("u", _LAYER_OVERLAY), ("s", b"vlt-subtitle")],
            interface=ctypes.addressof(iface_layer_surface), version=ls_ver)
        if not self._surface or not self._layer_surface:
            raise RuntimeError("建 layer 面失败（create_surface/get_layer_surface 返回空）")

        def on_configure(_d: Any, _ls: Any, serial: int, width: int, height: int) -> None:
            self._configured = True
            try:
                self._req(self._layer_surface, _ZLSS_ACK_CONFIGURE, [("u", int(serial))])
            except Exception:  # noqa: BLE001
                pass
            self._dirty = True
            if width and height:
                self._log_once_configured(width, height)
            self._flush()

        def on_closed(_d: Any, _ls: Any) -> None:
            print(f"{_LOG} ⚠️ 合成器关闭了 layer 面（输出被拔 / 被移除）→ 面板停更；"
                  f"重新勾选桌面字幕后可重建", flush=True)
            self._destroy_surface()
            self._mark_dead("layer surface closed")

        listener = _LayerSurfaceListener(
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_uint32)(on_configure),
            _cb(ctypes.c_void_p, ctypes.c_void_p)(on_closed))
        self._listeners.append(listener)
        self._attach_listener(self._layer_surface, listener)

        self._configured = False
        self._apply_layer_state(commit=False)
        self._apply_input_region(commit=False)
        self._req(self._surface, _WL_SURFACE_COMMIT, [])
        self._flush()
        if wait_configure:
            self._pump_until(lambda: self._configured, _CONFIGURE_TIMEOUT_S)

    def _log_once_configured(self, width: int, height: int) -> None:
        if getattr(self, "_configured_logged", False):
            return
        self._configured_logged = True
        print(f"{_LOG} ✅ layer 面已 configure（{width}x{height} 逻辑像素）", flush=True)

    def _destroy_surface(self) -> None:
        if self._layer_surface is not None:
            try:
                self._req(self._layer_surface, _ZLSS_DESTROY, [])
                self._lib.wl_proxy_destroy(_ptr(self._layer_surface))
            except Exception:  # noqa: BLE001
                pass
            self._layer_surface = None
        if self._surface is not None:
            try:
                self._req(self._surface, _WL_SURFACE_DESTROY, [])
                self._lib.wl_proxy_destroy(_ptr(self._surface))
            except Exception:  # noqa: BLE001
                pass
            self._surface = None
        self._output_global = 0
        self._configured = False
        self._dirty = True

    def _apply_layer_state(self, *, commit: bool) -> None:
        if self._layer_surface is None or self._output is None:
            return
        lw, lh = logical_size(self._size, self._output.scale)
        top, left = layer_margins(self._pos, self._output)
        self._req(self._layer_surface, _ZLSS_SET_SIZE, [("u", lw), ("u", lh)])
        self._req(self._layer_surface, _ZLSS_SET_ANCHOR, [("u", _ANCHOR_TOP | _ANCHOR_LEFT)])
        self._req(self._layer_surface, _ZLSS_SET_EXCLUSIVE_ZONE, [("i", -1)])
        self._req(self._layer_surface, _ZLSS_SET_MARGIN,
                  [("i", top), ("i", 0), ("i", 0), ("i", left)])
        self._req(self._layer_surface, _ZLSS_SET_KEYBOARD_INTERACTIVITY,
                  [("u", _KEYBOARD_INTERACTIVITY_NONE)])
        if commit and self._surface is not None:
            self._req(self._surface, _WL_SURFACE_COMMIT, [])
            self._flush()

    def _apply_input_region(self, *, commit: bool) -> None:
        """穿透 = 空输入区；解锁拖动 = 恢复默认（NULL = 整个面）。"""
        if self._surface is None:
            return
        if self._draggable or not self._click_through:
            self._req(self._surface, _WL_SURFACE_SET_INPUT_REGION, [("o", None)])
        else:
            region = self._req(self._compositor, _WL_COMPOSITOR_CREATE_REGION, [("n", None)],
                               interface=_core_iface(self._lib, "wl_region_interface"),
                               version=1)
            self._req(self._surface, _WL_SURFACE_SET_INPUT_REGION, [("o", region)])
            self._req(region, _WL_REGION_DESTROY, [])
            self._lib.wl_proxy_destroy(_ptr(region))
        if commit:
            self._req(self._surface, _WL_SURFACE_COMMIT, [])
            self._flush()

    # ---------- wl_shm 出图 ----------

    def _rebuild_buffers(self) -> None:
        """按当前尺寸/scale 重造 shm 池与 2 块轮换缓冲。"""
        if self._shm is None:
            raise RuntimeError("wl_shm 还没绑上")
        scale = self._output.scale if self._output is not None else 1
        bw, bh = buffer_dims(self._size, scale)
        if (bw, bh) == self._buf_dims and self._buffers:
            return
        self._release_buffers()
        stride = bw * 4
        total = stride * bh * 2
        fd = os.memfd_create("vlt-overlay", 0)
        os.ftruncate(fd, total)
        mm = mmap.mmap(fd, total)
        pool = self._req(self._shm, _WL_SHM_CREATE_POOL,
                         [("n", None), ("h", fd), ("i", total)],
                         interface=_core_iface(self._lib, "wl_shm_pool_interface"),
                         version=1)
        if not pool:
            os.close(fd)
            mm.close()
            raise RuntimeError("create_pool 失败")
        buffers = []
        for i in range(2):
            buf = self._req(pool, _WL_SHM_POOL_CREATE_BUFFER,
                            [("n", None), ("i", i * stride * bh), ("i", bw), ("i", bh),
                             ("i", stride), ("u", _WL_SHM_FORMAT_ARGB8888)],
                            interface=_core_iface(self._lib, "wl_buffer_interface"),
                            version=1)
            if not buf:
                raise RuntimeError("create_buffer 失败")
            st = {"proxy": buf, "addr": _addr(buf), "busy": False}
            buffers.append(st)
            listener = _BufferListener(
                _cb(ctypes.c_void_p, ctypes.c_void_p)(
                    (lambda _i: (lambda _d, _b: self._on_buffer_release(_i)))(i)))
            self._buffer_listeners.append(listener)
            self._attach_listener(buf, listener)
        self._shm_fd = fd
        self._shm_map = mm
        self._pool = pool
        self._buffers = buffers
        self._stride = stride
        self._buf_dims = (bw, bh)
        self._next_buf = 0
        self._dirty = True

    def _release_buffers(self) -> None:
        for b in self._buffers:
            try:
                self._req(b["proxy"], _WL_BUFFER_DESTROY, [])
                self._lib.wl_proxy_destroy(_ptr(b["proxy"]))
            except Exception:  # noqa: BLE001
                pass
        self._buffers = []
        self._buffer_listeners.clear()          # 旧缓冲的监听引用整批释放，别随重建越积越多
        if self._pool is not None:
            try:
                self._req(self._pool, _WL_SHM_POOL_DESTROY, [])
                self._lib.wl_proxy_destroy(_ptr(self._pool))
            except Exception:  # noqa: BLE001
                pass
            self._pool = None
        if self._shm_map is not None:
            try:
                self._shm_map.close()
            except Exception:  # noqa: BLE001
                pass
            self._shm_map = None
        if self._shm_fd is not None:
            try:
                os.close(self._shm_fd)
            except Exception:  # noqa: BLE001
                pass
            self._shm_fd = None
        self._buf_dims = (0, 0)

    def _on_buffer_release(self, idx: int) -> None:
        if 0 <= idx < len(self._buffers):
            self._buffers[idx]["busy"] = False

    def _draw(self) -> None:
        if self._dead or not self._configured or self._surface is None or self._panel is None:
            return
        if not self._buffers:
            try:
                self._rebuild_buffers()
            except Exception as exc:  # noqa: BLE001
                print(f"{_LOG} ⚠️ 建 shm 缓冲失败：{type(exc).__name__}: {exc}", flush=True)
                return
        idx = self._next_buf
        if self._buffers[idx]["busy"]:
            idx = 1 - idx
            if self._buffers[idx]["busy"]:
                return                        # 两块都被合成器占着：下一跳再试，内容不会丢
        bw, bh = self._buf_dims
        img = self._panel
        if img.size != (bw, bh):
            padded = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
            padded.paste(img, (0, 0))
            img = padded
        data = premultiplied_bgra(img, self._alpha)
        offset = self._stride * bh * idx
        self._shm_map.seek(offset)
        self._shm_map.write(data)
        buf = self._buffers[idx]["proxy"]
        self._req(self._surface, _WL_SURFACE_ATTACH, [("o", buf)])
        ver = int(self._lib.wl_proxy_get_version(_ptr(self._surface)))
        if ver >= 4:
            self._req(self._surface, _WL_SURFACE_DAMAGE_BUFFER,
                      [("i", 0), ("i", 0), ("i", bw), ("i", bh)])
        else:  # pragma: no cover —— 老合成器兜底
            self._req(self._surface, _WL_SURFACE_DAMAGE,
                      [("i", 0), ("i", 0), ("i", bw), ("i", bh)])
        if self._output is not None and self._output.scale > 1 and ver >= 3:
            self._req(self._surface, _WL_SURFACE_SET_BUFFER_SCALE,
                      [("i", self._output.scale)])
        self._req(self._surface, _WL_SURFACE_COMMIT, [])
        self._buffers[idx]["busy"] = True
        self._next_buf = 1 - idx
        self._dirty = False
        self._flush()

    # ---------- 事件泵 ----------

    def _pump_events(self, timeout_ms: int = 0) -> bool:
        """非阻塞泵事件。返回 False = 连接坏了（面板停更，不崩进程）。

        ⚠️ 规范流程：prepare_read → flush → poll → read_events | cancel_read；
        **只要进过 prepare_read 就必须以 read/cancel 收尾**（用 finally 兜）。
        """
        if self._dead or self._display is None:
            return False
        prepared = False
        try:
            if self._lib.wl_display_prepare_read(_ptr(self._display)) != 0:
                # 队列里还有没派发的事件：先派发；下一跳再 prepare
                if self._lib.wl_display_dispatch_pending(_ptr(self._display)) == -1:
                    return self._mark_dead("dispatch_pending 失败")
                return True
            prepared = True
            self._lib.wl_display_flush(_ptr(self._display))
            fd = self._lib.wl_display_get_fd(_ptr(self._display))
            r, _, _ = select.select([fd], [], [], max(0, timeout_ms) / 1000.0)
            if r:
                if self._lib.wl_display_read_events(_ptr(self._display)) == -1:
                    return self._mark_dead("read_events 失败")
            else:
                # 没数据：按规范先 cancel 收尾，再派发已排队的（dispatch 不再夹在 prepare 中间）
                self._lib.wl_display_cancel_read(_ptr(self._display))
            prepared = False
        finally:
            if prepared:
                self._lib.wl_display_cancel_read(_ptr(self._display))
        if self._lib.wl_display_dispatch_pending(_ptr(self._display)) == -1:
            return self._mark_dead("dispatch_pending 失败")
        return True

    def _pump_until(self, pred: Callable[[], bool], timeout_s: float) -> bool:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if pred():
                return True
            if not self._pump_events(timeout_ms=5):
                return False
            time.sleep(_PUMP_SLEEP_S)
        return pred()

    def _flush(self) -> None:
        try:
            self._lib.wl_display_flush(_ptr(self._display))
        except Exception:  # noqa: BLE001
            pass

    def _mark_dead(self, why: str) -> bool:
        if not self._dead:
            self._dead = True
            self.available = False
            print(f"{_LOG} ⚠️ Wayland 连接断开（面板停更）：{why}", flush=True)
        return False

    # ---------- 指针 / 拖动 ----------

    def _make_pointer(self, seat: Any) -> None:
        pointer = self._req(seat, _WL_SEAT_GET_POINTER, [("n", None)],
                            interface=_core_iface(self._lib, "wl_pointer_interface"),
                            version=int(self._lib.wl_proxy_get_version(_ptr(seat))))
        if not pointer:
            return
        self._pointer = pointer

        def on_motion(_d: Any, _p: Any, _t: int, fx: int, fy: int) -> None:
            """本地坐标法（第二拖动源）：niri 系合成器只发 motion、不发 relative。

            实测（niri 26.04 + 本机 `WAYLAND_DEBUG`）：按下拖动期间 `wl_pointer.motion`
            持续送达（含拖出面板后的坐标），而 `relative_motion` **全程 0 条**。
            niri 的 click-grab 又把焦点坐标冻结在按下那一刻，所以本地坐标差 = 真实位移。
            """
            if not self._drag_active or self._drag_mode == "relative":
                return
            local = (_fixed_to_float(fx), _fixed_to_float(fy))
            if self._drag_mode == "":
                if time.monotonic() - self._drag_started < _RELATIVE_GRACE_S:
                    return
                self._drag_mode = "local"
                self._drag_anchor_pos = self._pos
                self._drag_anchor_local = local
                return
            if self._drag_anchor_local is None:
                return
            self.move(*drag_target(self._drag_anchor_pos, self._drag_anchor_local, local))

        def on_button(_d: Any, _p: Any, _serial: int, _t: int, button: int,
                      state: int) -> None:
            self._buttons_seen += 1
            if button != _BTN_LEFT:
                return
            if int(state) == _WL_POINTER_STATE_PRESSED:
                if self._draggable:
                    self._drag_active = True
                    self._drag_mode = ""
                    self._drag_anchor_pos = self._pos
                    self._drag_anchor_local = None
                    self._drag_started = time.monotonic()
                    print(f"{_LOG} 已抓住面板（拖动中，放开结束）", flush=True)
            else:
                if self._drag_active:
                    self._drag_active = False
                    self._drag_mode = ""
                    self._drag_anchor_local = None
                    # 跨屏落位：拖动中面板被夹在本输出边缘（中途换面会打断指针 grab）——
                    # 松开后补一次 move()：此时不在拖动中，会走安全的重建 layer 面路径，
                    # 把面换到指针松手时所在的那块屏上（落点即指针位置）。
                    self.move(*self._pos)
                    if self._on_drag_end is not None:
                        try:
                            self._on_drag_end(self._pos[0], self._pos[1])
                        except Exception as exc:  # noqa: BLE001
                            print(f"{_LOG} ⚠️ 拖动回调出错（已忽略）："
                                  f"{type(exc).__name__}: {exc}", flush=True)
                    print(f"{_LOG} 拖动结束，落点 {self._pos}", flush=True)

        listener = _PointerListener(
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                ctypes.c_int32, ctypes.c_int32)(lambda *a: None),
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                ctypes.c_void_p)(lambda *a: None),
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                ctypes.c_int32, ctypes.c_int32)(on_motion),
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_uint32, ctypes.c_uint32)(on_button),
            _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_int32)(lambda *a: None),
            _cb(ctypes.c_void_p, ctypes.c_void_p)(lambda *a: None))
        self._listeners.append(listener)
        self._attach_listener(pointer, listener)

        # 测试钩子：跳过 relative-pointer 对象，强制走「本地坐标法」
        # （用于在嵌套合成器里模拟 niri 那类不发 relative_motion 的会话）。
        if self._rel_manager is not None and not os.environ.get("VLT_WAYLAND_NO_RELATIVE"):
            rel = self._req(self._rel_manager, _REL_MANAGER_GET,
                            [("n", None), ("o", pointer)],
                            interface=ctypes.addressof(iface_rel_pointer), version=1)
            if not rel:
                return
            self._rel_pointer = rel

            def on_rel(_d: Any, _p: Any, _hi: int, _lo: int, dx: int, dy: int,
                       _dxu: int, _dyu: int) -> None:
                if not self._drag_active or self._drag_mode == "local":
                    return                       # 已切本地坐标法：别两个源都算
                self._drag_mode = "relative"
                nx = self._pos[0] + int(round(_fixed_to_float(dx)))
                ny = self._pos[1] + int(round(_fixed_to_float(dy)))
                self.move(nx, ny)

            listener2 = _RelPointerListener(
                _cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                    ctypes.c_int32, ctypes.c_int32, ctypes.c_int32,
                    ctypes.c_int32)(on_rel))
            self._listeners.append(listener2)
            self._attach_listener(rel, listener2)

    # ---------- 对外（窗口契约） ----------

    def set_panel(self, image: Image.Image, alpha: float | None = None) -> None:
        """贴一帧面板；`alpha=None` 保持当前整层乘子（窗口契约，见 base.DesktopWindow）。"""
        self._panel = image
        if alpha is not None:
            self._alpha = clamp01(alpha)
        self._dirty = True
        self._draw()

    def move(self, x: int, y: int) -> None:
        """移到全局坐标；自动挑输出。拖动过程中**不换面**（换面会打断指针 grab）。"""
        if self._dead:
            return
        self._pos = (int(x), int(y))
        outs = self._output_infos()
        if not outs:
            return
        out = pick_output(self._pos, self._size, outs)
        if out is None:
            return
        gname = self._global_for_output(out)
        if not self._drag_active and (self._surface is None or self._output_global != gname):
            try:
                self._ensure_surface(out, wait_configure=False)
            except Exception as exc:  # noqa: BLE001 — 建面失败也停更而不是崩
                self._mark_dead(f"建面失败 {type(exc).__name__}: {exc}")
                return
        elif self._output is None:
            self._output = out
        self._apply_layer_state(commit=True)

    def set_size(self, size: tuple[int, int]) -> None:
        size = (max(1, int(size[0])), max(1, int(size[1])))
        if size == self._size:
            return
        self._size = size
        try:
            self._rebuild_buffers()
        except Exception as exc:  # noqa: BLE001
            self._mark_dead(f"改尺寸失败 {type(exc).__name__}: {exc}")
            return
        self._apply_layer_state(commit=True)
        self._dirty = True

    def set_alpha(self, alpha: float) -> None:
        a = clamp01(alpha)
        if abs(a - self._alpha) < 1e-6:
            return
        self._alpha = a
        self._dirty = True
        self._draw()

    def set_click_through(self, on: bool) -> None:
        on = bool(on)
        if on == self._click_through:
            return
        self._click_through = on
        self._apply_input_region(commit=True)

    def set_draggable(self, on: bool) -> None:
        on = bool(on)
        if on == self._draggable:
            return
        self._draggable = on
        if not on:
            self._drag_active = False
            self._drag_mode = ""
            self._drag_anchor_local = None
        self._apply_input_region(commit=True)

    def tick(self) -> None:
        if self._dead:
            return
        if not self._pump_events():
            return
        if self._dirty and self._configured:
            self._draw()

    @property
    def position(self) -> tuple[int, int]:
        return self._pos

    def close(self) -> None:
        if self._display is None and not self._buffers and self._surface is None:
            self.available = False
            return
        self._teardown()
        self.available = False

    # ---------- marshalling ----------

    def _req(self, proxy: Any, opcode: int, args: list[tuple[str, Any]],
             interface: int | None = None, version: int = 0,
             flags: int = 0) -> Any:
        """转调模块级 `marshal_request`（文档见那里）。"""
        return marshal_request(self._lib, proxy, opcode, args, interface, version, flags)

    # ---------- 收尾 ----------

    def _teardown(self) -> None:
        if self._lib is None:
            self._dead = True
            return
        try:
            self._drag_active = False
            self._release_buffers()
            self._destroy_surface()
            for proxy in (self._pointer, self._rel_pointer, self._rel_manager,
                          self._xdg_manager, self._seat, self._layer_shell, self._shm,
                          self._compositor, self._registry):
                if proxy:
                    try:
                        self._lib.wl_proxy_destroy(_ptr(proxy))
                    except Exception:  # noqa: BLE001
                        pass
            self._pointer = self._rel_pointer = self._rel_manager = self._seat = None
            self._xdg_manager = None
            self._layer_shell = self._shm = self._compositor = self._registry = None
            for st in self._outputs.values():
                xa = st.get("xdg_addr")
                if xa:
                    try:
                        self._lib.wl_proxy_destroy(ctypes.c_void_p(int(xa)))
                    except Exception:  # noqa: BLE001
                        pass
            for addr in self._output_proxies:
                try:
                    self._lib.wl_proxy_destroy(ctypes.c_void_p(int(addr)))
                except Exception:  # noqa: BLE001
                    pass
            self._output_proxies.clear()
            self._outputs.clear()
            if self._display:
                try:
                    self._lib.wl_display_flush(_ptr(self._display))
                except Exception:  # noqa: BLE001
                    pass
                self._lib.wl_display_disconnect(_ptr(self._display))
        except Exception as exc:  # noqa: BLE001
            print(f"{_LOG} ⚠️ 关闭原生 Wayland 窗时报错（已忽略）："
                  f"{type(exc).__name__}: {exc}", flush=True)
        finally:
            self._display = None
            self._listeners.clear()
            self._dead = True
            self.available = False
