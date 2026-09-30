"""手腕屏的 **OpenXR** 后端（Linux 独占）。

这是 Windows 侧 `openvr.IVROverlay()` 的**结构对等物**：我们的进程作为
`XR_EXTX_overlay` overlay session 直接连 Monado/WiVRn 的合成器，
**不需要任何第三方 overlay 管理器、不需要注册面板、不写配置文件、不重启任何服务**。

## 下面每一条都是 spike 实测出来的，不是推断（`scripts/spike_openxr.py`）

| 结论 | 依据 |
|---|---|
| 图形绑定只能用 **`XR_MNDX_egl_enable` + `GraphicsBindingEGLMNDX`** | Monado 的 `oxr_session.c` 只分发 XLIB/WIN32/ES_ANDROID/VULKAN/**EGL_MNDX**/D3D，**没有 `OPENGL_WAYLAND_KHR`** |
| 会话链 = `SessionCreateInfo → GraphicsBindingEGLMNDX → SessionCreateInfoOverlayEXTX` | `createFlags` 必须为 0（规范要求） |
| 建 session **前**必须调 `xrGetOpenGLGraphicsRequirementsKHR` | Monado 检查 `sys->gotten_requirements`，否则 `GRAPHICS_REQUIREMENTS_CALL_MISSING` |
| 建完 session **必须泵事件到 READY 再 `xrBeginSession`** | `oxr_session_begin()` 首句就要求 `XR_SESSION_STATE_READY`，否则 `SESSION_NOT_RUNNING` |
| swapchain 选 `GL_RGBA8 (0x8058)`，acquire 后必须 `wait_swapchain_image` | 实测可用格式列表里 0x8058 在；漏 wait 会「上传成功但画面不动」 |
| Quad ✅ / **Cylinder ✅**（弯曲可用） | spike E/F 两项实测通过 |
| 手部 pose **可用** | spike G 实测 `position_tracked`，profile 匹配到 `oculus/touch_controller` |

## 踩过的坑（都有注释标出，别重蹈）

1. **`xrPollEvent` 的结果要从缓冲区起始强转**，不能从 `varying[]` 起 ——
   `EventDataBuffer{type(0),next(8),varying(16)}` vs `EventDataSessionStateChanged{type(0),next(8),session(16),state(24)}`，从 varying 读会偏 16 字节、读到垃圾 0。
2. **必须调 `xrSyncActions`**，否则动作状态永远不更新（pose 读不到）。
3. **X11/GLX 走不通**：niri 下 X11 是精简的 `xwayland-satellite`，
   `glXChooseFBConfig`/`glXCreatePbuffer` 能过但**所有** context 创建方式都被拒。
   所以走 libwayland-client + libEGL。
4. 建议**多个** interaction profile，别只给一个。
5. `xrWaitFrame` 没有超时参数。
"""
from __future__ import annotations

import ctypes
import logging
import math
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .overlay import OverlayConfig, render_conversation, render_panel

log = logging.getLogger(__name__)


# ================================================================ 纯逻辑（离线可测）

# HTC Vive tracker 的 role 路径表（OpenXR 按 **role** 寻址 tracker，不像 OpenVR 那样按设备索引）。
# ⚠️ 未实测（spike 时用户用的是手柄不是 tracker）—— 所以 `tracker` 锚点在 Linux 上
#    是「按 role 顺序找一个能追踪的」，与 Windows 的「第 N 个 GenericTracker」语义略有差别。
TRACKER_ROLES = (
    "right_wrist", "left_wrist", "right_elbow", "left_elbow",
    "chest", "waist", "right_foot", "left_foot",
)


def euler_to_quaternion(rot_deg: tuple[float, float, float]) -> tuple[float, float, float, float]:
    """欧拉角（度）→ 四元数 `(x, y, z, w)`。

    ⚠️ **顺序必须与 Windows 侧后端的 `openvr_overlay.build_matrix` 完全一致**（`Rz * Ry * Rx`），
    否则用户实测调好的 `rot: [-47, -16, 0]` 在 Linux 上会转到别的方向 ——
    配置语义必须两端一致，不然「同一个 config.yaml 两边都好看」就不成立了。

    实现上直接用四元数乘法 `qz ⊗ qy ⊗ qx`，比手写展开可靠。
    """
    rx, ry, rz = (math.radians(a) / 2.0 for a in rot_deg)
    qz = (math.cos(rz), 0.0, 0.0, math.sin(rz))     # (w, x, y, z)
    qy = (math.cos(ry), 0.0, math.sin(ry), 0.0)
    qx = (math.cos(rx), math.sin(rx), 0.0, 0.0)
    w, x, y, z = _qmul(_qmul(qz, qy), qx)
    return (x, y, z, w)


def _qmul(a: tuple[float, float, float, float],
          b: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """四元数乘（Hamilton，`(w, x, y, z)` 顺序）。"""
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def anchor_paths(anchor: str, tracker_index: int = 0) -> tuple[str | None, str | None]:
    """配置里的 `anchor` → (完整 pose 路径, 顶层用户路径)。

    返回 `(None, None)` 表示该锚点不用 action（`hmd` 用 VIEW 参考空间）。
    """
    if anchor == "left_hand":
        return "/user/hand/left/input/grip/pose", "/user/hand/left"
    if anchor == "right_hand":
        return "/user/hand/right/input/grip/pose", "/user/hand/right"
    if anchor == "tracker":
        idx = max(0, min(int(tracker_index), len(TRACKER_ROLES) - 1))
        base = f"/user/vive_tracker_htcx/role/{TRACKER_ROLES[idx]}"
        return f"{base}/input/grip/pose", base
    return None, None                      # hmd / 未知 → 用 VIEW 参考空间


def pick_swapchain_format(formats: list[int]) -> int:
    """挑一个 8 位 RGBA 的 GL 内部格式。

    ⚠️ 不能直接取 `formats[0]`：实测列表里第一个是 0x805b（RGBA16F），
    而我们按 8 位上传 → 画面会错位。顺序：RGBA8 → sRGB8_ALPHA8 → RGB10_A2。
    """
    for cand in (0x8058, 0x8C43, 0x8059):
        if cand in formats:
            return cand
    return formats[0] if formats else 0x8058


def layer_geometry(width_m: float, aspect: float, curvature: float) -> dict:
    """把「面板宽 + 宽高比 + 弯曲度」换算成合成层参数。

    `curvature` 沿用 Windows 侧 `setOverlayCurvature` 的 0~1 语义：
    0 = 平面（Quad），>0 = 柱面（Cylinder）。半径由弧长关系反推，保证**弦长仍是 width_m**。
    """
    aspect = aspect if aspect > 0 else 1.0
    if curvature <= 0.0:
        return {"kind": "quad", "size": (width_m, width_m / aspect)}
    angle = max(0.2, min(1.5, curvature * 5.0))
    return {"kind": "cylinder", "radius": width_m / angle,
            "central_angle": angle, "aspect_ratio": aspect}


def should_rebuild(old: OverlayConfig, new: OverlayConfig) -> tuple[bool, bool]:
    """配置热重载时判断要做什么：返回 `(几何/锚点变了, 渲染参数变了)`。

    几何变了要重应用变换（位置/宽/透明度/弯曲/锚点）；
    渲染参数变了必须**重新渲染一帧贴图**，否则界面上拖字号滑块会「看着生效、屏上没变」。

    与 `openvr_overlay.WristOverlay.tick()` 里的判断口径保持一致（刻意重复这两组比较，
    不动那边已经过测试的代码）。
    """
    geo = (new.pos, new.rot, new.width_m, new.alpha, new.curvature) != (
        old.pos, old.rot, old.width_m, old.alpha, old.curvature)
    anchor = (new.anchor != old.anchor or new.tracker_index != old.tracker_index)
    render = (new.font_size != old.font_size
              or new.source_font_size != old.source_font_size
              or new.size_px != old.size_px
              or new.max_lines != old.max_lines
              or new.show_source != old.show_source)
    return (geo or anchor), render


# ================================================================ GL / EGL（Wayland surfaceless）

EGL_PLATFORM_WAYLAND_KHR = 0x31D8
EGL_OPENGL_API = 0x30A2
EGL_NONE = 0x3038
EGL_SURFACE_TYPE, EGL_PBUFFER_BIT = 0x3033, 0x0001
EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT = 0x3040, 0x0008
EGL_RED_SIZE, EGL_GREEN_SIZE, EGL_BLUE_SIZE, EGL_ALPHA_SIZE = 0x3024, 0x3023, 0x3022, 0x3021
EGL_CONTEXT_MAJOR_VERSION, EGL_CONTEXT_MINOR_VERSION = 0x3098, 0x30FB
EGL_NO_SURFACE = 0
GL_TEXTURE_2D, GL_RGBA, GL_UNSIGNED_BYTE = 0x0DE1, 0x1908, 0x1401
GL_VENDOR, GL_RENDERER, GL_VERSION = 0x1F00, 0x1F01, 0x1F02


class EglGlContext:
    """Wayland + EGL 的 surfaceless GL context（纯 ctypes，不引 PyOpenGL/glfw 做 GL 调用）。

    ⚠️ 每个 extern 函数都要写全 `argtypes`/`restype` —— 不写的话 ctypes 把 64 位指针
    当 32 位 int 传，直接段错误（spike 阶段实测崩过一次）。
    """

    def __init__(self) -> None:
        self.wl = ctypes.CDLL("libwayland-client.so.0")
        self.egl = ctypes.CDLL("libEGL.so.1")
        self.gl = ctypes.CDLL("libGL.so.1")
        self._bind()

        self.wl_display = self.wl.wl_display_connect(None)
        if not self.wl_display:
            raise RuntimeError("wl_display_connect 失败（WAYLAND_DISPLAY 没设？）")
        self.egl_display = self.egl.eglGetPlatformDisplay(
            EGL_PLATFORM_WAYLAND_KHR, self.wl_display, None)
        if not self.egl_display:
            raise RuntimeError(f"eglGetPlatformDisplay 失败 0x{self.egl.eglGetError():x}")
        maj, minr = ctypes.c_int(), ctypes.c_int()
        if not self.egl.eglInitialize(self.egl_display, ctypes.byref(maj), ctypes.byref(minr)):
            raise RuntimeError(f"eglInitialize 失败 0x{self.egl.eglGetError():x}")
        if not self.egl.eglBindAPI(EGL_OPENGL_API):
            raise RuntimeError(f"eglBindAPI 失败 0x{self.egl.eglGetError():x}")
        cfg_attribs = (ctypes.c_int * 13)(
            EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
            EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE)
        self.egl_config = ctypes.c_void_p()
        num = ctypes.c_int()
        if not self.egl.eglChooseConfig(self.egl_display, cfg_attribs,
                                        ctypes.byref(self.egl_config), 1, ctypes.byref(num)) \
                or num.value == 0:
            raise RuntimeError(f"eglChooseConfig 失败 0x{self.egl.eglGetError():x}")
        ctx_attribs = (ctypes.c_int * 5)(EGL_CONTEXT_MAJOR_VERSION, 3,
                                         EGL_CONTEXT_MINOR_VERSION, 3, EGL_NONE)
        self.egl_context = self.egl.eglCreateContext(self.egl_display, self.egl_config,
                                                     None, ctx_attribs)
        if not self.egl_context:
            self.egl_context = self.egl.eglCreateContext(self.egl_display, self.egl_config,
                                                         None, None)
        if not self.egl_context:
            raise RuntimeError(f"eglCreateContext 失败 0x{self.egl.eglGetError():x}")
        if not self.egl.eglMakeCurrent(self.egl_display, EGL_NO_SURFACE, EGL_NO_SURFACE,
                                       self.egl_context):
            raise RuntimeError(f"eglMakeCurrent(surfaceless) 失败 0x{self.egl.eglGetError():x}")

    def _bind(self) -> None:
        self.wl.wl_display_connect.argtypes = [ctypes.c_char_p]
        self.wl.wl_display_connect.restype = ctypes.c_void_p
        self.wl.wl_display_disconnect.argtypes = [ctypes.c_void_p]
        self.wl.wl_display_disconnect.restype = None
        self.egl.eglGetPlatformDisplay.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        self.egl.eglGetPlatformDisplay.restype = ctypes.c_void_p
        self.egl.eglInitialize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                           ctypes.POINTER(ctypes.c_int)]
        self.egl.eglInitialize.restype = ctypes.c_uint
        self.egl.eglBindAPI.argtypes = [ctypes.c_uint]
        self.egl.eglBindAPI.restype = ctypes.c_uint
        self.egl.eglChooseConfig.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                             ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
                                             ctypes.POINTER(ctypes.c_int)]
        self.egl.eglChooseConfig.restype = ctypes.c_uint
        self.egl.eglCreateContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self.egl.eglCreateContext.restype = ctypes.c_void_p
        self.egl.eglMakeCurrent.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_void_p]
        self.egl.eglMakeCurrent.restype = ctypes.c_uint
        self.egl.eglGetError.restype = ctypes.c_uint
        self.egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
        self.egl.eglGetProcAddress.restype = ctypes.c_void_p
        self.gl.glBindTexture.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self.gl.glBindTexture.restype = None
        self.gl.glTexImage2D.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        self.gl.glTexImage2D.restype = None
        self.gl.glTexSubImage2D.argtypes = [ctypes.c_uint, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int,
                                            ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        self.gl.glTexSubImage2D.restype = None
        self.gl.glGetString.argtypes = [ctypes.c_uint]
        self.gl.glGetString.restype = ctypes.c_char_p
        self.gl.glGetError.argtypes = []
        self.gl.glGetError.restype = ctypes.c_uint
        self.gl.glFinish.argtypes = []
        self.gl.glFinish.restype = None

    def get_proc_address_addr(self) -> int:
        """Monado 会**真的调用**我们提供的 `getProcAddress`。"""
        return ctypes.cast(self.egl.eglGetProcAddress, ctypes.c_void_p).value

    def gl_string(self, which: int) -> str:
        p = self.gl.glGetString(which)
        return p.decode() if p else "?"

    def upload(self, texture_id: int, width: int, height: int, rgba: bytes) -> None:
        """把一张 RGBA 贴图写进 swapchain 给我们的 GL 纹理。

        ⚠️ 必须用 `glTexSubImage2D`，**不能**用 `glTexImage2D`：swapchain 的纹理是运行时
        已经完整分配的（immutable），重新 `glTexImage2D` 会返回 `GL_INVALID_OPERATION
        (0x502)` —— 上传静默失败、纹理保持初始黑色。实测现象就是「面板纯黑、没有任何文字」。

        ⚠️ 还要按行倒序：OpenGL 纹理原点在**左下**，而 PIL 图像是**自上而下**，
        不翻转的话面板内容整体倒置。
        """
        import numpy as np
        arr = np.frombuffer(rgba, dtype=np.uint8).reshape(height, width, 4)[::-1]
        buf = ctypes.create_string_buffer(arr.tobytes(), len(rgba))
        self.gl.glBindTexture(GL_TEXTURE_2D, texture_id)
        self.gl.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, width, height,
                                GL_RGBA, GL_UNSIGNED_BYTE, buf)
        err = self.gl.glGetError()
        if err:
            log.warning("[overlay:xr] GL 上传出错 glGetError=0x%x（texture=%s %dx%d）",
                        err, texture_id, width, height)
        self.gl.glFinish()

    def close(self) -> None:
        for fn, args in ((self.egl.eglMakeCurrent,
                          (self.egl_display, EGL_NO_SURFACE, EGL_NO_SURFACE, None)),
                         (self.wl.wl_display_disconnect, (self.wl_display,))):
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                pass


# ================================================================ OpenXR 会话


def _test_process_guard() -> str | None:
    """防呆：**测试进程里拒绝建立 OpenXR 会话**。

    为什么要有这条：Overlay 会话是连到用户**正在用的** XR 合成器上的。
    调试期间反复建/断会话会直接干扰用户当时的 OpenXR 应用（实测被投诉过）。
    单元测试只需要验配置/渲染/纯逻辑，**不该**碰运行时 —— 真机验证应当是
    用户明确授权的单独一次，而不是「顺手跑一下测试」。

    正常使用（`python -m vlt.gui` / `-m vlt.app`）与专门的诊断脚本
    （`scripts/spike_openxr.py`，在 scripts/ 下）都不会命中这个判断。
    """
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    if not path:
        return None
    p = Path(path)
    if p.name.startswith("test_") or "tests" in p.parts:
        return f"检测到测试进程（{p.name}）→ 拒绝建立 OpenXR 会话"
    return None


class XrError(RuntimeError):
    """OpenXR 调用失败（带上是哪一步，便于日志定位）。"""


class XrOverlaySession:
    """一套「overlay session + swapchain + 每帧提交」的最小封装。

    刻意做成薄封装：所有 xr 调用集中在这里，`OpenXrOverlay` 只管业务
    （渲染、热重载、自愈），两层职责分明。
    """

    def __init__(self, gl: "EglGlContext", size_px: tuple[int, int]) -> None:
        self._gl = gl
        self.size_px = size_px
        self.instance: Any = None
        self.system_id: Any = None
        self.session: Any = None
        self.swapchain: Any = None
        self.textures: list[int] = []
        self.format = 0
        self.ref_space: Any = None          # LOCAL
        self.view_space: Any = None         # VIEW（anchor=hmd 用）
        self.action_set: Any = None
        self._action_map: dict[str, Any] = {}     # anchor_key → (action, space)
        self._anchor_key: tuple[str, int] | None = None
        self._frame_state: Any = None
        # 会话状态（IDLE→READY→SYNCHRONIZED→VISIBLE→FOCUSED）。**很关键**：
        # `xrSyncActions` 在非 FOCUSED 时会直接抛 `XR_ERROR_SESSION_NOT_FOCUSED`
        # （Monado 源码 oxr_input.c 明写 "Can only call this function if the session
        #  state is focused"），所以提交前要看一眼它。
        self.state: Any = None

    # ---------- 建立 / 销毁 ----------
    def create(self, extensions: list[str]) -> None:
        """建 instance → system → overlay session（三层都建，任一层失败就抛）。"""
        import xr
        self._xr = xr
        self.instance = xr.create_instance(xr.InstanceCreateInfo(
            application_info=xr.ApplicationInfo(application_name="VRChat LiveTranslate",
                                                application_version=1),
            enabled_extension_names=extensions))
        self.system_id = xr.get_system(self.instance)
        self._begin_session()
        self._create_ref_spaces()
        self._create_swapchain()
        log.info("[overlay:xr] 会话就绪：%sx%s format=0x%x，扩展 %d 个",
                 self.size_px[0], self.size_px[1], self.format, len(extensions))

    def _begin_session(self) -> None:
        """建 overlay session 并推进到 running。"""
        import xr
        overlay_info = xr.SessionCreateInfoOverlayEXTX(
            create_flags=xr.OverlaySessionCreateFlagsEXTX(0),   # 规范要求必须 0
            session_layers_placement=100)
        binding = self._egl_binding()
        # 链顺序：SessionCreateInfo → GraphicsBinding → Overlay（顺序不能反）
        binding.next = ctypes.cast(ctypes.pointer(overlay_info), ctypes.c_void_p)
        create_info = xr.SessionCreateInfo(system_id=self.system_id)
        create_info.next = ctypes.cast(ctypes.pointer(binding), ctypes.c_void_p)

        # ⚠️ 必须先查图形需求：Monado 的 EGL 分支检查 `gotten_requirements`
        xr.get_opengl_graphics_requirements_khr(self.instance, self.system_id)
        self.session = xr.create_session(self.instance, create_info)

        # ⚠️ 建完还要泵事件到 READY 才能 begin（否则 SESSION_NOT_RUNNING）
        deadline = time.monotonic() + 5.0
        state = None
        while time.monotonic() < deadline:
            s = self.pump_events()
            if s is not None:
                state = s
            if state == xr.SessionState.READY:
                break
            if state in (xr.SessionState.EXITING, xr.SessionState.LOSS_PENDING):
                raise XrError(f"会话进入 {state}")
            time.sleep(0.01)
        else:
            raise XrError(f"等 READY 超时（当前 {state}）")

        configs = list(xr.enumerate_view_configurations(self.instance, self.system_id))
        want = xr.ViewConfigurationType.PRIMARY_STEREO
        vct = want if want in configs else (configs[0] if configs else want)
        xr.begin_session(self.session, xr.SessionBeginInfo(primary_view_configuration_type=vct))
        for _ in range(50):
            self.pump_events()
            time.sleep(0.01)

    def _egl_binding(self) -> Any:
        """构造 `GraphicsBindingEGLMNDX`（三个指针字段的类型来自 PyOpenGL）。

        ⚠️ 不能给 int / c_void_p，必须 `ctypes.cast(c_void_p(addr), 该类型)`，
            否则报 "expected EGLDisplay instead of int"。
        """
        import xr
        f = xr.GraphicsBindingEGLMNDX._fields_
        pfn_t, disp_t, cfg_t, ctx_t = f[0][1], f[1][1], f[2][1], f[3][1]
        return xr.GraphicsBindingEGLMNDX(
            get_proc_address=pfn_t(self._gl.get_proc_address_addr()),
            display=ctypes.cast(ctypes.c_void_p(self._gl.egl_display), disp_t),
            config=ctypes.cast(ctypes.c_void_p(self._gl.egl_config.value), cfg_t),
            context=ctypes.cast(ctypes.c_void_p(self._gl.egl_context), ctx_t))

    def _create_ref_spaces(self) -> None:
        # ⚠️ `create_reference_space` 的第一个参数是 **session**，不是 instance。
        #    写错时错误信息很不直观（`expected Session instance instead of Instance`），
        #    而且参数**个数**是对的、静态扫不出来 —— 这条是真机冒烟才抓到的。
        #    `tests/test_openxr_overlay.py` 里有一条专门扫 self.* 句柄错配的用例守着它。
        import xr
        self.ref_space = xr.create_reference_space(self.session, xr.ReferenceSpaceCreateInfo(
            reference_space_type=xr.ReferenceSpaceType.LOCAL))
        self.view_space = xr.create_reference_space(self.session, xr.ReferenceSpaceCreateInfo(
            reference_space_type=xr.ReferenceSpaceType.VIEW))

    def _create_swapchain(self) -> None:
        import xr
        self.swapchain = None
        formats = list(xr.enumerate_swapchain_formats(self.session))
        self.format = pick_swapchain_format(formats)
        w, h = self.size_px
        self.swapchain = xr.create_swapchain(self.session, xr.SwapchainCreateInfo(
            format=self.format, sample_count=1, width=w, height=h,
            face_count=1, array_size=1, mip_count=1))
        imgs = xr.enumerate_swapchain_images(self.swapchain, xr.SwapchainImageOpenGLKHR)
        self.textures = [int(i.image) for i in imgs]

    def rebuild_swapchain(self) -> None:
        """贴图上传反复失败时的第一级恢复。"""
        import xr
        if self.swapchain is not None:
            try:
                xr.destroy_swapchain(self.swapchain)
            except Exception:  # noqa: BLE001
                pass
        self._create_swapchain()

    # ---------- 锚点 ----------
    def ensure_anchor(self, anchor: str, tracker_index: int) -> None:
        """确保配置要求的锚点空间已建立（改锚点时重建）。"""
        key = (anchor, int(tracker_index))
        if key == self._anchor_key:
            return
        import xr
        full, top = anchor_paths(anchor, tracker_index)
        if full is None:
            self._anchor_key = key          # hmd → 直接用 view_space
            return
        if self.action_set is None:
            self.action_set = xr.create_action_set(self.instance, xr.ActionSetCreateInfo(
                action_set_name="vlt_wrist", localized_action_set_name="VLT Wrist Panel",
                priority=0))
        name = f"pose_{anchor}_{tracker_index}"
        act = xr.create_action(self.action_set, xr.ActionCreateInfo(
            action_name=name, action_type=xr.ActionType.POSE_INPUT,
            localized_action_name="Wrist Anchor",
            subaction_paths=[xr.string_to_path(self.instance, top)]))
        # 建议多个 profile —— 只给一个的话，控制器型号对不上就完全没有 pose
        for profile in ("/interaction_profiles/khr/simple_controller",
                        "/interaction_profiles/oculus/touch_controller",
                        "/interaction_profiles/valve/index_controller",
                        "/interaction_profiles/microsoft/motion_controller",
                        "/interaction_profiles/htc/vive_controller"):
            try:
                xr.suggest_interaction_profile_bindings(
                    self.instance, xr.InteractionProfileSuggestedBinding(
                        interaction_profile=xr.string_to_path(self.instance, profile),
                        suggested_bindings=[xr.ActionSuggestedBinding(
                            action=act, binding=xr.string_to_path(self.instance, full))]))
            except Exception:  # noqa: BLE001 — 运行时不认的 profile 跳过
                pass
        xr.attach_session_action_sets(self.session, xr.SessionActionSetsAttachInfo(
            action_sets=[self.action_set]))
        space = xr.create_action_space(self.session, xr.ActionSpaceCreateInfo(
            action=act, subaction_path=xr.string_to_path(self.instance, top),
            pose_in_action_space=xr.Posef(
                orientation=xr.Quaternionf(0.0, 0.0, 0.0, 1.0),
                position=xr.Vector3f(0.0, 0.0, 0.0))))
        self._action_map = {name: (act, space)}
        self._anchor_key = key

    def anchor_space(self, anchor: str) -> Any:
        """层要挂到哪个 space。

        ⚠️ action space 在动作同步生效前是**未追踪**的，层会落到 LOCAL 原点 ——
        实测表现就是「面板固定不动、不跟手也不跟头」。所以这里做一次回退：
        锚点没被追踪时改用 `view_space`（至少跟着头），sync 生效后下一帧自然切回。
        """
        if anchor == "hmd":
            return self.view_space
        if self._action_map:
            space = next(iter(self._action_map.values()))[1]
            if self._space_tracked(space):
                return space
            return self.view_space
        return self.view_space

    def _space_tracked(self, space: Any) -> bool:
        """这个 space 当前是否带位置追踪（locate 一次看标志位）。"""
        import xr
        fr = self._frame_state
        t = int(getattr(fr, "predicted_display_time", 0) or 0) or time.monotonic_ns()
        try:
            loc = xr.locate_space(space, self.ref_space, xr.Time(t))
            return bool(int(loc.location_flags)
                        & int(xr.SpaceLocationFlags.POSITION_TRACKED_BIT))
        except Exception:  # noqa: BLE001
            return False

    def anchor_tracked(self, anchor: str) -> bool | None:
        """锚点是否被追踪（诊断用；拿不到返回 None）。"""
        import xr
        if anchor == "hmd" or not self._action_map:
            return None
        space = next(iter(self._action_map.values()))[1]
        try:
            self._frame_state = self._frame_state or xr.wait_frame(self.session)
            loc = xr.locate_space(space, self.ref_space,
                                  xr.Time(self._frame_state.predicted_display_time
                                          or time.monotonic_ns()))
            return bool(int(loc.location_flags)
                        & int(xr.SpaceLocationFlags.POSITION_TRACKED_BIT))
        except Exception:  # noqa: BLE001
            return None

    # ---------- 事件 / 每帧 ----------
    def pump_events(self) -> Any:
        """把事件队列取空，返回最后见到的 session state。

        ⚠️ 必须从**缓冲区起始**强转（不是 `varying`）：`EventDataBuffer.varying` 在偏移 16，
        而 `EventDataSessionStateChanged.session/state` 在 16/24 —— 从 varying 起算会偏 16 字节，
        读出来的 state 是垃圾（实测得到 0=UNKNOWN，害我误判成「收不到事件」）。
        """
        import xr
        state = None
        want = int(xr.StructureType.EVENT_DATA_SESSION_STATE_CHANGED)
        while True:
            try:
                ev = xr.poll_event(self.instance)
            except Exception:  # noqa: BLE001 — XR_EVENT_UNAVAILABLE：队列空了
                break
            try:
                etype = int(ev.type)
            except Exception:  # noqa: BLE001
                break
            if etype == want:
                ssc = ctypes.cast(ctypes.pointer(ev),
                                  ctypes.POINTER(xr.EventDataSessionStateChanged)).contents
                try:
                    state = xr.SessionState(int(ssc.state))
                    self.state = state
                except ValueError:
                    pass
        return state

    def submit(self, image: Any, cfg: OverlayConfig) -> None:
        """把一张 PIL 图作为一层提交上去。失败抛异常（由上层决定自愈）。"""
        import xr
        w, h = image.size
        self.pump_events()
        frame = xr.wait_frame(self.session)
        self._frame_state = frame
        xr.begin_frame(self.session)
        idx = xr.acquire_swapchain_image(self.swapchain)
        # ⚠️ 拿到 image 后必须 wait 才能写
        xr.wait_swapchain_image(self.swapchain, xr.SwapchainImageWaitInfo(
            timeout=1_000_000_000))
        if self.action_set is not None and self.state is not None \
                and int(self.state) >= int(xr.SessionState.SYNCHRONIZED):
            # ⚠️ 动作必须**每帧**同步。只同步一次、或只在 FOCUSED 时才同步，pose 会一直停在
            #    「未追踪」→ 层落到 LOCAL 原点：实测表现正是「黑色长方形固定不动、不跟手也不跟头」。
            #    非 FOCUSED 时 Monado 可能抛 `XR_ERROR_SESSION_NOT_FOCUSED`，吃掉即可
            #    （同步失败不该让整帧作废）。
            try:
                xr.sync_actions(self.session, xr.ActionsSyncInfo(active_action_sets=[
                    xr.ActiveActionSet(action_set=self.action_set,
                                       subaction_path=xr.Path(0))]))
            except Exception as exc:  # noqa: BLE001
                log.debug("[overlay:xr] sync_actions 失败（本帧不同步）：%s: %s",
                          type(exc).__name__, exc)
        self._gl.upload(self.textures[idx], w, h, image.tobytes())
        xr.release_swapchain_image(self.swapchain)

        sub = xr.SwapchainSubImage(
            swapchain=self.swapchain,
            image_rect=xr.Rect2Di(offset=xr.Offset2Di(0, 0),
                                  extent=xr.Extent2Di(w, h)))
        pose = xr.Posef(
            orientation=xr.Quaternionf(*euler_to_quaternion(cfg.rot)),
            position=xr.Vector3f(*cfg.pos))
        geo = layer_geometry(cfg.width_m, w / h if h else 1.0, cfg.curvature)
        space = self.anchor_space(cfg.anchor)
        if geo["kind"] == "cylinder":
            layer = xr.CompositionLayerCylinderKHR(
                sub_image=sub, pose=pose, space=space,
                radius=geo["radius"], central_angle=geo["central_angle"],
                aspect_ratio=geo["aspect_ratio"])
        else:
            layer = xr.CompositionLayerQuad(
                sub_image=sub, pose=pose, space=space,
                size=xr.Extent2Df(*geo["size"]),
                eye_visibility=xr.EyeVisibility.BOTH)
        base_t = ctypes.POINTER(xr.CompositionLayerBaseHeader)
        arr = (base_t * 1)(ctypes.cast(ctypes.pointer(layer), base_t))
        xr.end_frame(self.session, xr.FrameEndInfo(
            display_time=frame.predicted_display_time,
            environment_blend_mode=xr.EnvironmentBlendMode.OPAQUE,
            layer_count=1, layers=arr))

    def destroy(self) -> None:
        """幂等销毁（顺序：swapchain → session → instance）。"""
        import xr
        for attr, fn in (("swapchain", "destroy_swapchain"), ("session", "destroy_session"),
                         ("instance", "destroy_instance")):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                getattr(xr, fn)(obj)
            except Exception:  # noqa: BLE001
                pass
            setattr(self, attr, None)


# ================================================================ 对外：与 WristOverlay 同接口

class OpenXrOverlay:
    """手腕屏的 OpenXR 后端。

    公开接口与 `openvr_overlay.WristOverlay`（Windows 侧后端）保持一致，
    所以 `engine.py` / `gui.py` 不需要知道自己在哪个平台：

        cfg / config_path / dry_run / available / frames_updated
        start() / update() / update_entries() / tick() / close()

    自愈分级（照搬 Windows 侧踩出来的思路）：
        贴图失败 → 重建 swapchain → 仍失败 → 重建整个 session → 再失败 → 重建 instance
    ⚠️ OpenXR 没有 `openvr.shutdown()` 那种「一行硬重启」，所以三级都要自己写。
    """

    HEARTBEAT_S = 30.0

    def __init__(self, cfg: OverlayConfig, config_path: Path | None = None,
                 dry_run: bool = False) -> None:
        from ..paths import APP_DIR
        self.cfg = cfg
        self.config_path = config_path
        self.dry_run = dry_run
        self.available = False
        self.frames_updated = 0
        self._gl: EglGlContext | None = None
        self._sess: XrOverlaySession | None = None
        self._last_render: tuple[str, str] | None = None
        self._last_entries: tuple | None = None
        self._last_cfg_mtime = 0.0
        self._frames_dir = APP_DIR / "out" / "overlay_frames"
        # 自愈状态机
        self._fails = 0
        self._stage = "none"            # none / swapchain / session / instance
        self._fails_in_stage = 0
        self._last_ok_at = 0.0
        self._last_heartbeat = 0.0
        self._rebuilds = 0
        self._reinits = 0
        # 「最近一次内容」缓存（热重载 / 自愈后重画用）
        self._last_render_cached: tuple[str, str] | None = None
        self._last_entries_cached: tuple | None = None
        # ★ 后台帧循环：OpenXR 的 composition layer 是**每帧**提交的（内容不变也要重提，
        #   否则面板会在静止几帧后消失 —— 实测「黑色长方形出现一下又没了」就是这个），
        #   而 `xrWaitFrame` 没有超时参数、session 未 running 时会**永久阻塞**
        #   （实测：连续提交会卡死，被 timeout 杀掉退出码 124）。
        #   两件事都不能发生在调用方的线程里（engine 是 asyncio 循环、GUI 是主线程），
        #   所以帧循环放独立线程，`update()` 只渲染并放到「待显示」槽，绝不阻塞。
        self._frame_thread: threading.Thread | None = None
        self._frame_stop = threading.Event()
        self._init_done = threading.Event()
        self._img_lock = threading.Lock()
        self._pending_img: Any | None = None     # 新渲染的一帧（待上传）
        self._shown_img: Any | None = None       # 当前该显示的一帧（每帧重提用）

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        """建立 overlay 会话。失败返回 False（调用方据此禁用这一条腿，别的功能不受影响）。"""
        if not self.cfg.enabled:
            log.info("[overlay:xr] 配置里已禁用")
            return False
        if self.dry_run:
            self._frames_dir.mkdir(parents=True, exist_ok=True)
            log.info("[overlay:xr][dry-run] 不连运行时，渲染结果写到 %s", self._frames_dir)
            return False
        blocked = _test_process_guard()
        if blocked:
            log.warning("[overlay:xr] %s（这条腿不启用；测试不该碰用户的运行时）", blocked)
            return False
        # ★ 整条 XR/GL 生命线必须在**同一个线程**里：`eglMakeCurrent` 把 context 绑在
        #   调用它的线程上，换个线程再 glTexImage2D / xrWaitFrame 就是错的 —— 实测表现
        #   是「帧循环线程里 session 一直停在 READY(2)、提交报 SessionNotRunning」。
        self._frame_stop.clear()
        self._init_done = threading.Event()
        self._frame_thread = threading.Thread(target=self._xr_main, daemon=True, name="vlt-xr")
        self._frame_thread.start()
        if not self._init_done.wait(timeout=30.0):
            log.warning("[overlay:xr] ⚠️ 初始化超时（30s）——手腕屏已禁用，其它输出不受影响")
            self._frame_stop.set()
            return False
        return self.available

    def _xr_main(self) -> None:
        """XR 主循环：建 GL/session → 帧循环 → 清理，**全在这一个线程里**。"""
        try:
            self._gl = EglGlContext()
            self._bring_up(rebuild_gl=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("[overlay:xr] ⚠️ 建立 overlay 会话失败：%s: %s"
                        "（手腕屏已禁用，其它输出不受影响）", type(exc).__name__, exc)
            self._teardown(keep_gl=False)
            self._init_done.set()
            return
        self.available = True
        log.info("[overlay:xr] ✅ 已挂到 %s（%sx%s，GL %s，会话状态 %s）",
                 self.cfg.anchor, self.cfg.size_px[0], self.cfg.size_px[1],
                 self._gl.gl_string(GL_RENDERER), self._sess.state)
        self._init_done.set()
        try:
            self._frame_loop()
        finally:
            self.available = False
            self._teardown(keep_gl=False)     # 同线程清理（GL context 线程绑定）

    def _frame_loop(self) -> None:
        """后台帧循环：泵事件 → 每帧同步动作 → 每帧重提「当前该显示的那一帧」。

        * **每帧**提交是硬要求：OpenXR 的 composition layer 不是持久对象，
          内容不变也要重提，否则面板静止几帧后就没了（实测到的现象）。
        * `xrWaitFrame` **没有超时**，session 未 running 时会永久阻塞 —— 所以只在
          session 至少 READY 时才进帧循环；这样即使运行时抽风，卡住的也只是这条
          daemon 线程，engine 的 asyncio 循环 / GUI 主线程不受影响。
        * `sync_actions` 也在每帧做：动作状态只有同步后才更新，只同步一次的话
          锚点会一直停在「未追踪」→ 面板固定在原点、不跟手也不跟头。
        """
        import xr
        while not self._frame_stop.is_set():
            sess = self._sess
            if sess is None:
                return
            self._reload_config_if_changed()      # 热重载也放这里（ensure_anchor 是 XR 调用）
            try:
                sess.pump_events()
            except Exception:  # noqa: BLE001
                pass
            with self._img_lock:
                img = self._pending_img
                if img is not None:
                    self._pending_img = None
                    self._shown_img = img
                else:
                    img = self._shown_img
            if img is None:
                time.sleep(0.05)
                continue
            state = sess.state
            if state is None or int(state) < int(xr.SessionState.READY):
                time.sleep(0.05)        # 未 running 时绝不进 wait_frame（会吊死）
                continue
            try:
                sess.submit(img, self.cfg)
            except Exception as exc:  # noqa: BLE001
                self._fails += 1
                self._fails_in_stage += 1
                if self._fails == 1:
                    log.warning("[overlay:xr] ❌ 提交层失败：%s: %s", type(exc).__name__, exc)
                if self._fails % 30 == 0:
                    log.warning("[overlay:xr] ❌ 已连续失败 %d 次（面板停在最后一帧）", self._fails)
                self._escalate()
                time.sleep(0.05)
                continue
            self.frames_updated += 1
            self._last_ok_at = time.monotonic()
            if self._fails:
                log.info("[overlay:xr] ✅ 恢复上传（连续失败 %d 次，期间重建 %d 次、重开会话 %d 次）",
                         self._fails, self._rebuilds, self._reinits)
                self._fails = 0
                self._stage = "none"
                self._fails_in_stage = 0
            if self.frames_updated == 1 or self.frames_updated % 300 == 0:
                log.info("[overlay:xr] ← 面板已更新（第 %d 帧）", self.frames_updated)

    def _bring_up(self, *, rebuild_gl: bool) -> None:
        """（重新）建一套 XR 会话，并把当前内容画上去。"""
        import xr
        if rebuild_gl or self._gl is None:
            if self._gl is not None:
                self._gl.close()
            self._gl = EglGlContext()
        self._sess = XrOverlaySession(self._gl, self.cfg.size_px)
        self._sess.create(self._extensions())
        self._sess.ensure_anchor(self.cfg.anchor, self.cfg.tracker_index)
        # 先记下「当前内容」，重建后要照原样画回去（否则自愈后屏幕是空的）
        cached_entries, cached_render = self._last_entries_cached, self._last_render_cached
        self._last_render = None
        self._last_entries = None
        if cached_entries:
            self.update_entries(list(cached_entries), force=True)
        elif cached_render:
            self.update(*cached_render, force=True)

    @staticmethod
    def _extensions() -> list[str]:
        """挑出可用的扩展（运行时不支持的就不启用，免得 create_instance 直接失败）。"""
        import xr
        try:
            have = set()
            for e in xr.enumerate_instance_extension_properties():
                n = e.extension_name
                have.add(n.decode() if isinstance(n, bytes) else str(n))
        except Exception:  # noqa: BLE001
            have = set()
        want = ["XR_EXTX_overlay", "XR_KHR_opengl_enable", "XR_MNDX_egl_enable",
                "XR_KHR_composition_layer_cylinder"]
        return [e for e in want if not have or e in have]

    def close(self) -> None:
        """请求 XR 线程退出并等它收尾。

        teardown 由**该线程自己**执行：GL context 是线程绑定的，换个线程销毁同样不安全。
        """
        self._frame_stop.set()
        th = self._frame_thread
        if th is not None and th.is_alive():
            th.join(timeout=5.0)
            if th.is_alive():
                log.warning("[overlay:xr] XR 线程未在 5s 内退出（可能卡在 wait_frame），放弃等待")
        self._frame_thread = None
        self.available = False

    def _teardown(self, *, keep_gl: bool) -> None:
        if self._sess is not None:
            self._sess.destroy()
            self._sess = None
        if not keep_gl and self._gl is not None:
            self._gl.close()
            self._gl = None

    # ---------- 内容 ----------
    def update(self, text: str, source: str = "", force: bool = False) -> None:
        """渲染一帧并交给后台帧循环 —— **立即返回，不阻塞调用方**。"""
        self._last_render_cached = (text, source)
        if self.dry_run:
            self._write_demo(render_panel(text, source, self.cfg))
            return
        if not force and self._last_render == (text, source):
            return
        self._last_render = (text, source)
        self._last_entries = None
        self._queue_frame(render_panel(text, source, self.cfg))

    def update_entries(self, entries: list, force: bool = False) -> None:
        """渲染会话面板并交给后台帧循环 —— **立即返回，不阻塞调用方**。"""
        entries = list(entries or [])
        self._last_entries_cached = tuple(entries)
        if self.dry_run:
            self._write_demo(render_conversation(entries, self.cfg))
            return
        key = tuple(map(tuple, entries))
        if not force and self._last_entries == key:
            return
        self._last_entries = key
        self._last_render = None
        self._queue_frame(render_conversation(entries, self.cfg))

    def _queue_frame(self, img) -> None:  # noqa: ANN001
        """把一帧放进「待显示」槽（线程安全、立即返回）。

        真正的 `wait_frame → sync_actions → 上传 → end_frame` 由后台帧循环执行：
        `xrWaitFrame` 没有超时，绝不能让它跑在 engine 的事件循环或 GUI 主线程上。
        """
        with self._img_lock:
            self._pending_img = img

    def _write_demo(self, img) -> None:  # noqa: ANN001
        self._frames_dir.mkdir(parents=True, exist_ok=True)
        out = self._frames_dir / "latest.png"
        img.save(out)
        log.info("[overlay:xr][dry-run] 已写出 %s", out)

    # ---------- 每帧 ----------
    # ---------- 自愈 ----------
    def _escalate(self) -> None:
        """分级自愈：连续失败到阈值就升一级（swapchain → session → instance）。

        ⚠️ 刻意限频：失败一次就重启 instance 等于自己把自己拖死。
        """
        if self._fails_in_stage < 3:
            return
        self._fails_in_stage = 0
        if self._stage == "none":
            self._stage = "swapchain"
        elif self._stage == "swapchain":
            self._stage = "session"
        else:
            self._stage = "instance"
        try:
            if self._stage == "swapchain":
                self._sess.rebuild_swapchain()
                log.warning("[overlay:xr] ♻️ 已重建 swapchain")
            elif self._stage == "session":
                self._rebuilds += 1
                self._teardown(keep_gl=True)
                self._bring_up(rebuild_gl=False)
                log.warning("[overlay:xr] ♻️♻️ 已重建 overlay 会话（第 %d 次）", self._rebuilds)
            else:
                self._reinits += 1
                self._teardown(keep_gl=True)
                self._bring_up(rebuild_gl=True)
                log.warning("[overlay:xr] ♻️♻️♻️ 已重建 GL context + 会话（第 %d 次）", self._reinits)
            self._stage = "none"
        except Exception as exc:  # noqa: BLE001
            log.warning("[overlay:xr] ⚠️ 自愈（%s）失败，下轮再试：%s: %s",
                        self._stage, type(exc).__name__, exc)

    def tick(self) -> None:
        """定期调用（engine / GUI 侧）：只留心跳。

        ⚠️ 这里**不做任何 XR 调用**（不泵事件、不热重载、不 locate）—— 整条 XR 生命线由
        `_xr_main` 线程独占；跨线程调 XR 会搅乱状态机（实测：帧线程里 session 停在
        READY、提交报 SessionNotRunning）。热重载也搬进了那条线程。
        """
        if self.dry_run or not self.available or self._sess is None:
            return
        now = time.monotonic()
        if now - self._last_heartbeat >= self.HEARTBEAT_S:
            self._last_heartbeat = now
            age = now - self._last_ok_at if self._last_ok_at else -1.0
            log.info("[overlay:xr][diag] 面板活着：已上传 %d 帧，上次成功 %.1fs 前，"
                     "会话状态=%s，锚点 %s", self.frames_updated, age,
                     self._sess.state, self.cfg.anchor)

    def _reload_config_if_changed(self) -> None:
        """配置热重载：位置/尺寸/字号改完存盘即生效，无需重启。"""
        if not (self.config_path and self.config_path.exists()):
            return
        mtime = self.config_path.stat().st_mtime
        if mtime == self._last_cfg_mtime:
            return
        self._last_cfg_mtime = mtime
        try:
            import yaml
            raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            new_cfg = OverlayConfig.from_dict(raw.get("overlay") or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("[overlay:xr] 读配置失败（本次不重载）：%s", exc)
            return
        geo, render = should_rebuild(self.cfg, new_cfg)
        if not (geo or render):
            return
        cached_entries = self._last_entries_cached
        cached_render = self._last_render_cached
        self.cfg = new_cfg
        try:
            if geo:
                self._sess.ensure_anchor(new_cfg.anchor, new_cfg.tracker_index)
            if render:
                if cached_entries:
                    self.update_entries(list(cached_entries), force=True)
                elif cached_render:
                    self.update(*cached_render, force=True)
            elif geo:
                # 只改了几何：用当前内容重提一帧，让变换生效
                if cached_entries or cached_render:
                    self._last_entries = None
                    self._last_render = None
                    if cached_entries:
                        self.update_entries(list(cached_entries), force=True)
                    else:
                        self.update(*cached_render, force=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("[overlay:xr] ⚠️ 热重载应用失败：%s: %s", type(exc).__name__, exc)


# ================================================================ 冒烟自检

def _smoke(seconds: float = 15.0) -> int:
    """建会话 → 反复提交一帧示例面板 → 到点退出。不需要 API key、不连网。

    这是「真后端能不能用」的端到端验证入口（等价于 Windows 侧的 `--demo`，
    但会**真的把面板贴到你眼前**，因为要验的正是 XR 那一段）。

        python3 -m vlt.output.openxr_overlay --smoke 15
    """
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = OverlayConfig()
    ov = OpenXrOverlay(cfg)
    if not ov.start():
        print("❌ 启动失败（上面有原因）。手腕屏需要：Wayland 会话 + 已连接的 OpenXR 运行时"
              "（WiVRn/Monado 且头显已连）", flush=True)
        return 1
    img = render_panel(
        "你好，我是逆袭。这句话正在被实时翻译，看看贴在你手腕上是什么效果。",
        "Hello! I'm Nixi. This sentence is being translated in real time.")
    print(f"✅ 会话已建立，接下来 {seconds:.0f}s 内每 100ms 重提交一帧（你会看到这块面板）。"
          f"\n   想调位置/角度：改 config.yaml 的 overlay.anchor / offset，存盘即热重载。",
          flush=True)
    t0 = time.monotonic()
    seen_states: list[str] = []
    next_report = t0 + 1.0
    try:
        while time.monotonic() - t0 < seconds:
            ov._submit(img)          # 覆盖提交（面板要持续重提交才会一直显示）
            ov.tick()
            st = str(getattr(ov._sess, "state", None)).rsplit(".", 1)[-1]
            if not seen_states or seen_states[-1] != st:
                seen_states.append(st)
            if time.monotonic() >= next_report:
                next_report = time.monotonic() + 3.0
                print(f"    [{time.monotonic()-t0:4.1f}s] 会话状态={st} "
                      f"已提交={ov.frames_updated} 帧 "
                      f"锚点追踪={ov._sess.anchor_tracked(cfg.anchor)}", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    print(f"共提交 {ov.frames_updated} 帧；会话状态序列 {'→'.join(seen_states)}；"
          f"锚点 {cfg.anchor} 追踪={ov._sess.anchor_tracked(cfg.anchor) if ov._sess else None}",
          flush=True)
    if ov.frames_updated == 0:
        print("⚠️ 一帧都没提交成功。看上面的失败原因；若是会话一直没到 FOCUSED，"
              "试试**开着 VRChat（或任意 OpenXR 应用）**再跑 —— overlay 会话的 visible/focused "
              "依赖合成器上报。", flush=True)
    ov.close()
    print("已关闭。", flush=True)
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="OpenXR 手腕屏后端（冒烟自检）")
    ap.add_argument("--smoke", type=float, default=15.0, metavar="秒",
                    help="建会话并持续提交示例面板，默认 15 秒")
    args = ap.parse_args()
    raise SystemExit(_smoke(args.smoke))
