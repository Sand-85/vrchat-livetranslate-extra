#!/usr/bin/env python3
"""B0 spike —— 自建 OpenXR overlay 后端（对应 Windows 侧的 IVROverlay）可行性验证。

## 为什么要先跑这个

Windows 侧的手腕屏是 `openvr.IVROverlay().createOverlay()` 一把梭；Linux 要照做就得走
`XR_EXTX_overlay`（我们的进程作为 overlay session 直接连 Monado/WiVRn 的合成器）。
但有两处是**纸面上推不出来**的：

  1. 图形绑定选哪个（OpenGL 还是 Vulkan）—— 决定实现形态与依赖
  2. overlay session 里**能不能读到手部 pose** —— 决定 `anchor: right_hand/tracker`
     这些锚点是否可用；读不到就只剩 `anchor: hmd`（位置仍由 pos/rot 决定，但不跟手）

这两条不先验，实现可能整块返工。所以这个脚本只做验证，不产出任何产品代码。

## 七项检查（逐项独立报告，部分成功也有信息量）

  A 运行时与扩展      EXTX_overlay / opengl / cylinder / hand_tracking 是否都在
  B instance + system xrCreateInstance → xrGetSystem（需要 HMD，真机或 XRT_DEVICE_GENERIC_HMD）
  C GLX 离屏 context  纯 ctypes 调 libX11/libGL 建一个 pbuffer context（不引入新依赖）
  D overlay session   把 SessionCreateInfoOverlayEXTX 链进图形绑定，建 overlay session
  E swapchain + Quad  提交一帧 CompositionLayerQuad（= Windows 侧 setOverlayRaw）
  F Cylinder 层       = Windows 侧 setOverlayCurvature
  G 手部 pose         xrLocateSpace 读 /user/hand/right/input/grip/pose

用法：
    python3 scripts/spike_openxr.py
环境变量：
    XR_RUNTIME_JSON  指定运行时 manifest（没设 active_runtime.json 时用），例如
                     XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS: list[tuple[str, str, str]] = []      # (检查项, "ok"/"fail"/"skip", 说明)


def record(name: str, status: str, detail: str = "") -> None:
    RESULTS.append((name, status, detail))
    mark = {"ok": "✅", "fail": "❌", "skip": "⏭"}[status]
    print(f"  {mark} {name}" + (f" —— {detail}" if detail else ""), flush=True)


def _ext_name(e) -> str:
    n = e.extension_name
    return n.decode() if isinstance(n, bytes) else str(n)


# ================================================================ A 运行时与扩展

NEEDED = [
    "XR_EXTX_overlay",
    "XR_KHR_opengl_enable",
    "XR_KHR_composition_layer_cylinder",
    "XR_EXT_hand_tracking",
    # ⚠️ 这个**必须**有：Monado 系运行时的 OpenGL 绑定是它，不是标准的 OPENGL_WAYLAND/XCB。
    #    实测证据：Monado 的 oxr_session.c 只分发 XLIB / WIN32 / ES_ANDROID / VULKAN /
    #    **EGL_MNDX** / D3D11 / D3D12 —— **没有 OPENGL_WAYLAND_KHR**
    #    （所以用 Wayland 绑定会得到
    #     "Argument chain does not contain any known graphics bindings"）。
    "XR_MNDX_egl_enable",
]


def check_runtime() -> list[str]:
    import xr
    try:
        exts = [_ext_name(e) for e in xr.enumerate_instance_extension_properties()]
    except Exception as exc:  # noqa: BLE001
        record("A 运行时与扩展", "fail", f"枚举失败：{type(exc).__name__}: {exc}")
        return []
    missing = [x for x in NEEDED if x not in exts]
    if missing:
        record("A 运行时与扩展", "fail", f"缺 {missing}（共 {len(exts)} 个扩展）")
    else:
        record("A 运行时与扩展", "ok", f"需要的 {len(NEEDED)} 个扩展都在（共 {len(exts)} 个）")
    return exts


# ================================================================ C GL + Wayland context

# GL 后端由**生产实现**提供（`vlt/output/openxr_overlay.py` 的 `create_gl_context()`）：
#   * Wayland → libwayland-client + libEGL，图形绑定 `GraphicsBindingEGLMNDX`；
#   * X11     → libX11 + GLX pbuffer，图形绑定 `GraphicsBindingOpenGLXlibKHR`。
# `--gl wayland|x11` 可强制某一条（默认 auto：Wayland 优先，失败回退 X11）。
#
# ⚠️ 历史：niri 上曾经只有 Wayland 一条路能用 —— 它的 X11 是精简的
#   `xwayland-satellite`：`glXChooseFBConfig`/`glXCreatePbuffer` 都成功，但**所有**
#   context 创建方式一律被 X 服务器拒掉（`GLXBadFBConfig`/`BadValue`）。这是 satellite
#   的特例；真正的 Xorg 会话上 GLX 正常（XLIB 绑定）。
#
# 两种后端都在构造时把 context make current —— OpenXR 要求调用 xrCreateSession 时
# 绑定里的 context 是 current 的（Monado 的 EGL 分支自己取 eglGetCurrentContext()）。

# GL 常量（spike 自查用；EGL/GLX 常量都在生产实现里）
GL_VENDOR, GL_RENDERER, GL_VERSION = 0x1F00, 0x1F01, 0x1F02


class _ProductionGlContext:
    """把**生产 GL 后端**适配成 spike 用的小接口（width/height/gl/xr_binding/info）。

    X11/GLX 与 Wayland/EGL 两个后端都在产品代码里
    （`vlt/output/openxr_overlay.py` 的 `create_gl_context()`）—— spike 再抄一份
    就是两个口径（同一件事写两遍），所以这里只做适配，不重复实现。
    """

    def __init__(self, width: int = 1024, height: int = 440, prefer: str = "auto") -> None:
        import os

        from vlt.output.openxr_overlay import create_gl_context
        if prefer != "auto":
            os.environ["VLT_OVERLAY_GL"] = prefer
        self.width, self.height = width, height
        self._ctx = create_gl_context()
        self.gl = self._ctx.gl

    def name(self) -> str:
        return self._ctx.name

    def xr_binding(self):
        return self._ctx.binding()

    def info(self) -> str:
        from vlt.output.openxr_overlay import GL_RENDERER, GL_VENDOR
        return (f"{self._ctx.name} / {self._ctx.gl_string(GL_VENDOR)} / "
                f"{self._ctx.gl_string(GL_RENDERER)}")


# ================================================================ 会话状态机

def _pump_events(instance):
    """把事件队列里的事件全部取出来，返回最后一次见到的 session state（没有则 None）。

    ⚠️ OpenXR 的会话状态（IDLE→READY→SYNCHRONIZED→VISIBLE→FOCUSED）**只靠泵事件推进**。
        不泵事件，会话永远停在 IDLE/READY，`xrWaitFrame` 就会报
        `XR_ERROR_SESSION_NOT_RUNNING`。
    """
    import xr
    state = None
    want = int(xr.StructureType.EVENT_DATA_SESSION_STATE_CHANGED)
    while True:
        try:
            ev = xr.poll_event(instance)
        except Exception:                # noqa: BLE001 — XR_EVENT_UNAVAILABLE：队列空了
            break
        try:
            etype = int(ev.type)
        except Exception:                # noqa: BLE001
            break
        if etype == want:
            # ⚠️ 必须从**缓冲区起始**强转，不能从 `ev.varying` 起 ——
            #    `XrEventDataBuffer{ type(0), next(8), varying(16..) }` 而
            #    `XrEventDataSessionStateChanged{ type(0), next(8), session(16), state(24), time(32) }`；
            #    从 varying 起算会整整偏 16 字节，读出来的 state 是垃圾（实测得到 0=UNKNOWN）。
            ssc = ctypes.cast(ctypes.pointer(ev),
                              ctypes.POINTER(xr.EventDataSessionStateChanged)).contents
            try:
                state = xr.SessionState(int(ssc.state))
            except ValueError:
                pass
    return state


def _begin_session(instance, session, system_id, timeout_s: float = 5.0):
    """等状态机走到 READY，再 `xrBeginSession`。返回 (成功, 说明)。

    源码依据：Monado 的 `oxr_session_begin()` 第一件事就是
        if (sess->state != XR_SESSION_STATE_READY)
            return oxr_error(log, XR_ERROR_SESSION_NOT_READY, "Session is not ready to begin");
    所以建完 session 直接 `xrWaitFrame` 必然拿到 SESSION_NOT_RUNNING。
    """
    import xr
    deadline = time.monotonic() + timeout_s
    state = None
    seen: list[str] = []
    while time.monotonic() < deadline:
        s = _pump_events(instance)
        if s is not None and s != state:
            state = s
            seen.append(str(s).rsplit(".", 1)[-1])
        if state == xr.SessionState.READY:
            break
        if state in (xr.SessionState.EXITING, xr.SessionState.LOSS_PENDING):
            return False, f"会话直接进入 {state}"
        time.sleep(0.01)
    else:
        return False, (f"等 READY 超时（当前 {state}；见过的状态序列 "
                       f"{'→'.join(seen) or '（一个事件都没收到）'}）")

    try:
        configs = list(xr.enumerate_view_configurations(instance, system_id))
    except Exception as exc:             # noqa: BLE001
        configs, cfg_err = [], str(exc)
    else:
        cfg_err = ""
    want = xr.ViewConfigurationType.PRIMARY_STEREO
    vct = want if want in configs else (configs[0] if configs
                                       else xr.ViewConfigurationType.PRIMARY_STEREO)
    xr.begin_session(session, xr.SessionBeginInfo(primary_view_configuration_type=vct))

    # 再泵几轮，让状态推进到 SYNCHRONIZED / VISIBLE
    for _ in range(50):
        _pump_events(instance)
        time.sleep(0.01)
    detail = f"view_config={vct}"
    if configs:
        detail += f"（系统支持 {len(configs)} 种）"
    elif cfg_err:
        detail += f"（枚举失败：{cfg_err}）"
    return True, detail


# ================================================================ 主流程

def _create_instance(exts: list[str], enabled: list[str]):
    """按需启用扩展建 instance。运行时没起来时抛异常。"""
    import xr
    return xr.create_instance(xr.InstanceCreateInfo(
        application_info=xr.ApplicationInfo(application_name="vlt-spike",
                                            application_version=1),
        enabled_extension_names=enabled))


def _wait_for_runtime(exts: list[str], timeout_s: float):
    """等运行时起来（头显连上）再继续。

    为什么不能拿「枚举扩展」当就绪判据：那一步**不需要 daemon**，Monado/WiVRn 的 .so
    在被 dlopen 时就能答，所以它在运行时没起来时也会「成功」—— 拿它判就绪会一路假通过。
    真正需要 daemon 的是 `xrCreateInstance`，所以拿它轮询。
    """
    enabled = [e for e in ("XR_EXTX_overlay", "XR_KHR_opengl_enable",
                           "XR_KHR_composition_layer_cylinder", "XR_EXT_hand_tracking",
                           "XR_MNDX_egl_enable")
               if not exts or e in exts]
    deadline = time.monotonic() + timeout_s
    attempt = 0
    while True:
        attempt += 1
        try:
            return _create_instance(exts, enabled), enabled
        except Exception as exc:  # noqa: BLE001
            left = deadline - time.monotonic()
            if left <= 0:
                record("B1 xrCreateInstance", "fail",
                       f"{type(exc).__name__}: {exc}（等了 {timeout_s:.0f}s 仍没等到运行时）")
                return None, enabled
            if attempt == 1 or attempt % 10 == 0:
                print(f"  ⏳ 等运行时…（还剩 {left:.0f}s；"
                      f"头显连上了吗？WiVRn/Monado 起来了吗？）", flush=True)
            time.sleep(1.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="B0 spike：OpenXR overlay 后端可行性验证")
    ap.add_argument("--wait", type=float, default=0.0, metavar="秒",
                    help="先等运行时就绪（头显连上）再开始，默认 0 = 立刻跑")
    ap.add_argument("--gl", choices=["auto", "wayland", "x11"], default="auto",
                    help="强制 GL 后端（auto：Wayland 优先、失败回退 X11）")
    ap.add_argument("--gl-only", action="store_true",
                    help="只验 [C] GL context + 图形绑定结构（不需要头显/运行时，可本机单独跑）")
    args = ap.parse_args()

    if args.gl_only:
        # 这一项跟 OpenXR 无关 —— 单独抽出来是为了能在没有头显时先验通，
        # 免得为了一个 ctypes 签名错误让人反复戴头显（真的踩过）。
        print("=== [C] GL context + 图形绑定（--gl-only）===", flush=True)
        try:
            glx = _ProductionGlContext(prefer=args.gl)
            record(f"C  GL context（{glx.name()}）", "ok", glx.info())
            b = glx.xr_binding()      # 顺带验绑定结构体能不能构造出来
            record("C2 图形绑定结构", "ok", f"{type(b).__name__}")
        except Exception as exc:  # noqa: BLE001
            record("C  GL context", "fail", f"{type(exc).__name__}: {exc}")
        _summary()
        return 0

    import xr

    print("=== B0 spike：OpenXR overlay 后端可行性 ===", flush=True)
    print(f"    pyopenxr {getattr(xr, '__version__', '?')}\n", flush=True)

    print("[A] 运行时与扩展", flush=True)
    exts = check_runtime()

    print("\n[B] instance + system", flush=True)
    if args.wait > 0:
        print(f"    （--wait {args.wait:.0f}s：等 xrCreateInstance 成功）", flush=True)
        instance, enabled = _wait_for_runtime(exts, args.wait)
        if instance is None:
            _summary()
            return 1
        record("B1 xrCreateInstance", "ok", f"启用了 {enabled}")
    else:
        try:
            enabled = [e for e in ("XR_EXTX_overlay", "XR_KHR_opengl_enable",
                                   "XR_KHR_composition_layer_cylinder",
                                   "XR_EXT_hand_tracking", "XR_MNDX_egl_enable")
                       if not exts or e in exts]
            instance = _create_instance(exts, enabled)
            record("B1 xrCreateInstance", "ok", f"启用了 {enabled}")
        except Exception as exc:  # noqa: BLE001
            record("B1 xrCreateInstance", "fail", f"{type(exc).__name__}: {exc}"
                   "（运行时没起来？加 --wait 60 等头显连上）")
            _summary()
            return 1

    system_id = None
    try:
        system_id = xr.get_system(instance)
        record("B2 xrGetSystem", "ok", f"system_id={system_id}")
    except Exception as exc:  # noqa: BLE001
        record("B2 xrGetSystem", "fail", f"{type(exc).__name__}: {exc}（没有 HMD？）")
        _summary()
        return 1

    print("\n[C] GL context + 图形绑定", flush=True)
    glx = None
    try:
        glx = _ProductionGlContext(prefer=args.gl)
        record(f"C  GL context（{glx.name()}）", "ok", glx.info())
    except Exception as exc:  # noqa: BLE001
        record("C  GL context", "fail", f"{type(exc).__name__}: {exc}")
        _summary()
        return 1

    print("\n[D] overlay session", flush=True)
    session = None
    try:
        overlay_info = xr.SessionCreateInfoOverlayEXTX(
            create_flags=xr.OverlaySessionCreateFlagsEXTX(0),   # 规范要求必须为 0
            session_layers_placement=100)
        # 绑定由 GL 后端给：Wayland → EGL_MNDX，X11 → GraphicsBindingOpenGLXlibKHR
        # （Monado 系运行时对 Wayland 只实现 EGL_MNDX，没有 OPENGL_WAYLAND；
        #   XLIB 本来就在它的分发表里。）
        binding = glx.xr_binding()
        # 链顺序（照 VRCX-0 的 OpenXR 后端）：SessionCreateInfo → GraphicsBinding → Overlay
        binding.next = ctypes.cast(ctypes.pointer(overlay_info), ctypes.c_void_p)
        create_info = xr.SessionCreateInfo(system_id=system_id)
        create_info.next = ctypes.cast(ctypes.pointer(binding), ctypes.c_void_p)

        # ⚠️ **建 session 之前必须先查图形需求**，否则 Monado 直接拒：
        #    `XR_ERROR_GRAPHICS_REQUIREMENTS_CALL_MISSING: Has not called
        #     xrGetOpenGL[ES]GraphicsRequirementsKHR`
        #    源码依据：oxr_session.c 的 EGL_MNDX 分支检查 `sys->gotten_requirements`
        #    （`#ifdef XR_USE_PLATFORM_EGL` 那一段），不满足就 return 该错误。
        req = xr.get_opengl_graphics_requirements_khr(instance, system_id)
        record("D0 GL 图形需求", "ok",
               f"min={req.min_api_version_supported} max={req.max_api_version_supported}")

        session = xr.create_session(instance, create_info)
        record("D  overlay session", "ok", "XR_EXTX_overlay 会话已建立")

        # ⚠️ 建完 session 还不算「在跑」——必须泵事件等 READY 再 xrBeginSession，
        #    否则后面每个 xrWaitFrame 都会报 SESSION_NOT_RUNNING（实测踩过）。
        started, why = _begin_session(instance, session, system_id)
        record("D1 xrBeginSession", "ok" if started else "fail", why)
        if not started:
            _summary()
            return 1
    except Exception as exc:  # noqa: BLE001
        record("D  overlay session", "fail", f"{type(exc).__name__}: {exc}")
        _summary()
        return 1

    print("\n[E] swapchain + 提交一帧 Quad", flush=True)
    quad_ok, formats = _submit_frame(instance, session, glx, layer_kind="quad")

    print("\n[F] Cylinder 层（弯曲）", flush=True)
    if "XR_KHR_composition_layer_cylinder" in exts:
        _submit_frame(instance, session, glx, layer_kind="cylinder")
    else:
        record("F  Cylinder 层", "skip", "运行时没声明该扩展 → curvature 不可用")

    print("\n[G] 手部 pose（决定 anchor 能选哪些）", flush=True)
    _check_hand_pose(instance, session)

    _summary()
    return 0 if quad_ok else 1


def _make_swapchain(session, glx):
    """建一个 RGBA8 swapchain 并返回 (swapchain, gl 纹理 id 列表)。"""
    import xr
    formats = list(xr.enumerate_swapchain_formats(session))
    print(f"     可用 swapchain 格式：{[hex(f) for f in formats[:12]]}"
          f"{' …' if len(formats) > 12 else ''}", flush=True)
    # ⚠️ 优先 GL_RGBA8(0x8058)，其次 sRGB 变体 —— 面板要的就是一张普通 RGBA 贴图。
    #    直接取 formats[0] 有风险：可能是 GL_RGBA16F 之类，我们按 8 位上传会错位。
    for cand in (0x8058, 0x8C43, 0x8059):
        if cand in formats:
            want = cand
            break
    else:
        want = formats[0] if formats else 0
        print(f"     ⚠️ 没找到首选的 RGBA8/sRGB8，退回 {hex(want)}", flush=True)
    sc = xr.create_swapchain(session, xr.SwapchainCreateInfo(
        format=want, sample_count=1, width=glx.width, height=glx.height,
        face_count=1, array_size=1, mip_count=1))
    # ⚠️ enumerate_swapchain_images 必须给 element_type —— GL 用 SwapchainImageOpenGLKHR
    #    （Vulkan 是另一套类型，给错了拿到的就是野指针）
    imgs = xr.enumerate_swapchain_images(sc, xr.SwapchainImageOpenGLKHR)
    tex_ids = [getattr(i, "image", None) for i in imgs]
    return sc, want, tex_ids


def _submit_frame(instance, session, glx, *, layer_kind: str) -> tuple[bool, list]:
    import xr

    try:
        sc, fmt, tex_ids = _make_swapchain(session, glx)
    except Exception as exc:  # noqa: BLE001
        record(f"E  swapchain ({layer_kind})", "fail", f"{type(exc).__name__}: {exc}")
        return False, []

    # 建参考空间（LOCAL）——层必须挂在某个 space 上
    try:
        ref = xr.create_reference_space(session, xr.ReferenceSpaceCreateInfo(
            reference_space_type=xr.ReferenceSpaceType.LOCAL))
    except Exception as exc:  # noqa: BLE001
        record(f"E  LOCAL reference space ({layer_kind})", "fail",
               f"{type(exc).__name__}: {exc}")
        return False, []

    try:
        _pump_events(instance)          # 不泵事件，会话状态不会推进
        frame = xr.wait_frame(session)
        xr.begin_frame(session)
        idx = xr.acquire_swapchain_image(sc)
        # ⚠️ 拿到 image 之后必须 wait 才能写 —— OpenGL 绑定下这是规范要求，
        #    漏了会出现「上传了但画面还是上一帧」甚至上传失败。
        #    timeout 用 1 秒而不是 XR_INFINITE_DURATION：出错时宁可报错也别吊死。
        xr.wait_swapchain_image(sc, xr.SwapchainImageWaitInfo(timeout=1_000_000_000))
        # 往纹理里写点东西，证明贴图通路可用（一帧纯色）
        gl = glx.gl
        gl.glBindTexture.argtypes = [ctypes.c_uint, ctypes.c_uint]
        gl.glTexImage2D.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
                                    ctypes.c_void_p]
        GL_TEXTURE_2D, GL_RGBA, GL_UNSIGNED_BYTE = 0x0DE1, 0x1908, 0x1401
        buf = (ctypes.c_ubyte * (glx.width * glx.height * 4))()
        for i in range(0, len(buf), 4):          # 深灰底、不全黑，便于肉眼确认
            buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = 20, 22, 28, 205
        if tex_ids and tex_ids[idx] is not None:
            gl.glBindTexture(GL_TEXTURE_2D, tex_ids[idx])
            gl.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, glx.width, glx.height, 0,
                            GL_RGBA, GL_UNSIGNED_BYTE, buf)
        xr.release_swapchain_image(sc)

        sub = xr.SwapchainSubImage(swapchain=sc,
                                   image_rect=xr.Rect2Di(offset=xr.Offset2Di(0, 0),
                                                         extent=xr.Extent2Di(glx.width, glx.height)))
        pose = xr.Posef(orientation=xr.Quaternionf(0.0, 0.0, 0.0, 1.0),
                        position=xr.Vector3f(0.0, 0.0, -1.0))
        if layer_kind == "cylinder":
            layer = xr.CompositionLayerCylinderKHR(
                sub_image=sub, pose=pose, radius=0.25, central_angle=1.2,
                aspect_ratio=glx.width / glx.height, space=ref)
        else:
            layer = xr.CompositionLayerQuad(
                sub_image=sub, pose=pose, space=ref,
                size=xr.Extent2Df(glx.width / 1024.0, glx.height / 1024.0),
                eye_visibility=xr.EyeVisibility.BOTH)
        # pyopenxr 里没有 LP_ 前缀的别名，直接用 POINTER(CompositionLayerBaseHeader)
        base_t = ctypes.POINTER(xr.CompositionLayerBaseHeader)
        base = ctypes.cast(ctypes.pointer(layer), base_t)
        arr = (base_t * 1)(base)
        xr.end_frame(session, xr.FrameEndInfo(
            display_time=frame.predicted_display_time,
            environment_blend_mode=xr.EnvironmentBlendMode.OPAQUE,
            layer_count=1, layers=arr))
        record(f"E  提交 {layer_kind} 层", "ok",
               f"format=0x{fmt:x} 纹理={tex_ids[:2]} 尺寸={glx.width}x{glx.height} "
               f"should_render={frame.should_render}")
        return True, formats_of(fmt)
    except Exception as exc:  # noqa: BLE001
        record(f"E  提交 {layer_kind} 层", "fail", f"{type(exc).__name__}: {exc}")
        return False, []


def formats_of(fmt: int) -> list:
    return [f"0x{fmt:x}"]


def _check_hand_pose(instance, session) -> None:
    """★ 关键未知：overlay session 里能不能读到手部 pose。

    overlay session 拿不到**输入焦点**（规范要求 createFlags=0），但 pose 查询不是输入焦点。
    这一步既要验「能不能读」，也要在**读不到时区分原因**：

      A. 交互 profile 没匹配上  → `get_current_interaction_profile` 会给出空路径
      B. 没同步动作           → 缺 `xrSyncActions`，动作状态永远不更新
      C. 会话没到 VISIBLE/FOCUSED → 报会话状态就能看出来
      D. overlay 会话压根不给 pose → 连 **HMD 的 VIEW 空间**都读不到位置

    所以这一版会同时定位「手部 action space」和「VIEW 参考空间」并打印原始 flags ——
    两个都不 tracked = 情况 D（那就只能退化成 anchor: hmd）；只有 VIEW tracked = 情况 A/B/C。
    """
    import xr

    def flags_str(v: int) -> str:
        names = {1: "ORIENT_VALID", 2: "POSITION_VALID", 4: "ORIENT_TRACKED", 8: "POSITION_TRACKED"}
        bits = [n for b, n in names.items() if int(v) & b]
        return f"0x{int(v):x}[{','.join(bits) or '无'}]"

    # ---- 建 action set / pose action / 建议绑定
    try:
        aset = xr.create_action_set(instance, xr.ActionSetCreateInfo(
            action_set_name="vlt_spike", localized_action_set_name="VLT Spike", priority=0))
        hand_path = xr.string_to_path(instance, "/user/hand/right")
        grip = xr.string_to_path(instance, "/user/hand/right/input/grip/pose")
        act = xr.create_action(aset, xr.ActionCreateInfo(
            action_name="hand_pose", action_type=xr.ActionType.POSE_INPUT,
            localized_action_name="Hand Pose", subaction_paths=[hand_path]))
        suggested = 0
        for profile in ("/interaction_profiles/khr/simple_controller",
                        "/interaction_profiles/oculus/touch_controller",
                        "/interaction_profiles/valve/index_controller",
                        "/interaction_profiles/microsoft/motion_controller"):
            try:
                xr.suggest_interaction_profile_bindings(instance, xr.InteractionProfileSuggestedBinding(
                    interaction_profile=xr.string_to_path(instance, profile),
                    suggested_bindings=[xr.ActionSuggestedBinding(action=act, binding=grip)]))
                suggested += 1
            except Exception:  # noqa: BLE001 — 运行时不认这个 profile，跳过
                pass
        xr.attach_session_action_sets(session, xr.SessionActionSetsAttachInfo(action_sets=[aset]))
        record("G1 动作绑定", "ok",
               f"建议了 {suggested} 个交互 profile × grip/pose；subaction /user/hand/right")
    except Exception as exc:  # noqa: BLE001
        record("G1 动作绑定", "fail", f"{type(exc).__name__}: {exc}")
        return

    # ---- 同步动作（不 sync，动作状态永远不更新）
    try:
        xr.sync_actions(session, xr.ActionsSyncInfo(active_action_sets=[
            xr.ActiveActionSet(action_set=aset, subaction_path=xr.Path(0))]))
        record("G2 xrSyncActions", "ok", "已同步")
    except Exception as exc:  # noqa: BLE001
        record("G2 xrSyncActions", "fail", f"{type(exc).__name__}: {exc}")
        return

    # ---- 当前交互 profile（空 = 没有 profile 匹配上）
    try:
        prof = xr.get_current_interaction_profile(session, hand_path)
        prof_name = (xr.path_to_string(instance, prof.interaction_profile)
                     if int(prof.interaction_profile) else "")
        record("G3 交互 profile", "ok" if prof_name else "fail",
               prof_name or "空 —— 没有任何建议的 profile 匹配上（控制器型号对不上？）")
    except Exception as exc:  # noqa: BLE001
        record("G3 交互 profile", "fail", f"{type(exc).__name__}: {exc}")

    # ---- 两个空间：手部 action space + HMD 的 VIEW 参考空间
    try:
        hand_space = xr.create_action_space(session, xr.ActionSpaceCreateInfo(
            action=act, subaction_path=hand_path,
            pose_in_action_space=xr.Posef(orientation=xr.Quaternionf(0.0, 0.0, 0.0, 1.0),
                                          position=xr.Vector3f(0.0, 0.0, 0.0))))
        ref = xr.create_reference_space(session, xr.ReferenceSpaceCreateInfo(
            reference_space_type=xr.ReferenceSpaceType.LOCAL))
        view = xr.create_reference_space(session, xr.ReferenceSpaceCreateInfo(
            reference_space_type=xr.ReferenceSpaceType.VIEW))
    except Exception as exc:  # noqa: BLE001
        record("G4 空间创建", "fail", f"{type(exc).__name__}: {exc}")
        return

    # ---- 连续几帧看追踪情况
    try:
        state = None
        for i in range(20):
            s = _pump_events(instance) or state
            state = s
            frame = xr.wait_frame(session)
            xr.begin_frame(session)
            xr.end_frame(session, xr.FrameEndInfo(
                display_time=frame.predicted_display_time,
                environment_blend_mode=xr.EnvironmentBlendMode.OPAQUE,
                layer_count=0, layers=None))
            xr.sync_actions(session, xr.ActionsSyncInfo(active_action_sets=[
                xr.ActiveActionSet(action_set=aset, subaction_path=xr.Path(0))]))
            t = frame.predicted_display_time or time.monotonic_ns()
            hl = xr.locate_space(hand_space, ref, xr.Time(t))
            vl = xr.locate_space(view, ref, xr.Time(t))
            if i % 5 == 0 or i == 19:
                print(f"      [{i:2}] session={state} 手={flags_str(hl.location_flags)} "
                      f"VIEW={flags_str(vl.location_flags)}", flush=True)
            if int(hl.location_flags) & int(xr.SpaceLocationFlags.POSITION_TRACKED_BIT):
                p = hl.pose.position
                record("G  手部 pose 可读", "ok",
                       f"位置=({p.x:.3f}, {p.y:.3f}, {p.z:.3f}) → anchor: right_hand / tracker 可用")
                return
            time.sleep(0.03)

        view_ok = bool(int(vl.location_flags) & int(xr.SpaceLocationFlags.POSITION_TRACKED_BIT))
        if view_ok:
            record("G  手部 pose 可读", "fail",
                   "HMD 的 VIEW 空间能追踪，但手部 action space 不行 → 问题在动作/绑定侧"
                   "（看 G3 的 profile 是否为空）")
        else:
            record("G  手部 pose 可读", "fail",
                   "连 HMD 的 VIEW 空间都不 tracked → overlay 会话拿不到位姿（情况 D）"
                   "→ anchor 只能退化成 hmd")
    except Exception as exc:  # noqa: BLE001
        record("G  手部 pose 可读", "fail", f"{type(exc).__name__}: {exc}")


def _summary() -> None:
    print("\n" + "=" * 62)
    ok = sum(1 for _, s, _ in RESULTS if s == "ok")
    fail = [n for n, s, _ in RESULTS if s == "fail"]
    skip = [n for n, s, _ in RESULTS if s == "skip"]
    print(f"结论：{ok} 项通过" + (f"，{len(fail)} 项失败" if fail else "")
          + (f"，{len(skip)} 项跳过" if skip else ""))
    if fail:
        print("失败项：" + "、".join(fail))
    print("=" * 62)


if __name__ == "__main__":
    raise SystemExit(main())
