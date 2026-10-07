"""手腕屏的 **OpenXR** 后端（Linux 独占）。

这是 Windows 侧 `openvr.IVROverlay()` 的**结构对等物**：我们的进程作为
`XR_EXTX_overlay` overlay session 直接连 Monado/WiVRn 的合成器，
**不需要任何第三方 overlay 管理器、不需要注册面板、不写配置文件、不重启任何服务**。

## 下面每一条都是 spike 实测出来的，不是推断（`scripts/spike_openxr.py`）

| 结论 | 依据 |
|---|---|
| 图形绑定**按会话类型二选一**：Wayland → `XR_MNDX_egl_enable` + `GraphicsBindingEGLMNDX`；X11 → `XR_KHR_opengl_enable` + `GraphicsBindingOpenGLXlibKHR` | Monado 的 `oxr_session.c` 只分发 XLIB/WIN32/ES_ANDROID/VULKAN/**EGL_MNDX**/D3D，**没有 `OPENGL_WAYLAND_KHR`**；XLIB 与 EGL_MNDX 两条路 WiVRn/Monado 都支持 |
| 会话链 = `SessionCreateInfo → GraphicsBindingEGLMNDX → SessionCreateInfoOverlayEXTX` | `createFlags` 必须为 0（规范要求） |
| 建 session **前**必须调 `xrGetOpenGLGraphicsRequirementsKHR` | Monado 检查 `sys->gotten_requirements`，否则 `GRAPHICS_REQUIREMENTS_CALL_MISSING` |
| 建完 session **必须泵事件到 READY 再 `xrBeginSession`** | `oxr_session_begin()` 首句就要求 `XR_SESSION_STATE_READY`，否则 `SESSION_NOT_RUNNING` |
| swapchain **必须优先 sRGB 变体 `GL_SRGB8_ALPHA8 (0x8C43)`**，只有 8 位线性格式时才退而求其次并在上传前预线性化（`format_needs_linearize()`）；acquire 后必须 `wait_swapchain_image` | 合成器按**线性**合成、输出前再 `from_linear_to_srgb()`（Monado `shaders/layer.comp` 的 `main()`，注释原文「no automatic conversion in hardware available」；WiVRn 侧 `layer_squasher.cpp` 的 `k_do_color_correction=true`）。层纹理标成线性 = 我们的 sRGB 字节被当线性值再编码一次 = **双重 gamma → 面板黑不下去**。实测：底板 (12,14,20) 在 72.4% 覆盖下显示成 **(51,56,68)** —— 而双重 gamma 的理论地板正是 (52,56,67)（不管背后多黑）；漏 wait 则「上传成功但画面不动」 |
| Quad ✅ / **Cylinder ✅**（弯曲可用） | spike E/F 两项实测通过 |
| 手部 pose **可用** | spike G 实测 `position_tracked`，profile 匹配到 `oculus/touch_controller` |

## 踩过的坑（都有注释标出，别重蹈）

1. **`xrPollEvent` 的结果要从缓冲区起始强转**，不能从 `varying[]` 起 ——
   `EventDataBuffer{type(0),next(8),varying(16)}` vs `EventDataSessionStateChanged{type(0),next(8),session(16),state(24)}`，从 varying 读会偏 16 字节、读到垃圾 0。
2. **必须调 `xrSyncActions`**，否则动作状态永远不更新（pose 读不到）。
3. **niri 下 X11/GLX 走不通 —— 但真正的 Xorg 没这个问题**：niri 的 X11 是精简的
   `xwayland-satellite`，`glXChooseFBConfig`/`glXCreatePbuffer` 能过但**所有**
   context 创建方式都被拒（`GLXBadFBConfig` / `BadValue`）。所以 GL 后端按会话类型选：
   有 Wayland 优先走 libwayland-client + libEGL（niri 的常规路径），否则走 X11/GLX
   （`XlibGlxContext`，pbuffer context + XLIB 图形绑定）。
   ⚠️ **X11 路径的真机验证仍是待办**（2026-10：开发机上没有「X11 显示 + 同一环境跑
      运行时」的验证环境）。离线覆盖到：Xvfb+GLX 下真实建上下文、binding 结构体、
      按后端的扩展清单（`tests/test_overlay_glx.py` / `tests/test_openxr_overlay.py`）。
   强制指定后端可用环境变量 `VLT_OVERLAY_GL=wayland|x11`（排查用）。
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
from typing import Any

from .overlay import OverlayConfig, render_conversation, render_panel, resolve_font_path

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


# GL 内部格式（OpenXR 的 GL swapchain 格式就是 GL internal format）。
GL_RGBA8 = 0x8058               # UNORM：采样时**不做** sRGB→线性 解码
GL_SRGB8_ALPHA8 = 0x8C43        # sRGB：采样时硬件解码
GL_RGB10_A2 = 0x8059
GL_SRGB8 = 0x8C41               # sRGB（无 alpha，仅用于判定，不选它当交换链）

# 「采样时硬件会做 sRGB→线性 解码」的格式。判定按**白名单**而不是黑名单：
# 将来运行时冒出别的 sRGB 变体，宁可保守地按线性处理（多转一次是安全的，少转才发灰）。
SRGB_TEXTURE_FORMATS = (GL_SRGB8_ALPHA8, GL_SRGB8)


def format_needs_linearize(fmt: int) -> bool:
    """该交换链格式的纹素会不会被硬件自动做 sRGB→线性 解码；不会 → 我们要自己转。

    ⚠️ 这不是「可选优化」，是**颜色正确性**：`render_panel` 出来的是 sRGB 编码的
    8 位像素（PIL 语义，与 Windows 侧喂给 SteamVR 的同一份），而合成器（Monado
    `layer.comp` / WiVRn `layer_squasher.cpp`）按**线性**合成、输出前再
    `from_linear_to_srgb()` 编码一次。格式选成线性（`0x8058`）时没人解码我们的
    字节 → 等于拿 sRGB 值当线性值用 → 输出端再编码 = **双重 gamma**：
    暗部被抬到 5 倍（12 → 61、32 → 99），亮端几乎不动（255 → 255）。

    用户实测的现象就是**面板黑不下去**：底板 (12,14,20) 在 72.4% 覆盖下，
    双重 gamma 的**理论地板**是 (52,56,67)（背后不管多黑都到不了黑），
    而头显内截图实测 (51,56,68) 正好压在地板上。SteamVR 侧（`setOverlayRaw`
    的 RGBA8 被 SteamVR 当 sRGB）没有这个问题，所以「两端观感不一致」。
    """
    return int(fmt) not in SRGB_TEXTURE_FORMATS


def pick_swapchain_format(formats: list[int]) -> int:
    """挑一个 8 位 RGBA 的 GL 内部格式 —— **优先 sRGB 变体**。

    两条约束一起管：

    1. **必须 8 位**：不能直接取 `formats[0]` —— 实测列表里第一个是 0x805b（RGBA16F），
       而我们按 8 位上传 → 画面会错位。所以候选只有 8 位的三个。
    2. **优先 sRGB**：见 `format_needs_linearize()` 的说明，选线性格式会双重 gamma。
       顺序 sRGB8_ALPHA8 → RGBA8 → RGB10_A2；运行时只给线性格式时也照用，
       由上传路径（`XrOverlaySession._prepared()`）预线性化兜住正确性。
    """
    for cand in (GL_SRGB8_ALPHA8, GL_RGBA8, GL_RGB10_A2):
        if cand in formats:
            return cand
    return formats[0] if formats else GL_SRGB8_ALPHA8


def _srgb_to_linear_lut() -> list[int]:
    """sRGB 8 位 → 线性 8 位 的 256 项 LUT（`Image.point()` 用）。

    用 8 位线性装线性值是**有损**的（线性空间暗处台阶大），但误差只出现在暗端
    且 ≤2/255：`12 → 1/255`，合成器输出时编回 13（真值 12）；`32 → 4/255` → 34。
    对「面板黑不下去」这个问题来说，13 和 12 已经肉眼无差；真要更准只能换 16 位
    上传，不值得为一条兜底路径把上传路径改复杂（首选路径是 sRGB 交换链，零损失）。
    """
    out = []
    for v in range(256):
        x = v / 255.0
        lin = x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
        out.append(round(255.0 * lin))
    return out


_SRGB_TO_LINEAR = _srgb_to_linear_lut()


def linearize_rgb(img: Any) -> Any:
    """把 sRGB 编码的 **RGB** 通道转成线性；alpha 通道一动不动。

    ⚠️ 只动 RGB：预乘/未预乘的语义全在 alpha 上，动 alpha 会把
    `layer_alpha_flags()` 那套（`UNPREMULTIPLIED_ALPHA_BIT`）搞坏。
    调用方只在 `format_needs_linearize(self.format)` 为真时走这里。
    """
    from PIL import Image

    if img.mode != "RGBA":
        img = img.convert("RGBA")
    r, g, b, a = img.split()
    lut = _SRGB_TO_LINEAR
    return Image.merge("RGBA", (r.point(lut), g.point(lut), b.point(lut), a))


def layer_alpha_flags() -> Any:
    """合成层的 alpha flag —— **必须设**，否则整层被当成不透明。

    规范里图层默认按「alpha = 1.0」合成，只有设了 `BLEND_TEXTURE_SOURCE_ALPHA_BIT`
    才会去读贴图的 alpha。少了它的**实测现象**正是「面板蓝框外面多出一圈不透明黑边」：
    贴图里 alpha=0 的 12px 边条 + 圆角外的三角区（占 7.5% 像素）被当成实心黑画出来，
    底板自己的半透明（`bg_alpha`）也一起失效。

    还要带 `UNPREMULTIPLIED_ALPHA_BIT`：PIL 出来的是**未预乘**（straight）alpha，
    而运行时的默认假设是**预乘**。少了它，半透明像素会被当成预乘值 → 观感偏亮、
    白字顶到 255（Khronos 官方 `hello_xr` 就是这两条一起用；Windows 侧同理，
    我们不设 `VROverlayFlags_IsPremultiplied`，SteamVR 就按未预乘合成）。
    """
    import xr
    f = xr.CompositionLayerFlags
    return f.BLEND_TEXTURE_SOURCE_ALPHA_BIT | f.UNPREMULTIPLIED_ALPHA_BIT


def apply_overlay_alpha(img: Any, alpha: float) -> Any:
    """整层 alpha 乘子 —— Linux 侧的 `setOverlayAlpha()`（Windows 能力的对等物）。

    ⚠️ **只乘 alpha 通道，RGB 一动不动**。SteamVR 的 `SetOverlayAlpha` 就是这么做的
    （它只作用于「图层 alpha」），所以效果是「底板变淡、文字该多亮还多亮」。
    要是连 RGB 一起乘（那是**预乘空间**的算法），白字会先变灰再变暗 ——
    而用户要的恰恰是「界面半透明、文字不透明」。

    用 256 项 LUT 一趟算完（纯 C 速度）；调用方（帧循环）还会记忆化，
    正常情况下一帧渲染只过这一次。
    """
    k = max(0.0, min(1.0, float(alpha)))
    if k >= 1.0:
        return img
    lut = [round(i * k) for i in range(256)]
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lut))
    return out


def rotate_vector(rot_deg: tuple[float, float, float],
                  v: tuple[float, float, float]) -> tuple[float, float, float]:
    """把**面板局部**的向量 `v` 按面板的 `rot` 转到父空间。

    ⚠️ 柱面层的 pose 偏移必须在**面板自己的坐标系**里做：直接拿世界系的 Z 会在面板
    转过去之后指到完全不同的方向（手腕屏默认 `rot=[-47,-16,0]`，世界 Z 与面板法线
    差了几十度，面板会整块歪出去）。用的是与 `euler_to_quaternion` 同一套 Rz·Ry·Rx 约定。
    """
    x, y, z, w = euler_to_quaternion(rot_deg)
    vx, vy, vz = v
    # v' = v + 2w(q×v) + 2q×(q×v)
    cx, cy, cz = y * vz - z * vy, z * vx - x * vz, x * vy - y * vx
    c2x, c2y, c2z = y * cz - z * cy, z * cx - x * cz, x * cy - y * cx
    return (vx + 2.0 * (w * cx + c2x),
            vy + 2.0 * (w * cy + c2y),
            vz + 2.0 * (w * cz + c2z))


def layer_geometry(width_m: float, aspect: float, curvature: float) -> dict:
    """把「面板宽 + 宽高比 + 弯曲度」换算成合成层参数。

    `curvature` 沿用 Windows 侧 `SetOverlayCurvature` 的语义（openvr.h 原文：
    「curvature 是占整圆的比例，1 = 完全闭合的圆柱；给定半径时
    curvature = overlay.width / (2π·r)」）：

        central_angle = 2π · curvature        radius = width_m / central_angle

    即 **width_m 是弧长**（弯曲 0.5 = 180° 时，看到的弦长只有弧长的 64% —— 与 Windows 一致）。

    ⚠️ 半径**不能**直接当层的位置用：OpenXR 柱面层里 `pose` 是**圆柱的轴（圆心）**，
    可见弧面在 pose 局部 −Z 方向、距原点 `radius` 处（Monado `layer_cylinder.vert`：
    `x = sin(a)·r`、`z = −cos(a)·r` ⇒ 表面点满足 x²+z²=r²，轴过 pose 原点）。
    所以这里一并给出 **pose_offset**（面板局部 +Z 上挪 radius），
    由调用方用面板的 rot 转过去加到位置上 —— 见 `submit()` 与 `rotate_vector()`。
    """
    aspect = aspect if aspect > 0 else 1.0
    # 小到这个程度就按平面处理：半径已经是宽度的 1/(2πc) 倍（c=0.005 → r≈32×宽），
    # 肉眼与平面无异，再小只会让半径往几百米上飙（数值上没必要，规范里 inf 也是「无限柱」）。
    if curvature <= 0.005:
        return {"kind": "quad", "size": (width_m, width_m / aspect)}
    angle = min(2.0 * math.pi - 1e-3, 2.0 * math.pi * curvature)
    return {"kind": "cylinder", "radius": width_m / angle,
            "central_angle": angle, "aspect_ratio": aspect,
            "pose_offset": (0.0, 0.0, width_m / angle)}


# 柱面合成层是 Khronos 扩展（`XR_KHR_composition_layer_cylinder`），**并非所有运行时
# 都提供**。运行时没有它、却还构造 `CompositionLayerCylinderKHR` 的话，`xrEndFrame`
# 会整帧失败 → 手腕屏这条腿整个没了。所以按「运行时实际启用的扩展」决定用不用柱面，
# 拿不到就退回平面层，并在调用点留一行 `[overlay:xr]`（降级不许静默）。
CYLINDER_EXT = "XR_KHR_composition_layer_cylinder"

# HTC Vive Tracker 的交互 profile 是**扩展**（`XR_HTCX_vive_tracker_interaction`）：
# `/user/vive_tracker_htcx/role/...` 这族路径只有在该扩展启用时才存在，否则
# `xrStringToPath` 直接 `XR_ERROR_PATH_UNSUPPORTED`（实测：没接 tracker 的 WiVRn/Monado
# 就是这样）。所以「要不要建 tracker 锚点」按**启用的扩展**判断，而不是撞上去看报错。
TRACKER_EXT = "XR_HTCX_vive_tracker_interaction"


def effective_curvature(curvature: float, extensions: list[str]) -> float:
    """按**运行时实际启用**的扩展决定这一帧用不用柱面层。

    没有 `XR_KHR_composition_layer_cylinder` 时把 curvature 归 0 —— `layer_geometry()`
    据此返回平面 `quad`。用户看到的仍是不弯的面板，而不是整条手腕屏消失。
    """
    return curvature if CYLINDER_EXT in extensions else 0.0


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
              or new.show_source != old.show_source
              # ↓ 这些**只影响贴图像素**（底板/原文/边框/分隔线/两端色条）：
              #   不重渲的话，界面上拖「底板不透明度/原文不透明度」就只是看着生效。
              or new.bg_alpha != old.bg_alpha
              or new.source_alpha != old.source_alpha
              or new.border_alpha != old.border_alpha
              or new.separator != old.separator
              or new.color_bg != old.color_bg
              or new.color_border != old.color_border
              or new.color_source != old.color_source
              or new.color_translation != old.color_translation
              or new.color_mine != old.color_mine
              or new.color_theirs != old.color_theirs)
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


# GLX 属性（取值与 /usr/include/GL/glx.h、glxext.h 一致；不要手写魔数到调用点）
GLX_RED_SIZE, GLX_GREEN_SIZE, GLX_BLUE_SIZE, GLX_ALPHA_SIZE = 8, 9, 10, 11
GLX_DRAWABLE_TYPE, GLX_RENDER_TYPE, GLX_X_RENDERABLE = 0x8010, 0x8011, 0x8012
GLX_RGBA_BIT, GLX_PBUFFER_BIT = 0x00000001, 0x00000004
GLX_VISUAL_ID, GLX_RGBA_TYPE = 0x800B, 0x8014
GLX_PBUFFER_WIDTH, GLX_PBUFFER_HEIGHT = 0x8041, 0x8040
GLX_CONTEXT_MAJOR_VERSION_ARB, GLX_CONTEXT_MINOR_VERSION_ARB = 0x2091, 0x2092
GLX_CONTEXT_PROFILE_MASK_ARB, GLX_CONTEXT_CORE_PROFILE_BIT_ARB = 0x9126, 0x00000001


class _GlBackend:
    """GL 后端的公共部分（纯 ctypes，不引 PyOpenGL/glfw 做 GL 调用）。

    两个后端（Wayland-EGL / X11-GLX）只差两件事：**怎么建 current context**
    （子类 `__init__`）与**给 xrCreateSession 的图形绑定结构**（子类 `binding()`）；
    贴图上传、字符串查询走同一份 libGL，完全共用。

    ⚠️ 每个 extern 函数都要写全 `argtypes`/`restype` —— 不写的话 ctypes 把 64 位指针
    当 32 位 int 传，直接段错误（spike 阶段实测崩过一次）。
    """

    name = "?"
    #: 建 session 需要运行时提供的**实例扩展**。后端不同、清单不同：
    #: Wayland 要 `XR_MNDX_egl_enable`；X11 的 XLIB 绑定本身在 `XR_KHR_opengl_enable` 里。
    REQUIRED_EXTENSIONS: tuple[str, ...] = ("XR_EXTX_overlay", "XR_KHR_opengl_enable")

    def __init__(self) -> None:
        self.gl = ctypes.CDLL("libGL.so.1")
        self._bind_gl()

    def _bind_gl(self) -> None:
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

    def binding(self) -> Any:
        """xrCreateSession 链上的图形绑定结构（`create()` 返回前不得释放）。"""
        raise NotImplementedError

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
        raise NotImplementedError


class EglGlContext(_GlBackend):
    """Wayland + EGL 的 **surfaceless** context（`XR_MNDX_egl_enable` 图形绑定）。

    这是 niri / KDE / GNOME 等 Wayland 会话的常规路径（本机实测通过）。
    """

    name = "wayland-egl"
    REQUIRED_EXTENSIONS = ("XR_EXTX_overlay", "XR_KHR_opengl_enable", "XR_MNDX_egl_enable")

    def __init__(self) -> None:
        super().__init__()
        self.wl = ctypes.CDLL("libwayland-client.so.0")
        self.egl = ctypes.CDLL("libEGL.so.1")
        self._bind_egl()

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

    def _bind_egl(self) -> None:
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

    def get_proc_address_addr(self) -> int:
        """Monado 会**真的调用**我们提供的 `getProcAddress`。"""
        return ctypes.cast(self.egl.eglGetProcAddress, ctypes.c_void_p).value

    def binding(self) -> Any:
        """构造 `GraphicsBindingEGLMNDX`（三个指针字段的类型来自 PyOpenGL）。

        ⚠️ 不能给 int / c_void_p，必须 `ctypes.cast(c_void_p(addr), 该类型)`，
            否则报 "expected EGLDisplay instead of int"。
        """
        import xr
        t = {n: tp for n, tp in xr.GraphicsBindingEGLMNDX._fields_}
        return xr.GraphicsBindingEGLMNDX(
            get_proc_address=t["get_proc_address"](self.get_proc_address_addr()),
            display=ctypes.cast(ctypes.c_void_p(self.egl_display), t["display"]),
            config=ctypes.cast(ctypes.c_void_p(self.egl_config.value), t["config"]),
            context=ctypes.cast(ctypes.c_void_p(self.egl_context), t["context"]))

    def close(self) -> None:
        for fn, args in ((self.egl.eglMakeCurrent,
                          (self.egl_display, EGL_NO_SURFACE, EGL_NO_SURFACE, None)),
                         (self.wl.wl_display_disconnect, (self.wl_display,))):
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                pass


# Xlib 错误处理回调签名：int handler(Display*, XErrorEvent*)
_X_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


class XlibGlxContext(_GlBackend):
    """X11 + GLX 的 **pbuffer** context（`GraphicsBindingOpenGLXlibKHR` 图形绑定）。

    用在真正的 Xorg 会话（或带完整 XWayland/GLX 的桌面）。
    ⚠️ niri 的精简 `xwayland-satellite` 没有可用的 GLX 渲染 —— 那条路上会建不起来，
       但这没关系：niri 是 Wayland 会话，走上面的 EGL 后端（`create_gl_context()` 选择）。

    为什么用 pbuffer 而不是窗口：贴图只是上传到 swapchain 的纹理，不需要可见窗口；
    pbuffer 不需要窗口管理器配合，Xvfb/无头环境也能建起来（离线测试就靠这条）。

    ⚠️⚠️ **建上下文期间必须接管 Xlib 的错误处理器**：GLX 的失败（比如
    `glXCreateContextAttribsARB` 在不支持 GLX 渲染的 X server 上）走的是 Xlib
    **异步错误**通道，而 Xlib 默认处理器会把**整个进程**杀掉（打印一行
    `X Error of failed request: GLXBadFBConfig` 然后 exit）—— 实测踩过：
    在 niri 的 `xwayland-satellite` 上只是建 overlay 的 GL context，却把整个应用带走了。
    这里在作用域内装自己的处理器（吞掉 + 记账），`XSync` 把异步错误冲出来，
    然后还原原处理器；有错就抛 RuntimeError 交给 `create_gl_context()` 回退。
    （处理器是 Xlib 进程级全局的，窗口期只有毫秒级；期间别的线程的 X 错误会被
      一并吞掉——但那本来会导致 exit，吞掉反而是更安全的偏置。）
    """

    name = "x11-glx"
    REQUIRED_EXTENSIONS = ("XR_EXTX_overlay", "XR_KHR_opengl_enable")

    def __init__(self, width: int = 64, height: int = 64) -> None:
        super().__init__()
        self.x = ctypes.CDLL("libX11.so.6")
        self.glx = ctypes.CDLL("libGL.so.1")     # GLX 符号在 libGL 里
        self._bind_x()

        self.display = self.x.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError("XOpenDisplay 失败（DISPLAY 没设或 X server 连不上？）")
        self._fbconfigs_raw = None
        self.pbuffer = 0
        self.context = None
        self._xerror_hits: list[int] = []

        def _on_xerror(_display, _event):          # noqa: ANN001 — ctypes 回调
            self._xerror_hits.append(1)
            return 0                                # 忽略（默认处理器会 exit，绝不能走它）

        self._xerror_cb = _X_ERROR_HANDLER(_on_xerror)   # 必须留引用：GC 掉就是野指针
        old_handler = self.x.XSetErrorHandler(self._xerror_cb)
        try:
            try:
                err_base = ctypes.c_int(0)
                evt_base = ctypes.c_int(0)
                if not self.glx.glXQueryExtension(self.display, ctypes.byref(err_base),
                                                  ctypes.byref(evt_base)):
                    raise RuntimeError("X server 没有 GLX 扩展")

                screen = self.x.XDefaultScreen(self.display)
                attribs = (ctypes.c_int * 15)(
                    GLX_X_RENDERABLE, 1,
                    GLX_DRAWABLE_TYPE, GLX_PBUFFER_BIT,
                    GLX_RENDER_TYPE, GLX_RGBA_BIT,
                    GLX_RED_SIZE, 8, GLX_GREEN_SIZE, 8,
                    GLX_BLUE_SIZE, 8, GLX_ALPHA_SIZE, 8, 0)
                nelem = ctypes.c_int(0)
                raw = self.glx.glXChooseFBConfig(self.display, screen, attribs,
                                                 ctypes.byref(nelem))
                if not raw or nelem.value == 0:
                    raise RuntimeError("glXChooseFBConfig 没找到可用配置")
                self._fbconfigs_raw = raw
                fbconfig = ctypes.cast(raw, ctypes.POINTER(ctypes.c_void_p))[0]
                self.fbconfig = fbconfig
                vid = ctypes.c_int(0)
                self.glx.glXGetFBConfigAttrib(self.display, ctypes.c_void_p(fbconfig),
                                              GLX_VISUAL_ID, ctypes.byref(vid))
                self.visualid = vid.value

                pb_attribs = (ctypes.c_int * 5)(GLX_PBUFFER_WIDTH, width,
                                                GLX_PBUFFER_HEIGHT, height, 0)
                self.pbuffer = self.glx.glXCreatePbuffer(self.display,
                                                         ctypes.c_void_p(fbconfig), pb_attribs)
                if not self.pbuffer:
                    raise RuntimeError("glXCreatePbuffer 失败")
                self.context = self._create_context(fbconfig)
                if not self.context:
                    raise RuntimeError("glXCreate*Context 全部失败")
                if not self.glx.glXMakeCurrent(self.display, self.pbuffer, self.context):
                    raise RuntimeError("glXMakeCurrent 失败")

                # 把异步 X 错误冲出来：没这一步错误会「迟到」到处理器还原之后
                self.x.XSync(self.display, 0)
                if self._xerror_hits:
                    raise RuntimeError("GLX 建上下文收到 X 错误"
                                       "（这个 X server 不支持 GLX 渲染？）")
            except Exception:
                self.close()          # 建到一半失败也要把 X 连接/上下文收干净
                raise
        finally:
            self.x.XSetErrorHandler(old_handler)

    def _create_context(self, fbconfig: int) -> int | None:
        """先试 3.3 core（与 EGL 后端同口径），不行退回兼容 profile。"""
        try:
            proc = self.glx.glXGetProcAddressARB(b"glXCreateContextAttribsARB")
            if proc:
                fn = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_int,
                                      ctypes.POINTER(ctypes.c_int))(proc)
                attribs = (ctypes.c_int * 7)(
                    GLX_CONTEXT_MAJOR_VERSION_ARB, 3,
                    GLX_CONTEXT_MINOR_VERSION_ARB, 3,
                    GLX_CONTEXT_PROFILE_MASK_ARB, GLX_CONTEXT_CORE_PROFILE_BIT_ARB, 0)
                ctx = fn(self.display, ctypes.c_void_p(fbconfig), None, 1, attribs)
                if ctx:
                    return ctx
        except Exception:  # noqa: BLE001 — 拿不到 ARB 入口就退回老接口
            pass
        return self.glx.glXCreateNewContext(self.display, ctypes.c_void_p(fbconfig),
                                            GLX_RGBA_TYPE, None, 1)

    def _bind_x(self) -> None:
        self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x.XOpenDisplay.restype = ctypes.c_void_p
        self.x.XDefaultScreen.argtypes = [ctypes.c_void_p]
        self.x.XDefaultScreen.restype = ctypes.c_int
        self.x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.x.XCloseDisplay.restype = ctypes.c_int
        self.x.XFree.argtypes = [ctypes.c_void_p]
        self.x.XFree.restype = ctypes.c_int
        # ⚠️ 建上下文期间要接管错误处理器 + 主动 XSync（见 __init__ 的说明）
        self.x.XSetErrorHandler.argtypes = [ctypes.c_void_p]
        self.x.XSetErrorHandler.restype = ctypes.c_void_p
        self.x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x.XSync.restype = ctypes.c_int
        self.glx.glXQueryExtension.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                               ctypes.POINTER(ctypes.c_int)]
        self.glx.glXQueryExtension.restype = ctypes.c_int
        self.glx.glXChooseFBConfig.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                               ctypes.POINTER(ctypes.c_int),
                                               ctypes.POINTER(ctypes.c_int)]
        self.glx.glXChooseFBConfig.restype = ctypes.c_void_p
        self.glx.glXGetFBConfigAttrib.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                  ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.glx.glXGetFBConfigAttrib.restype = ctypes.c_int
        self.glx.glXCreatePbuffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_int)]
        self.glx.glXCreatePbuffer.restype = ctypes.c_ulong
        self.glx.glXCreateNewContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                 ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        self.glx.glXCreateNewContext.restype = ctypes.c_void_p
        self.glx.glXMakeCurrent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        self.glx.glXMakeCurrent.restype = ctypes.c_int
        self.glx.glXGetProcAddressARB.argtypes = [ctypes.c_char_p]
        self.glx.glXGetProcAddressARB.restype = ctypes.c_void_p
        self.glx.glXDestroyContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.glx.glXDestroyContext.restype = None
        self.glx.glXDestroyPbuffer.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.glx.glXDestroyPbuffer.restype = None

    def binding(self) -> Any:
        """构造 `GraphicsBindingOpenGLXlibKHR`（字段类型来自 PyOpenGL 的 GLX 实现）。

        ⚠️ 与 EGL 那条一样：指针字段必须 cast 成 PyOpenGL 声明的类型，不能给裸 int。
        """
        import xr
        t = {n: tp for n, tp in xr.GraphicsBindingOpenGLXlibKHR._fields_}
        return xr.GraphicsBindingOpenGLXlibKHR(
            x_display=ctypes.cast(ctypes.c_void_p(self.display), t["x_display"]),
            visualid=int(self.visualid),
            glx_fbconfig=ctypes.cast(ctypes.c_void_p(self.fbconfig), t["glx_fbconfig"]),
            glx_drawable=int(self.pbuffer),
            glx_context=ctypes.cast(ctypes.c_void_p(self.context), t["glx_context"]))

    def close(self) -> None:
        display = getattr(self, "display", None)
        if not display:
            return
        try:
            if self.context:
                self.glx.glXMakeCurrent(display, 0, None)
                self.glx.glXDestroyContext(display, self.context)
                self.context = None
            if self.pbuffer:
                self.glx.glXDestroyPbuffer(display, self.pbuffer)
                self.pbuffer = 0
            if self._fbconfigs_raw:
                self.x.XFree(self._fbconfigs_raw)
                self._fbconfigs_raw = None
        finally:
            self.display = None
            try:
                self.x.XCloseDisplay(display)
            except Exception:  # noqa: BLE001
                pass


_GL_BACKENDS: dict[str, type[_GlBackend]] = {"wayland": EglGlContext, "x11": XlibGlxContext}


def create_gl_context() -> _GlBackend:
    """挑一个能建起来的 GL 后端并返回（Wayland 优先，失败回退 X11）。

    选择顺序：
      * 环境里**两个**都设了（Wayland 会话带 XWayland）→ 先试 Wayland（niri 的
        xwayland-satellite 没有 GLX，Wayland 才是主路）；
      * 只有 DISPLAY（Xorg 会话）→ 走 X11/GLX；
      * 两个都没有 → 两条都试一遍，把两边的错误都报出来（便于排查）。
    可用 `VLT_OVERLAY_GL=wayland|x11` 强制指定（只试那一条，失败直接抛）。
    """
    import os
    forced = (os.environ.get("VLT_OVERLAY_GL") or "").strip().lower()
    if forced and forced not in _GL_BACKENDS:
        log.warning("[overlay:xr] VLT_OVERLAY_GL=%r 不认识（只认 wayland/x11）→ 忽略", forced)
        forced = ""
    if forced:
        order = [forced]
    else:
        order = []
        if os.environ.get("WAYLAND_DISPLAY"):
            order.append("wayland")
        if os.environ.get("DISPLAY"):
            order.append("x11")
        if not order:
            order = ["wayland", "x11"]

    errors: list[str] = []
    for kind in order:
        try:
            backend = _GL_BACKENDS[kind]()
        except Exception as exc:  # noqa: BLE001 — 换下一个后端；全失败时统一报
            errors.append(f"{kind}: {type(exc).__name__}: {exc}")
            log.warning("[overlay:xr] GL 后端 %s 建不起来（%s）→ 试下一个", kind, exc)
            continue
        log.info("[overlay:xr] GL 后端：%s", backend.name)
        return backend
    raise RuntimeError("GL 后端都建不起来：" + "；".join(errors))


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

    def __init__(self, gl: "_GlBackend", size_px: tuple[int, int]) -> None:
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
        # 锚点 → 动作/空间。**一次建全**：动作集一旦 attach 就变成 immutable，
        # 之后 `xrCreateAction` / `xrSuggestInteractionProfileBindings` /
        # `xrAttachSessionActionSets` 一律返回 XR_ERROR_ACTIONSETS_ALREADY_ATTACHED
        # （规范原文见 `_ensure_actions`）。所以切锚点**不能**再建动作，只能换用哪个 space。
        self._actions: dict[tuple[str, int], Any] = {}
        self._spaces: dict[tuple[str, int], Any] = {}
        self._actions_ready = False
        self._anchor_key: tuple[str, int] | None = None
        self._frame_state: Any = None
        # 「整层 alpha + 转字节」的记忆化缓存：(帧, alpha, bytes)。
        # 帧循环每帧重提同一张图，没有它就要每帧白跑一次 LUT。
        self._prep: tuple[Any, float, int, bytes] | None = None
        # 本会话**实际启用**的扩展名（`create()` 传进来的那批）。`submit()` 靠它判断
        # 运行时到底支不支持柱面层，不支持就退回平面（见 `effective_curvature()`）。
        self.extensions: list[str] = []
        self._cylinder_warned = False        # 柱面降级只在第一次留痕，不每帧刷日志
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
        self.extensions = list(extensions)   # 记下真正启用的那批（submit 判断柱面能力用）
        self.instance = xr.create_instance(xr.InstanceCreateInfo(
            application_info=xr.ApplicationInfo(application_name="VRChat LiveTranslate",
                                                application_version=1),
            enabled_extension_names=extensions))
        self.system_id = xr.get_system(self.instance)
        self._begin_session()
        self._create_ref_spaces()
        self._create_swapchain()
        log.info("[overlay:xr] 会话就绪：%sx%s format=0x%x（%s），扩展 %d 个",
                 self.size_px[0], self.size_px[1], self.format,
                 "线性纹理 → 像素已预线性化"
                 if format_needs_linearize(self.format)
                 else "sRGB 纹理 → 采样时硬件解码",
                 len(extensions))

    def _begin_session(self) -> None:
        """建 overlay session 并推进到 running。"""
        import xr
        overlay_info = xr.SessionCreateInfoOverlayEXTX(
            create_flags=xr.OverlaySessionCreateFlagsEXTX(0),   # 规范要求必须 0
            session_layers_placement=100)
        binding = self._gl.binding()
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
        if format_needs_linearize(self.format):
            # 降级**不许静默**：这条说明运行时没给 sRGB 的 8 位格式，我们靠预线性化
            # 兜的正确性（暗端会有 ±2/255 的量化误差，不是错色）。
            log.warning("[overlay:xr] ⚠️ 运行时没提供 sRGB 的 8 位 RGBA 交换链格式"
                        "（可用：%s）→ 用 format=0x%x（线性）并在上传前预线性化像素",
                        [hex(int(f)) for f in formats[:12]], self.format)
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

    def set_size(self, size_px: tuple[int, int]) -> None:
        """改面板像素尺寸：更新尺寸并**重建交换链**（「面板高」滑块热重载用）。

        ⚠️ 只改配置、不动交换链的话：新面板图比交换链大 → `glTexSubImage2D` 越界
        （GL_INVALID_VALUE，整张不写）+ `SwapchainSubImage.image_rect` 越界
        （`XR_ERROR_SWAPCHAIN_RECT_INVALID`）→ 每帧 `xrEndFrame` 都失败。
        而自愈重建用的还是**本对象的旧尺寸** → 面板永久卡死（用户实测：
        430 → 440 一步就触发，日志连刷 232 次「已重建 swapchain」也不恢复，
        只能重启应用）。同尺寸调用幂等（不白重建）。
        """
        size_px = (max(1, int(size_px[0])), max(1, int(size_px[1])))
        if size_px == tuple(self.size_px):
            return
        self.size_px = size_px
        if self.swapchain is not None:
            self.rebuild_swapchain()

    # ---------- 锚点 ----------

    # 手部锚点建议绑定的交互 profile。只给一个的话，控制器型号对不上就完全没有 pose。
    HAND_PROFILES = (
        "/interaction_profiles/khr/simple_controller",
        "/interaction_profiles/oculus/touch_controller",
        "/interaction_profiles/valve/index_controller",
        "/interaction_profiles/microsoft/motion_controller",
        "/interaction_profiles/htc/vive_controller",
    )
    # tracker 走 **HTC Vive Tracker 专用 profile**（`/user/vive_tracker_htcx/role/...`
    # 是它的子动作路径）。拿控制器 profile 去绑 tracker 路径会被运行时判成无效绑定。
    TRACKER_PROFILE = "/interaction_profiles/htc/vive_tracker_htcx"

    @staticmethod
    def _anchor_keys() -> list[tuple[str, int]]:
        """开局就把这些锚点的动作全建出来（attach 之后就再也不能建了）。"""
        return ([("left_hand", 0), ("right_hand", 0)]
                + [("tracker", i) for i in range(len(TRACKER_ROLES))])

    def _ensure_actions(self) -> None:
        """建好全部锚点动作 + 建议绑定 + attach —— **整个会话只做一次**。

        规范（OpenXR `input.adoc`）写得没有余地：
          * `xrAttachSessionActionSets`「**must** return XR_ERROR_ACTIONSETS_ALREADY_ATTACHED
            if called more than once for a given session」；
          * `xrCreateAction` / `xrSuggestInteractionProfileBindings`：「If `actionSet` has been
            included in a call to `xrAttachSessionActionSets`, the implementation **must**
            return XR_ERROR_ACTIONSETS_ALREADY_ATTACHED」——「When an action set is attached
            to a session, that action set becomes **immutable**」。

        老代码是「按需建动作」：每换一次锚点就 `create_action` + `attach` 一次，于是从
        第二次起必然抛 `ActionsetsAlreadyAttachedError`，被热重载的 `except` 吃掉 ——
        **面板压根没换过挂点**（用户实测日志：切左手/切 tracker 各报一次这条错）。
        所以这里改成「一次建全、之后只换 space」。

        ⚠️ 建议绑定**必须按 interaction profile 聚合，每个 profile 只调一次**。规范同一节：
        「If the application successfully calls xrSuggestInteractionProfileBindings more than
        once for an interaction profile, the runtime **must discard the previous suggested
        bindings and replace them** with the new suggested bindings」。逐个动作各调一次的话，
        每个 profile 上只剩**最后一个动作**的绑定 —— 实测踩过：修好右手之后，右手那条把
        左手那条覆盖掉，左手锚点就永远 untracked、静默退回 VIEW（面板跟着头）。
        这个 API 收的就是一张绑定**表**，一次给全才是它的用法。
        """
        if self._actions_ready:
            return
        import xr
        if self.action_set is None:
            self.action_set = xr.create_action_set(self.instance, xr.ActionSetCreateInfo(
                action_set_name="vlt_wrist", localized_action_set_name="VLT Wrist Panel",
                priority=0))
        # tracker 整族的可用性：HTC tracker 的 role 路径属于扩展
        # `XR_HTCX_vive_tracker_interaction`，运行时不支持（没接 tracker 的 WiVRn/Monado 就是）
        # 就**整族跳过** —— 否则每开一次程序对着 8 个 role 各报一次错，真正的故障反而被淹。
        tracker_ok = TRACKER_EXT in self.extensions
        if not tracker_ok:
            log.info("[overlay:xr] 本运行时没启用 %s → 跳过 tracker 动作（左右手锚点不受影响；"
                     "要用 tracker 锚点得先在运行时里接上 tracker）", TRACKER_EXT)
        skip_tracker = not tracker_ok
        for anchor, idx in self._anchor_keys():
            # ★ 逐个锚点降级：某个运行时（或某版 Monado）不认 tracker 的 role 路径时，
            #   只该让**那一个锚点**不可用，绝不能把整条手腕屏拖死 —— 现在开局就会建
            #   全部 8 个 tracker role 的动作，一个不认就整条腿没了（老实现只建当前
            #   锚点，所以踩不到，改成「一次建全」之后这层兜底是必须的）。
            if anchor == "tracker" and skip_tracker:
                continue
            try:
                full, top = anchor_paths(anchor, idx)
                if full is None:                 # hmd → 用 VIEW 参考空间，不需要动作
                    continue
                act = xr.create_action(self.action_set, xr.ActionCreateInfo(
                    action_name=f"pose_{anchor}_{idx}", action_type=xr.ActionType.POSE_INPUT,
                    # ⚠️ `localizedActionName` 在**同一个动作集内必须唯一**（规范：
                    #    duplicates of the corresponding field for any existing action in
                    #    the specified action set → **must** XR_ERROR_LOCALIZED_NAME_DUPLICATED）。
                    #    实测踩过：10 个动作共用 "Wrist Anchor" → 只有第一个建得出来，
                    #    右边/左手那个建不出来 → 查不到 space → 面板静默回退到 VIEW（跟着头）。
                    localized_action_name=f"Wrist Anchor {anchor} {idx}",
                    subaction_paths=[xr.string_to_path(self.instance, top)]))
                self._actions[(anchor, idx)] = act
            except Exception as exc:  # noqa: BLE001
                if anchor == "tracker":
                    skip_tracker = True          # 一个 role 建不出来 → 整族跳过（同一扩展管）
                    log.warning("[overlay:xr] ⚠️ tracker 的 role 路径用不了 → tracker 锚点整族跳过"
                                "（左右手不受影响）：%s: %s", type(exc).__name__, exc)
                else:
                    log.warning("[overlay:xr] ⚠️ 锚点 %s#%d 的动作建不出来，该锚点退回 VIEW 空间"
                                "（其它锚点不受影响）：%s: %s", anchor, idx, type(exc).__name__, exc)

        # 按 interaction profile 聚合绑定：**每个 profile 一次性给全**（见上面的规范引用）。
        # 左右手可以放同一次调用：控制器 profile 的 allowlist 对 /user/hand/left 与
        # /user/hand/right 都有效且都定义了 /input/grip/pose。
        by_profile: dict[str, list[tuple[Any, str]]] = {}
        for (anchor, idx), act in self._actions.items():
            full, _ = anchor_paths(anchor, idx)
            profiles = ((self.TRACKER_PROFILE,) if anchor == "tracker" else self.HAND_PROFILES)
            for profile in profiles:
                by_profile.setdefault(profile, []).append((act, full))
        for profile, pairs in by_profile.items():
            try:
                xr.suggest_interaction_profile_bindings(
                    self.instance, xr.InteractionProfileSuggestedBinding(
                        interaction_profile=xr.string_to_path(self.instance, profile),
                        suggested_bindings=[xr.ActionSuggestedBinding(
                            action=act, binding=xr.string_to_path(self.instance, full))
                            for act, full in pairs]))
            except Exception as exc:  # noqa: BLE001 — 运行时不认的 profile 整组跳过
                log.info("[overlay:xr] 交互 profile %s 的绑定建议被运行时拒绝（该 profile 上的"
                         "锚点会退回 VIEW 空间）：%s", profile, exc)

        # ★ 唯一的 attach 机会
        xr.attach_session_action_sets(self.session, xr.SessionActionSetsAttachInfo(
            action_sets=[self.action_set]))
        # 空间可以在 attach 之后再建（它不改动作集）。全部建好，切锚点就只是查表。
        for (anchor, idx), act in self._actions.items():
            try:
                _, top = anchor_paths(anchor, idx)
                self._spaces[(anchor, idx)] = xr.create_action_space(
                    self.session, xr.ActionSpaceCreateInfo(
                        action=act, subaction_path=xr.string_to_path(self.instance, top),
                        pose_in_action_space=xr.Posef(
                            orientation=xr.Quaternionf(0.0, 0.0, 0.0, 1.0),
                            position=xr.Vector3f(0.0, 0.0, 0.0))))
            except Exception as exc:  # noqa: BLE001
                log.warning("[overlay:xr] ⚠️ 锚点 %s#%d 的空间建不出来，退回 VIEW 空间：%s: %s",
                            anchor, idx, type(exc).__name__, exc)
        self._actions_ready = True

    def ensure_anchor(self, anchor: str, tracker_index: int) -> None:
        """切到某个锚点：确保动作都已建好（只建一次），然后记下当前用哪一个 space。

        ⚠️ 这里**不做任何会改动动作集的 XR 调用** —— 换了锚点只是换 `anchor_space()`
        返回哪个 space（以及 `sync_actions` 照旧每帧跑）。重建动作集/重新 attach 在
        OpenXR 里是被规范明确禁止的（见 `_ensure_actions`）。
        """
        self._ensure_actions()
        self._anchor_key = (anchor, int(tracker_index))

    def anchor_space(self, anchor: str, tracker_index: int = 0) -> Any:
        """层要挂到哪个 space。

        ⚠️ action space 在动作同步生效前是**未追踪**的，层会落到 LOCAL 原点 ——
        实测表现就是「面板固定不动、不跟手也不跟头」。所以这里做一次回退：
        锚点没被追踪时改用 `view_space`（至少跟着头），sync 生效后下一帧自然切回。
        """
        if anchor == "hmd":
            return self.view_space
        space = self._spaces.get((anchor, int(tracker_index)))
        if space is not None and self._space_tracked(space):
            return space
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

    def anchor_tracked(self, anchor: str, tracker_index: int = 0) -> bool | None:
        """锚点是否被追踪（诊断用；拿不到返回 None）。"""
        import xr
        if anchor == "hmd":
            return None
        space = self._spaces.get((anchor, int(tracker_index)))
        if space is None:
            return None
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

    def submit(self, image: Any, cfg: OverlayConfig,
               alpha: float | None = None) -> None:
        """把一张 PIL 图作为一层提交上去。失败抛异常（由上层决定自愈）。

        `alpha` 是整层 alpha 乘子（缺省取 `cfg.alpha`）。OpenXR 没有
        `setOverlayAlpha` 那种 API，只能自己乘进贴图的 alpha 通道；而且淡出要
        **逐帧**变（静默超时就归零），所以由调用方每帧算、这里只负责记忆化。
        """
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
        self._gl.upload(self.textures[idx], w, h,
                        self._prepared(image, cfg.alpha if alpha is None else alpha))
        xr.release_swapchain_image(self.swapchain)

        sub = xr.SwapchainSubImage(
            swapchain=self.swapchain,
            image_rect=xr.Rect2Di(offset=xr.Offset2Di(0, 0),
                                  extent=xr.Extent2Di(w, h)))
        curv = effective_curvature(cfg.curvature, self.extensions)
        if cfg.curvature > 0.0 and curv == 0.0 and not self._cylinder_warned:
            self._cylinder_warned = True        # 只留一次痕，别每帧刷
            log.warning("[overlay:xr] ⚠️ 运行时没有 %s 扩展 → 弯曲度 %.2f 退回平面层"
                        "（面板仍在，只是不弯；要弯曲请改用支持该扩展的运行时）",
                        CYLINDER_EXT, cfg.curvature)
        geo = layer_geometry(cfg.width_m, w / h if h else 1.0, curv)
        # ★ 柱面层的 pose 是**圆柱的轴（圆心）**，不是面板位置：不补偿的话圆心会落在
        #   用户设的位置、而可见弧面整体沿局部 −Z 漂出 radius 远（实测现象就是
        #   「圆点跑到我设的位置、面板飘走了」）。把轴沿面板局部 +Z 挪 radius，
        #   弧面中点就回到与平面层相同的位置 —— 也就是 Windows/SteamVR 的观感
        #   （中心不动，两侧朝你卷）。
        pos = cfg.pos
        if geo["kind"] == "cylinder":
            ox, oy, oz = rotate_vector(cfg.rot, geo["pose_offset"])
            pos = (pos[0] + ox, pos[1] + oy, pos[2] + oz)
        pose = xr.Posef(
            orientation=xr.Quaternionf(*euler_to_quaternion(cfg.rot)),
            position=xr.Vector3f(*pos))
        space = self.anchor_space(cfg.anchor, cfg.tracker_index)
        # ★ 两个 flag 缺一不可：BLEND 让贴图的 alpha 真的生效（否则整层不透明 →
        #   蓝框外一圈黑边），UNPREMULTIPLIED 声明我们给的是未预乘 alpha
        #   （PIL 的语义，与 Windows 侧给 SteamVR 的一致）。见 layer_alpha_flags()。
        if geo["kind"] == "cylinder":
            layer = xr.CompositionLayerCylinderKHR(
                layer_flags=layer_alpha_flags(), sub_image=sub, pose=pose, space=space,
                radius=geo["radius"], central_angle=geo["central_angle"],
                aspect_ratio=geo["aspect_ratio"])
        else:
            layer = xr.CompositionLayerQuad(
                layer_flags=layer_alpha_flags(), sub_image=sub, pose=pose, space=space,
                size=xr.Extent2Df(*geo["size"]),
                eye_visibility=xr.EyeVisibility.BOTH)
        base_t = ctypes.POINTER(xr.CompositionLayerBaseHeader)
        arr = (base_t * 1)(ctypes.cast(ctypes.pointer(layer), base_t))
        xr.end_frame(self.session, xr.FrameEndInfo(
            display_time=frame.predicted_display_time,
            environment_blend_mode=xr.EnvironmentBlendMode.OPAQUE,
            layer_count=1, layers=arr))

    def _prepared(self, image: Any, alpha: float) -> bytes:
        """按 `(帧, 整层 alpha, 交换链格式)` 记忆化「乘 alpha → 可选线性化 → 转字节」。

        帧循环**每帧都要重提同一张图**（OpenXR 的 composition layer 不是持久对象），
        所以这里必须缓存，否则每帧白过一次 1024x440 的 LUT。缓存里**持有这帧的
        引用**，`id()` 就不可能被回收复用，键也就不会撞车。

        格式也进键：`rebuild_swapchain()` 若协商到不同格式，线性化与否会变
        （见 `format_needs_linearize()`），拿旧字节贴上去颜色就错了。
        """
        k = max(0.0, min(1.0, float(alpha)))
        cached = self._prep
        if cached is not None and cached[0] is image and cached[1] == k \
                and cached[2] == self.format:
            return cached[3]
        img = apply_overlay_alpha(image, k)
        if format_needs_linearize(self.format):
            img = linearize_rgb(img)     # 只动 RGB；alpha 由上面的乘子决定
        data = img.tobytes()
        self._prep = (image, k, self.format, data)
        return data

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
        self._gl: _GlBackend | None = None
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
        # 「最后一次**有新内容**的时刻」——`fade_after_s` 超时后整层 alpha 归零
        # （Windows 侧是 `setOverlayAlpha(0)`，语义刻意保持一致）
        self._last_content_at = 0.0

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
            self._gl = create_gl_context()
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

    def _layer_alpha(self) -> float:
        """当前该用的**整层** alpha（Linux 侧的 `setOverlayAlpha()`）。

        `fade_after_s > 0` 且静默超过它 → 直接归零（与 Windows 侧一致：是**消失**，
        不是渐变 —— 那边就是 `setOverlayAlpha(0.0)`）。
        """
        cfg = self.cfg
        if cfg.fade_after_s > 0 and self._last_content_at:
            if time.monotonic() - self._last_content_at > cfg.fade_after_s:
                return 0.0
        return max(0.0, min(1.0, float(cfg.alpha)))

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
                sess.submit(img, self.cfg, self._layer_alpha())
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
        if rebuild_gl or self._gl is None:
            if self._gl is not None:
                self._gl.close()
            self._gl = create_gl_context()
        self._sess = XrOverlaySession(self._gl, self.cfg.size_px)
        self._sess.create(self._extensions(self._gl.REQUIRED_EXTENSIONS))
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
    def _extensions(required: tuple[str, ...] = (
            "XR_EXTX_overlay", "XR_KHR_opengl_enable", "XR_MNDX_egl_enable")) -> list[str]:
        """挑出可用的扩展（运行时不支持的就不启用，免得 create_instance 直接失败）。

        `required` 由 GL 后端声明（`_GlBackend.REQUIRED_EXTENSIONS`）：Wayland 的
        EGL_MNDX 要 `XR_MNDX_egl_enable`，X11 的 XLIB 绑定只要 `XR_KHR_opengl_enable`。
        默认值保持 Wayland 那套（旧调用口径不变）。

        ⚠️ 枚举失败时**不猜**：只请求「没有它这条腿根本起不来」的必需项，不把可选的
        柱面扩展（`CYLINDER_EXT`）算进去。请求一个运行时没有的扩展会让 `create_instance`
        直接失败 —— 那是**整条手腕屏消失**，比「不弯」严重得多。柱面的能力判定改由
        `submit()` 按实际启用的扩展做，拿不到就退回平面（`effective_curvature()`）。

        `TRACKER_EXT` 同理：有就启用（tracker 锚点才有可能可用），没有就不请求 ——
        反正那族路径不存在，`_ensure_actions()` 会按启用列表把 tracker 整族跳过。
        """
        import xr
        required = list(required)
        try:
            have = set()
            for e in xr.enumerate_instance_extension_properties():
                n = e.extension_name
                have.add(n.decode() if isinstance(n, bytes) else str(n))
        except Exception:  # noqa: BLE001
            return list(required)            # 枚举不出来就只赌必需项，不赌柱面/tracker
        want = required + [CYLINDER_EXT, TRACKER_EXT]
        return [e for e in want if e in have]

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
        self._last_content_at = time.monotonic()      # 有新内容 → 重置淡出计时
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
        self._last_content_at = time.monotonic()      # 有新内容 → 重置淡出计时
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
        ⚠️ 本方法**不许**在动作后把 `_stage` 放回 "none"：那等于每轮都从第一级重来、
        永远升不上第二级。用户实测日志：面板尺寸拖大后连刷 232 次「已重建 swapchain」、
        0 次重建会话 —— 面板永久卡死，只能重启应用。梯子的回落只在帧循环的
        「真的恢复上传」分支里做（`_fails = 0` 那处）。
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
        # 淡出阈值既不改几何也不改贴图 —— 它只影响帧循环里逐帧算的整层 alpha。
        # 但配置对象**必须**换掉，否则界面上改了 fade_after_s 存盘后不生效。
        fade_only = new_cfg.fade_after_s != self.cfg.fade_after_s
        if not (geo or render or fade_only):
            return
        cached_entries = self._last_entries_cached
        cached_render = self._last_render_cached
        self.cfg = new_cfg
        try:
            # ★ 尺寸变化必须让**交换链**跟着换（见 `XrOverlaySession.set_size` 的实测记录）：
            #   只重渲染不换交换链的话，新图比交换链大 → 上传越界 + image_rect 越界 →
            #   每帧提交失败，而自愈重建用的还是旧尺寸 → 面板永久卡死。
            if self._sess is not None and tuple(new_cfg.size_px) != tuple(self._sess.size_px):
                old = self._sess.size_px
                self._sess.set_size(tuple(new_cfg.size_px))
                log.info("[overlay:xr] 面板尺寸 %sx%s → %sx%s（交换链已跟随重建）",
                         old[0], old[1], new_cfg.size_px[0], new_cfg.size_px[1])
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

def render_alpha_test(cfg: OverlayConfig | None = None) -> Any:
    """画一张「一眼就能看出 alpha / 颜色空间对不对」的判定图（`--smoke --alpha-test` 用）。

    底板/边框沿用真实面板的配色与留白，所以看它 ≈ 看真面板：

      * **12px 透明边距 + 圆角**：外面一旦出现黑边，就是图层的 alpha 没生效
        （缺 `BLEND_TEXTURE_SOURCE_ALPHA_BIT`）—— 正是「蓝框外一圈黑」的病根；
      * **四块 25/50/75/100% 不透明度的灰块**：应当由淡到实。整体偏亮、半透明的块
        发白，就说明未预乘 alpha 被当成预乘了（缺 `UNPREMULTIPLIED_ALPHA_BIT`）；
      * **一行 100% 不透明的白字**：底板半透明不该把文字一起变淡
        （整层乘子只乘 alpha 通道，RGB 不动）；
      * **一排纯灰阶（0 / 12 / 32 / 64 / 128 / 192 / 255，全部 100% 不透明）**：
        盯**颜色空间**（transfer function）。数字应逐级变亮且与标注值对得上；
        若 12 看着像 60、32 像 99（暗端整体被抬起、255 却仍是白），就是层纹理被
        当成线性值再编码一次 = **双重 gamma** —— 用户实测的「面板黑不下去」，
        病根见 `format_needs_linearize()`。这一排是纯色不透明块，没有 alpha 参与，
        所以它**只**反映颜色空间，不会和上面那排 alpha 判定混在一起。
    """
    from PIL import Image, ImageDraw, ImageFont

    cfg = cfg or OverlayConfig()
    w, h = cfg.size_px
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 12                              # 与 render_panel 一致的留白：这一圈必须全透明
    d.rounded_rectangle([pad, pad, w - pad, h - pad], radius=28,
                        fill=(*cfg.color_bg, cfg.bg_alpha),
                        outline=(*cfg.color_border, cfg.border_alpha), width=3)

    def _font(size: int) -> ImageFont.FreeTypeFont:
        path = resolve_font_path(cfg.font)
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001 — 字体坏了也别让判定图画不出来
                pass
        return ImageFont.load_default()

    f_title = _font(max(18, cfg.font_size - 4))
    f_label = _font(max(14, cfg.source_font_size))
    x0 = pad * 2 + 6
    d.text((x0, pad * 2), "ALPHA TEST", font=f_title, fill=(*cfg.color_translation, 255))

    levels = (64, 128, 191, 255)          # 25% / 50% / 75% / 100%
    gap = 20
    bar = max(24, (w - 2 * x0 - gap * (len(levels) - 1)) // len(levels))
    top = pad * 3 + cfg.font_size
    bottom = top + max(60, h // 4)
    for i, a in enumerate(levels):
        x = x0 + i * (bar + gap)
        d.rectangle([x, top, x + bar, bottom], fill=(235, 235, 235, a))
        d.text((x, bottom + 10), f"{round(a / 255 * 100)}%",
               font=f_label, fill=(*cfg.color_translation, 255))
    d.text((x0, bottom + 22 + cfg.source_font_size), "文字必须实心 / text stays solid",
           font=f_label, fill=(*cfg.color_translation, 255))

    # ★ 灰阶带（颜色空间判定）：值写在标签上，观感应与标签一致。
    ramp_top = bottom + 22 + cfg.source_font_size + 18
    ramp_h = min(70, (h - pad) - ramp_top - (cfg.source_font_size + 12))
    if ramp_h >= 28:                     # 面板太矮就不画（判定图优先保持不越界/不遮底板）
        grays = (0, 12, 32, 64, 128, 192, 255)
        gw = max(18, (w - 2 * x0 - gap * (len(grays) - 1)) // len(grays))
        for i, v in enumerate(grays):
            x = x0 + i * (gw + gap)
            d.rectangle([x, ramp_top, x + gw, ramp_top + ramp_h], fill=(v, v, v, 255))
            d.text((x, ramp_top + ramp_h + 6), str(v), font=f_label,
                   fill=(*cfg.color_translation, 255))
    return img


def _smoke(seconds: float = 15.0, alpha_test: bool = False) -> int:
    """建会话 → 持续提交一帧面板 → 到点退出。不需要 API key、不连网。

    这是「真后端能不能用」的端到端验证入口（等价于 Windows 侧的 `--demo`，
    但会**真的把面板贴到你眼前**，因为要验的正是 XR 那一段）：

        python3 -m vlt.output.openxr_overlay --smoke 15
        python3 -m vlt.output.openxr_overlay --smoke 20 --alpha-test

    ⚠️ 会读**你自己的 config.yaml**（存在的话）：透明度和贴手腕的位置/角度都是
    真机要看的参数，用默认值测等于没测。想边看边调：改 config.yaml 存盘即热重载。
    """
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from ..paths import APP_DIR
    cfg_path = Path(APP_DIR) / "config.yaml"
    cfg = OverlayConfig()
    if cfg_path.exists():
        try:
            import yaml
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            cfg = OverlayConfig.from_dict(raw.get("overlay") or {})
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ 读 {cfg_path} 失败，这次用默认参数：{exc}", flush=True)
    ov = OpenXrOverlay(cfg, config_path=cfg_path if cfg_path.exists() else None)
    if not ov.start():
        print("❌ 启动失败（上面有原因）。手腕屏需要：Wayland 会话 + 已连接的 OpenXR 运行时"
              "（WiVRn/Monado 且头显已连）", flush=True)
        return 1
    if alpha_test:
        ov._queue_frame(render_alpha_test(cfg))
        print("✅ 会话已建立，正在提交 **alpha + 颜色空间判定图**。要看四点：\n"
              "   ① 蓝色边框外面是否**全透明**（有黑边 = 图层 alpha 没生效）\n"
              "   ② 四块灰是否由淡到实（发白/发光 = 未预乘 alpha 被当成预乘）\n"
              "   ③ 白字是否实心（跟着底板一起变淡 = 乘子乘到了 RGB）\n"
              "   ④ 最下面一排灰阶（标着 0/12/32/64/128/192/255）是否**黑得下去**：\n"
              "      若 12 看着像 60、32 像 99，而 255 仍是白 → 双重 gamma（层纹理被当线性值），\n"
              "      见 format_needs_linearize()；两侧观感不一致时先看这一排", flush=True)
    else:
        ov.update("你好，我是逆袭。这句话正在被实时翻译，看看贴在你手腕上是什么效果。",
                  "Hello! I'm Nixi. This sentence is being translated in real time.")
        print(f"✅ 会话已建立，接下来 {seconds:.0f}s 内会持续重提交这一帧（你会看到这块面板）。"
              f"\n   想调位置/角度：改 config.yaml 的 overlay.anchor / offsets（每个锚点各一份），"
              f"存盘即热重载。",
              flush=True)
    t0 = time.monotonic()
    seen_states: list[str] = []
    next_report = t0 + 1.0
    try:
        while time.monotonic() - t0 < seconds:
            # 面板由后台帧循环每帧重提 —— 这里只报状态（原来那句 ov._submit()
            # 早就不存在了：提交已搬进帧循环线程，见 _frame_loop）
            ov.tick()
            st = str(getattr(ov._sess, "state", None)).rsplit(".", 1)[-1]
            if not seen_states or seen_states[-1] != st:
                seen_states.append(st)
            if time.monotonic() >= next_report:
                next_report = time.monotonic() + 3.0
                print(f"    [{time.monotonic()-t0:4.1f}s] 会话状态={st} "
                      f"已提交={ov.frames_updated} 帧 "
                      f"整层 alpha={ov._layer_alpha():.2f} "
                      f"锚点追踪={ov._sess.anchor_tracked(cfg.anchor, cfg.tracker_index)}", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    print(f"共提交 {ov.frames_updated} 帧；会话状态序列 {'→'.join(seen_states)}；"
          f"锚点 {cfg.anchor} 追踪="
          f"{ov._sess.anchor_tracked(cfg.anchor, cfg.tracker_index) if ov._sess else None}",
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
    ap.add_argument("--alpha-test", action="store_true",
                    help="改提交「透明边距 + 25/50/75/100%% 半透明块 + 实心白字 + "
                         "0/12/32/64/128/192/255 灰阶带」判定图，"
                         "用来肉眼验收通透性与颜色空间（黑不下去 = 双重 gamma）")
    args = ap.parse_args()
    raise SystemExit(_smoke(args.smoke, args.alpha_test))
