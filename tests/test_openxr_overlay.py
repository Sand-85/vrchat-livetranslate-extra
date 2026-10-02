#!/usr/bin/env python
"""OpenXR 手腕屏后端的**纯逻辑**测试（不需要头显、不需要运行时）。

## 为什么这几个纯函数值得单独测

`euler_to_quaternion` 的**旋转顺序**如果和 Windows 侧不一致，
配置里那组实测调好的 `rot: [-47, -16, 0]` 在 Linux 上会**静默转到别的方向** ——
面板会以错误的角度贴在手腕上，而且没有任何报错。这种错只能靠测试钉住。

这里的验证方式刻意**与实现不同路**：
  * 实现：四元数乘法 `qz ⊗ qy ⊗ qx`
  * 测试：3×3 转置矩阵连乘 `Rz · Ry · Rx`，再用它去转向量
两条路算出同一个结果才算对（不是拿实现自己的公式自证）。

`vlt/platform/linux.py` 顶层只 import 标准库，`vlt/output/overlay.py` 也只是**惰性**
import openvr，所以这个测试在两个平台上都能跑。
"""
from __future__ import annotations

import inspect
import math
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.output.openxr_overlay import (  # noqa: E402
    CYLINDER_EXT,
    TRACKER_EXT,
    TRACKER_ROLES,
    OpenXrOverlay,
    XrOverlaySession,
    anchor_paths,
    apply_overlay_alpha,
    effective_curvature,
    euler_to_quaternion,
    layer_alpha_flags,
    layer_geometry,
    pick_swapchain_format,
    render_alpha_test,
    rotate_vector,
    should_rebuild,
)
from vlt.output.overlay import OverlayConfig, render_panel  # noqa: E402

# ---------------------------------------------------------------- 独立的参照实现


def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _rot_rx(t):
    c, s = math.cos(t), math.sin(t)
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def _rot_ry(t):
    c, s = math.cos(t), math.sin(t)
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def _rot_rz(t):
    c, s = math.cos(t), math.sin(t)
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def _ref_matrix(rot_deg):
    """参照实现：Rz · Ry · Rx（与 Windows 侧 build_matrix 同一约定，但代码路径不同）。"""
    rx, ry, rz = (math.radians(a) for a in rot_deg)
    return _mat_mul(_mat_mul(_rot_rz(rz), _rot_ry(ry)), _rot_rx(rx))


def _quat_rotate(q, v):
    """用四元数转一个向量（q 为 (x,y,z,w)）—— 与被测实现的推导路径不同。"""
    x, y, z, w = q
    # v' = v + 2*w*(q_vec × v) + 2*(q_vec × (q_vec × v))
    qv = (x, y, z)
    cross = lambda a, b: (a[1] * b[2] - a[2] * b[1],       # noqa: E731
                          a[2] * b[0] - a[0] * b[2],
                          a[0] * b[1] - a[1] * b[0])
    t = cross(qv, v)
    t2 = cross(qv, t)
    return tuple(v[i] + 2.0 * w * t[i] + 2.0 * t2[i] for i in range(3))


def _mat_vec(m, v):
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))


# ---------------------------------------------------------------- 测试

def test_quaternion_matches_matrix_convention():
    """★ 核心：四元数转出来的方向必须与 Rz·Ry·Rx 矩阵一致。

    这直接决定「用户调好的 rot 在 Linux 上是不是同一个方向」。
    """
    cases = [
        (0.0, 0.0, 0.0), (-47.0, -16.0, 0.0),      # config 里实测调好的那组
        (90.0, 0.0, 0.0), (0.0, 90.0, 0.0), (0.0, 0.0, 90.0),
        (30.0, -60.0, 120.0), (-15.5, 22.25, -33.75),
    ]
    basis = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (0.3, -0.7, 0.5)]
    worst = 0.0
    for rot in cases:
        q = euler_to_quaternion(rot)
        m = _ref_matrix(rot)
        for v in basis:
            got = _quat_rotate(q, v)
            want = _mat_vec(m, v)
            err = max(abs(got[i] - want[i]) for i in range(3))
            worst = max(worst, err)
            assert err < 1e-9, (
                f"rot={rot} v={v}：四元数转出 {got}，矩阵转出 {want}（差 {err:.2e}）\n"
                f"—— 说明 euler_to_quaternion 的旋转顺序与 Windows 侧 build_matrix 不一致")
    print(f"  四元数与 Rz·Ry·Rx 矩阵一致 OK（{len(cases)} 组角度，最大误差 {worst:.1e}）")


def test_quaternion_is_normalized():
    """四元数必须是单位四元数，否则贴图会被缩放。"""
    for rot in ((0, 0, 0), (-47, -16, 0), (123, -45, 67)):
        x, y, z, w = euler_to_quaternion(rot)
        n = math.sqrt(x * x + y * y + z * z + w * w)
        assert abs(n - 1.0) < 1e-12, f"rot={rot} 的模是 {n}"
    print("  四元数都是单位长度 OK")


def test_anchor_paths_mapping():
    """锚点 → OpenXR 路径的映射表（两端语义要对得上）。"""
    assert anchor_paths("right_hand") == (
        "/user/hand/right/input/grip/pose", "/user/hand/right")
    assert anchor_paths("left_hand") == (
        "/user/hand/left/input/grip/pose", "/user/hand/left")
    full, top = anchor_paths("tracker", 0)
    assert full == f"{top}/input/grip/pose" and top.startswith("/user/vive_tracker_htcx/role/")
    # tracker_index 越界要夹住，不能 IndexError
    full_oob, _ = anchor_paths("tracker", 999)
    assert full_oob == f"/user/vive_tracker_htcx/role/{TRACKER_ROLES[-1]}/input/grip/pose"
    # hmd 不用 action（走 VIEW 参考空间）
    assert anchor_paths("hmd") == (None, None)
    assert anchor_paths("不认识的东西") == (None, None)
    print("  锚点路径映射 OK（含越界夹取）")


def test_swapchain_format_prefers_rgba8():
    """★ 格式必须优先 8 位 RGBA —— 实测可用列表的第一个是 0x805b(RGBA16F)。

    直接取 `formats[0]` 就会拿到 16 位浮点格式，而我们按 8 位上传 → 画面错乱。
    """
    real = [0x805B, 0x881A, 0x8C43, 0x8058, 0x8CAC, 0x81A5]
    assert pick_swapchain_format(real) == 0x8058, "没优先选 GL_RGBA8"
    assert pick_swapchain_format([0x805B, 0x8C43]) == 0x8C43, "没有 RGBA8 时应退到 sRGB8_ALPHA8"
    assert pick_swapchain_format([0x805B, 0x8059]) == 0x8059
    assert pick_swapchain_format([0x805B]) == 0x805B, "都没有时应退回第一个而不是崩"
    assert pick_swapchain_format([]) == 0x8058, "空列表要有兜底"
    print("  格式选择优先 RGBA8 OK")


def test_layer_geometry_matches_windows_curvature():
    """★ 弯曲度的定义必须与 Windows（`SetOverlayCurvature`）**同一个口径**。

    openvr.h 原文：「curvature 是占整圆的比例，1 = 完全闭合的圆柱；给定半径时
    curvature = overlay.width / (2π·r)」⇒ 我们这边就必须是
    `central_angle = 2π·curvature`、`radius = width/central_angle`，
    而且 **width_m 是弧长**（不是弦长 —— 旧实现按弦长算，还带 5× 系数与 1.5 rad 上限，
    结果 0.3 以上滑到底没区别，和 Windows 也对不上）。
    """
    aspect, w = 1024 / 440, 0.23

    flat = layer_geometry(w, aspect, 0.0)
    assert flat["kind"] == "quad" and flat["size"][0] == w
    assert abs(flat["size"][1] - w / aspect) < 1e-9
    assert layer_geometry(w, aspect, 0.005)["kind"] == "quad", "小到看不出弯就该走平面"
    assert layer_geometry(w, aspect, -1.0)["kind"] == "quad", "负值也该走平面"

    for c in (0.05, 0.15, 0.2, 0.3, 0.5):
        g = layer_geometry(w, aspect, c)
        assert g["kind"] == "cylinder"
        assert abs(g["central_angle"] - 2 * math.pi * c) < 1e-9, f"c={c} 的圆心角不是 2π·c"
        assert abs(g["radius"] * g["central_angle"] - w) < 1e-9, "弧长必须等于 width_m"
        assert abs(g["aspect_ratio"] - aspect) < 1e-9
        # ★ 半径不能当位置用：柱面层要把「轴」沿面板局部 +Z 挪 radius（在 submit 里做）
        assert g["pose_offset"] == (0.0, 0.0, g["radius"])

    # 弯曲 0.5 = 半圈（180°）：弦长只有弧长的 2/π ≈ 64%，这是 Windows 口径的必然结果
    half = layer_geometry(w, aspect, 0.5)
    assert abs(half["central_angle"] - math.pi) < 1e-3
    chord = 2 * half["radius"] * math.sin(half["central_angle"] / 2)
    assert abs(chord / w - 2 / math.pi) < 1e-3

    # 极端值要夹住，不能出 NaN / 除零（aspect=0 也要能算）
    for c in (1.0, 5.0):
        g = layer_geometry(w, 0.0, c)
        assert g["kind"] == "cylinder" and g["radius"] > 0
        assert g["central_angle"] < 2 * math.pi, "圆心角越界（规范要求 < 2π）"
    print("  层几何换算 OK（2π 口径 · 弧长=宽度 · 带 pose_offset）")


def test_cylinder_pose_offset_rotates_with_panel():
    """★ 柱面层的「轴」偏移必须跟着**面板的 rot** 转，不能拿世界 Z。

    双路验证（与四元数那条同一风格）：实现用四元数转局部 (0,0,r)，测试用
    Rz·Ry·Rx 矩阵转。最后再钉住真正的目的：**弧面中点要落回原位置**
    （`pose + R·(0,0,−radius) == cfg.pos`）—— 不成立就是用户看到的「面板飘走」。
    """
    for rot in ((0.0, 0.0, 0.0), (-47.0, -16.0, 0.0), (30.0, -60.0, 120.0)):
        g = layer_geometry(0.23, 1024 / 440, 0.2)
        r = g["radius"]
        got = rotate_vector(rot, g["pose_offset"])
        want = _mat_vec(_ref_matrix(rot), (0.0, 0.0, r))
        assert max(abs(got[i] - want[i]) for i in range(3)) < 1e-9, \
            f"rot={rot}：偏移方向与 Rz·Ry·Rx 不一致"

        # 弧面中点（局部 (0,0,-r)）加上 pose 偏移后必须回到原点 = 面板位置不动
        back = rotate_vector(rot, (0.0, 0.0, -r))
        total = [got[i] + back[i] for i in range(3)]
        assert max(abs(v) for v in total) < 1e-9, f"rot={rot}：弧面中点没回到面板位置"

    # 不转的时候就是纯 +Z，长度正好是半径（对齐 Monado layer_cylinder.vert 的 z = -cos(a)*r）
    r0 = layer_geometry(0.23, 1024 / 440, 0.2)
    off = rotate_vector((0.0, 0.0, 0.0), r0["pose_offset"])
    assert abs(off[0]) < 1e-12 and abs(off[1]) < 1e-12
    assert abs(off[2] - r0["radius"]) < 1e-12
    print("  柱面 pose 偏移 OK（随面板 rot 旋转 · 弧面中点回到原位）")


def test_effective_curvature_degrades_without_extension():
    """★ #26-5：运行时没有柱面扩展 → 这一帧按平面层提交，**不许**硬构造柱面层。

    柱面合成层是 `XR_KHR_composition_layer_cylinder` 扩展；拿不到还构造的话
    `xrEndFrame` 会整帧失败（整条手腕屏没了），而这里要的只是「不弯」。
    """
    aspect, w = 1024 / 440, 0.23
    assert effective_curvature(0.3, [CYLINDER_EXT]) == 0.3, "有扩展就该保留弯曲度"
    assert effective_curvature(0.3, ["XR_EXTX_overlay"]) == 0.0, "没扩展必须归 0"
    assert effective_curvature(0.3, []) == 0.0
    assert effective_curvature(0.0, []) == 0.0 and effective_curvature(-1.0, []) == 0.0

    # 归 0 之后几何确实退回平面（这才是 submit 里真正会走的路）
    assert layer_geometry(w, aspect, effective_curvature(0.3, []))["kind"] == "quad"
    assert layer_geometry(w, aspect, effective_curvature(0.3, [CYLINDER_EXT]))["kind"] == "cylinder"
    print("  柱面扩展缺失 → effective_curvature 归 0、退回平面 OK")


def test_extensions_does_not_gamble_on_cylinder_when_unknown():
    """★ #26-5：枚举失败时**不赌**可选扩展 —— 只请求必需项，绝不带上柱面。

    请求一个运行时没有的扩展会让 `create_instance` 直接失败（整条腿消失），
    比「不弯」严重得多。
    """
    import types

    class _E:
        def __init__(self, name: str) -> None:
            self.extension_name = name.encode()

    fake = types.ModuleType("xr")
    fake.enumerate_instance_extension_properties = lambda: [  # type: ignore[attr-defined]
        _E("XR_EXTX_overlay"), _E("XR_KHR_opengl_enable"), _E("XR_MNDX_egl_enable")]

    saved = sys.modules.get("xr")
    sys.modules["xr"] = fake
    try:
        got = OpenXrOverlay._extensions()          # noqa: SLF001
        assert CYLINDER_EXT not in got, f"运行时没报柱面扩展却启用了它：{got}"
        assert "XR_EXTX_overlay" in got, "必需项不该被漏掉"

        # 枚举抛异常（旧实现走「全都要」，会把柱面也塞进去）→ 只赌必需项
        def boom():
            raise RuntimeError("模拟：枚举设备失败")

        fake.enumerate_instance_extension_properties = boom  # type: ignore[attr-defined]
        got2 = OpenXrOverlay._extensions()         # noqa: SLF001
        assert CYLINDER_EXT not in got2, f"枚举失败时不该赌柱面：{got2}"
        assert "XR_KHR_opengl_enable" in got2, "枚举失败也要保住必需项"

        # 运行时确实报了柱面扩展 → 保留（降级只在真拿不到时发生）
        fake.enumerate_instance_extension_properties = lambda: [  # type: ignore[attr-defined]
            _E(CYLINDER_EXT), _E("XR_EXTX_overlay"), _E("XR_KHR_opengl_enable"),
            _E("XR_MNDX_egl_enable")]
        assert CYLINDER_EXT in OpenXrOverlay._extensions()  # noqa: SLF001
    finally:
        if saved is None:
            sys.modules.pop("xr", None)
        else:
            sys.modules["xr"] = saved
    print("  _extensions 不赌柱面 OK（枚举失败只留必需项 · 真有扩展才启用）")


def test_submit_records_and_uses_enabled_extensions():
    """★ #26-5 接线：`submit()` 用**本会话实际启用的扩展**判定柱面能力。

    纯逻辑测试跑不了真 XR，但可以扫源码钉住「submit 里确实调了 effective_curvature
    并用 self.extensions」，避免哪天有人把它改回无条件的 `cfg.curvature`。
    """
    src = (ROOT / "vlt" / "output" / "openxr_overlay.py").read_text(encoding="utf-8")
    assert "effective_curvature(cfg.curvature, self.extensions)" in src, \
        "submit() 必须按实际启用的扩展决定弯曲度"
    assert "self.extensions = list(extensions)" in src, \
        "create() 必须记下实际启用的扩展"
    print("  submit/create 接线 OK（记下扩展 · 按扩展决定弯曲度）")


def test_should_rebuild_detects_changes():
    """热重载判定：几何/锚点 → 重应用变换；字号/尺寸/**透明度/配色** → 必须重渲贴图。"""
    base = OverlayConfig()
    assert should_rebuild(base, OverlayConfig()) == (False, False), "没改却说改了"

    import dataclasses
    geo = dataclasses.replace(base, width_m=0.30)
    assert should_rebuild(base, geo) == (True, False), "改宽度没被识别成几何变化"

    anchor = dataclasses.replace(base, anchor="hmd", tracker_index=2)
    assert should_rebuild(base, anchor) == (True, False), "改锚点没被识别"

    render = dataclasses.replace(base, font_size=44)
    assert should_rebuild(base, render) == (False, True), "改字号没被识别成需要重渲"

    # ★ 界面上新加的「底板/原文不透明度」滑块走的就是这条路：不重渲 = 拖着没反应
    for field, val in (("bg_alpha", 120), ("source_alpha", 255), ("border_alpha", 200),
                       ("separator", False), ("color_bg", (30, 30, 30)),
                       ("color_theirs", (1, 2, 3))):
        assert should_rebuild(base, dataclasses.replace(base, **{field: val})) == (False, True), \
            f"改 {field} 没被识别成需要重渲"

    both = dataclasses.replace(base, font_size=44, pos=(1.0, 0.0, 0.0))
    assert should_rebuild(base, both) == (True, True)
    print("  热重载判定 OK（含底板/原文不透明度、配色）")


def test_layer_alpha_flags_value():
    """★ 图层 alpha 的两个 flag 值必须是「贴图 alpha 生效 + 未预乘」。

    这是「蓝框外一圈黑」的那个开关：不设 `BLEND_TEXTURE_SOURCE_ALPHA_BIT`，
    整个图层会按不透明合成（贴图里 alpha=0 的边距被当成实心黑）。
    """
    try:
        import xr
    except ImportError:
        print("  ⏭ 没装 pyopenxr（非 Linux 环境），跳过")
        return
    f = int(layer_alpha_flags())
    want = int(xr.CompositionLayerFlags.BLEND_TEXTURE_SOURCE_ALPHA_BIT) \
        | int(xr.CompositionLayerFlags.UNPREMULTIPLIED_ALPHA_BIT)
    assert f == want, f"flag 不对：0x{f:x}，应该是 0x{want:x}（BLEND | UNPREMULTIPLIED）"
    assert f & 0x2, "少了 BLEND_TEXTURE_SOURCE_ALPHA_BIT → 图层不透明 → 一圈黑边"
    assert f & 0x4, "少了 UNPREMULTIPLIED_ALPHA_BIT → 未预乘的贴图会被当预乘 → 偏亮"
    print(f"  图层 alpha flag OK（0x{f:x}）")


def test_apply_overlay_alpha_scales_only_alpha():
    """整层 alpha 乘子 = Linux 侧的 `setOverlayAlpha()`：**只动 alpha，RGB 不碰**。

    只动 alpha 才能做到「底板变淡、文字仍实心」；连 RGB 一起乘（预乘空间的做法）
    会让白字先变灰再变暗 —— 那就不是用户要的东西了。
    """
    from PIL import Image

    img = Image.new("RGBA", (4, 1))
    img.putdata([(255, 255, 255, 255), (12, 14, 20, 205), (0, 0, 0, 0), (10, 20, 30, 128)])

    def _px(im):  # 不用 getdata()：Pillow 14 起弃用，getpixel 一直稳定
        return [im.getpixel((x, 0)) for x in range(4)]

    before = _px(img)
    assert apply_overlay_alpha(img, 1.0) is img, "alpha=1 时不该白拷一份"

    for k, want_alpha in ((0.0, [0, 0, 0, 0]), (0.5, [128, 102, 0, 64]),
                          (0.9, [230, 184, 0, 115])):
        out = _px(apply_overlay_alpha(img, k))
        assert [p[3] for p in out] == want_alpha, \
            f"k={k}：alpha 应为 {want_alpha}，实得 {[p[3] for p in out]}"
        assert [p[:3] for p in out] == [p[:3] for p in before], \
            f"k={k}：RGB 被改了（文字会跟着变暗）"

    # 越界值要夹住，不能崩、也不能溢出（alpha=0 的像素乘完还得是 0）
    assert [p[3] for p in _px(apply_overlay_alpha(img, 5.0))] == [255, 205, 0, 128]
    assert [p[3] for p in _px(apply_overlay_alpha(img, -1.0))] == [0, 0, 0, 0]
    print("  整层 alpha 乘子 OK（只乘 alpha、端点/越界都对）")


def test_panels_have_transparent_margin_and_translucent_plate():
    """贴图侧的独立判据：**边距全透明 + 底板半透明**。

    与上面的 flag 是两条互不相干的失效路径 —— 任何一条坏了，用户看到的一样是
    「蓝框外一圈黑」。这条不需要头显就能跑。
    """
    cfg = OverlayConfig()
    w, h = cfg.size_px
    for name, img in (("面板", render_panel("你好，测试。", "hello", cfg)),
                      ("alpha 判定图", render_alpha_test(cfg))):
        px = img.convert("RGBA").load()
        for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
                   (w // 2, 0), (0, h // 2), (w - 1, h // 2)):
            assert px[xy][3] == 0, f"{name} 的 {xy} 不是全透明（{px[xy]}）→ 屏上就是黑边"
        # 四角必须真的透明（圆角）；中间靠底那一块是底板 → 必须是配置的半透明值
        assert px[(w // 2, h - 20)][3] == cfg.bg_alpha, \
            f"{name} 的底板不是半透明（得到 {px[(w // 2, h - 20)]}）"
    print("  贴图边距全透明 + 底板半透明 OK")


def test_backend_config_field():
    """`overlay.backend` 的默认与解析（null 用来彻底关掉手腕屏）。"""
    assert OverlayConfig().backend == "auto"
    assert OverlayConfig.from_dict({}).backend == "auto"
    assert OverlayConfig.from_dict({"backend": "null"}).backend == "null"
    assert OverlayConfig.from_dict({"backend": None}).backend == "auto"
    print("  backend 配置字段 OK")


def test_composition_layers_declare_alpha_flags():
    """★ 回归锁：合成层**必须**声明 alpha flag（少了就是一整层不透明 → 一圈黑边）。

    flag 值本身的正确性由 `test_layer_alpha_flags_value()` 管；这条只管
    「**每一处**合成层构造都真的把 flag 传下去了」—— Quad 与 Cylinder 两条分支
    都得传，漏一处就是「换个曲率又冒出黑边」这种半好半坏的状态。
    """
    import ast
    backend = ROOT / "vlt" / "output" / "openxr_overlay.py"
    tree = ast.parse(backend.read_text(encoding="utf-8"))

    layers = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "xr"):
            continue
        if node.func.attr not in ("CompositionLayerQuad", "CompositionLayerCylinderKHR"):
            continue
        layers.append((node.lineno, node.func.attr,
                       {kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg}))

    assert layers, "一处合成层构造都没扫到 —— 源码结构变了？"
    names = {name for _, name, _ in layers}
    assert "CompositionLayerQuad" in names and "CompositionLayerCylinderKHR" in names, \
        f"应有 Quad 与 Cylinder 两条分支，实际只有 {names}"
    for lineno, name, kw in layers:
        assert "layer_flags" in kw, (
            f"L{lineno} xr.{name}(…) 没传 layer_flags —— 图层会按不透明合成，"
            f"贴图里的透明边距会变成一圈黑边")
        assert "layer_alpha_flags" in kw["layer_flags"], (
            f"L{lineno} layer_flags={kw['layer_flags']}，应当是 layer_alpha_flags() "
            f"（BLEND_TEXTURE_SOURCE_ALPHA | UNPREMULTIPLIED_ALPHA）")
    print(f"  合成层都带 alpha flag OK（{len(layers)} 处：Quad + Cylinder）")


def test_backend_xr_calls_are_wellformed():
    """★ 静态扫描产品后端里的所有 `xr.*` 调用，抓三类只有真机才会暴露的错：

      1. **句柄错配**：`xr.create_reference_space(self.instance, …)` —— 第一个参数要的是
         **session**。参数**个数是对的**，所以个数检查抓不到；报错信息
         （`expected Session instance instead of Instance`）也不指向具体那一行。
         **这条是真机冒烟抓出来的**，所以固化成测试，别再让人戴头显来抓。
      2. 位置参数个数不足（漏参数）
      3. 结构体关键字字段名写错

    没装 pyopenxr（Windows 构建）时跳过 —— 那个模块本来就不该进 Windows 产物。
    """
    import ast
    try:
        import xr
    except ImportError:
        print("  ⏭ 没装 pyopenxr（非 Linux 环境），跳过")
        return

    backend = ROOT / "vlt" / "output" / "openxr_overlay.py"
    tree = ast.parse(backend.read_text(encoding="utf-8"))
    # 句柄类型 → 实参里应当出现的字样（只看 self.<attr>，局部变量名太自由、误报多）
    handles = {"Session": ("session", "_sess"), "Instance": ("instance",),
               "Swapchain": ("swapchain",), "ActionSet": ("action_set", "aset"),
               "Action": ("action", "act"), "Space": ("space",)}

    bad_handle, bad_arity, bad_field, calls = [], [], [], 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "xr"):
            continue
        cls_or_fn = getattr(xr, node.func.attr, None)
        if cls_or_fn is None:
            bad_field.append(f"L{node.lineno} xr.{node.func.attr} 不存在")
            continue
        calls += 1
        # —— 结构体构造：关键字字段名
        if node.keywords and hasattr(cls_or_fn, "_fields_"):
            allowed = set(dir(cls_or_fn()))
            for kw in node.keywords:
                if kw.arg and kw.arg not in allowed:
                    bad_field.append(f"L{node.lineno} xr.{node.func.attr}({kw.arg}=…) 不是有效字段")
            continue
        # —— 枚举：构造就是「传一个值」，**别按签名判** ——
        #    `inspect.signature` 对枚举类报的参数个数**随 Python 版本变**
        #    （3.11 报 1 个、3.14 报 2 个，因为 __new__ 的来源不同），
        #    按签名判会在某个版本上凭空报「参数不足」（实测在 3.14 上踩到）。
        if isinstance(cls_or_fn, type) and hasattr(cls_or_fn, "__members__"):
            continue
        # —— 函数调用：参数个数 + 句柄错配
        try:
            params = list(inspect.signature(cls_or_fn).parameters.values())
        except (TypeError, ValueError):
            continue
        need = len([q for q in params
                    if q.kind in (q.POSITIONAL_ONLY, q.POSITIONAL_OR_KEYWORD) and q.default is q.empty])
        if len(node.args) < need:
            bad_arity.append(f"L{node.lineno} xr.{node.func.attr} 传 {len(node.args)} 个，至少要 {need}")
        for i, arg in enumerate(node.args):
            if i >= len(params):
                break
            name = getattr(params[i].annotation, "__name__", "")
            hint = handles.get(name)
            src = ast.unparse(arg).lower()
            if hint and src.startswith("self.") and not any(h in src for h in hint):
                bad_handle.append(f"L{node.lineno} xr.{node.func.attr} 参数{i+1} 期望 {name}，"
                                  f"实参是 `{ast.unparse(arg)}`")

    assert not bad_handle, "句柄类型错配（参数个数对、类型错）：\n  " + "\n  ".join(bad_handle)
    assert not bad_arity, "位置参数个数不足：\n  " + "\n  ".join(bad_arity)
    assert not bad_field, "xr.* 名字/字段名无效：\n  " + "\n  ".join(bad_field)
    print(f"  后端 {calls} 处 xr.* 调用：句柄/个数/字段 全部正确 OK")


def test_extensions_per_backend_required():
    """★ 后端决定必需扩展清单：X11/XLIB 不该要求 `XR_MNDX_egl_enable`。

    MNDX 是 Wayland 的 EGL 绑定专有扩展；X11 走 `XR_KHR_opengl_enable` 的 XLIB 分支。
    """
    import types

    from vlt.output.openxr_overlay import EglGlContext, XlibGlxContext

    assert "XR_MNDX_egl_enable" in EglGlContext.REQUIRED_EXTENSIONS
    assert "XR_MNDX_egl_enable" not in XlibGlxContext.REQUIRED_EXTENSIONS
    assert "XR_KHR_opengl_enable" in XlibGlxContext.REQUIRED_EXTENSIONS

    class _E:
        def __init__(self, name: str) -> None:
            self.extension_name = name.encode()

    fake = types.ModuleType("xr")
    fake.enumerate_instance_extension_properties = lambda: [  # type: ignore[attr-defined]
        _E("XR_EXTX_overlay"), _E("XR_KHR_opengl_enable"), _E("XR_MNDX_egl_enable")]
    saved = sys.modules.get("xr")
    sys.modules["xr"] = fake
    try:
        got_x11 = OpenXrOverlay._extensions(XlibGlxContext.REQUIRED_EXTENSIONS)  # noqa: SLF001
        assert "XR_MNDX_egl_enable" not in got_x11, f"X11 不该要 MNDX：{got_x11}"
        assert "XR_KHR_opengl_enable" in got_x11, got_x11
        got_way = OpenXrOverlay._extensions(EglGlContext.REQUIRED_EXTENSIONS)    # noqa: SLF001
        assert "XR_MNDX_egl_enable" in got_way, f"Wayland 需要 MNDX：{got_way}"
    finally:
        if saved is None:
            sys.modules.pop("xr", None)
        else:
            sys.modules["xr"] = saved
    print("  后端必需扩展清单 OK（X11 不要 MNDX · Wayland 要）")


def test_glx_binding_struct_fields():
    """★ XLIB 绑定结构体：指针字段必须 cast 成 pyopenxr/PyOpenGL 声明的类型。

    不建真上下文（`__new__` 绕过 __init__ 塞假地址）—— 这条只钉「结构体构造」；
    真上下文在 `tests/test_overlay_glx.py` 里用 Xvfb+GLX 跑。
    pyopenxr 在 Windows 测试机上没装 → 跳过（该后端本来就是 Linux 独占）。
    """
    try:
        import xr
    except Exception:  # noqa: BLE001
        print("  SKIP: 没有 pyopenxr（Windows 测试机不装）")
        return

    import ctypes as _ct

    from vlt.output.openxr_overlay import XlibGlxContext

    ctx = XlibGlxContext.__new__(XlibGlxContext)
    ctx.display = 0xDEAD0000
    ctx.fbconfig = 0xDEAD0001
    ctx.visualid = 0x21
    ctx.pbuffer = 0xDEAD0002
    ctx.context = 0xDEAD0003
    b = ctx.binding()
    assert type(b) is xr.GraphicsBindingOpenGLXlibKHR, type(b)
    fields = dict(xr.GraphicsBindingOpenGLXlibKHR._fields_)
    assert _ct.cast(b.x_display, _ct.c_void_p).value == 0xDEAD0000
    assert b.visualid == 0x21
    assert _ct.cast(b.glx_fbconfig, _ct.c_void_p).value == 0xDEAD0001
    assert b.glx_drawable == 0xDEAD0002
    assert _ct.cast(b.glx_context, _ct.c_void_p).value == 0xDEAD0003
    # 类型必须真是声明的那几个（给错类型运行时会 TypeError）
    assert b.x_display.__class__ is fields["x_display"]
    print("  XLIB 绑定结构体字段 OK（含指针 cast 类型）")


def test_create_gl_context_selection():
    """★ 后端选择：Wayland 优先、失败回退 X11；`VLT_OVERLAY_GL` 可强制。"""
    import os

    import vlt.output.openxr_overlay as O

    class _Ok:
        def __init__(self, name: str, log: list[str]) -> None:
            self.name = name
            log.append(name)

    class _Fail:
        def __init__(self, name: str, log: list[str]) -> None:
            log.append(name)
            raise RuntimeError(f"{name} 建不起来（模拟）")

    saved_backends = dict(O._GL_BACKENDS)
    saved_env = {k: os.environ.get(k)
                 for k in ("WAYLAND_DISPLAY", "DISPLAY", "VLT_OVERLAY_GL")}
    log: list[str] = []
    try:
        O._GL_BACKENDS.clear()
        O._GL_BACKENDS.update(wayland=lambda: _Ok("wayland", log),
                              x11=lambda: _Ok("x11", log))
        os.environ["WAYLAND_DISPLAY"] = "wayland-1"
        os.environ["DISPLAY"] = ":0"
        os.environ.pop("VLT_OVERLAY_GL", None)
        assert O.create_gl_context().name == "wayland", f"两个都有时应先 Wayland：{log}"

        # Wayland 建不起来 → 回退 X11
        log.clear()
        O._GL_BACKENDS["wayland"] = lambda: _Fail("wayland", log)
        assert O.create_gl_context().name == "x11", f"Wayland 失败应回退 X11：{log}"

        # 只有 DISPLAY（Xorg 会话）→ 直接 X11，别去碰 Wayland
        log.clear()
        O._GL_BACKENDS["wayland"] = lambda: _Ok("wayland", log)
        os.environ.pop("WAYLAND_DISPLAY", None)
        assert O.create_gl_context().name == "x11", f"Xorg 会话应该直接走 X11：{log}"
        assert log == ["x11"], f"不该先试 Wayland：{log}"

        # 强制开关：只试指定那条
        log.clear()
        os.environ["WAYLAND_DISPLAY"] = "wayland-1"
        os.environ["VLT_OVERLAY_GL"] = "wayland"
        assert O.create_gl_context().name == "wayland", log
        assert log == ["wayland"], log

        # 强制的那条失败 → 直接抛（不回退，便于排查）
        log.clear()
        O._GL_BACKENDS["x11"] = lambda: _Fail("x11", log)
        os.environ["VLT_OVERLAY_GL"] = "x11"
        try:
            O.create_gl_context()
        except RuntimeError as exc:
            assert "x11" in str(exc), str(exc)
        else:
            raise AssertionError("强制 x11 且失败时应抛 RuntimeError")
        assert log == ["x11"], f"强制模式不该试别的后端：{log}"

        # 两条都失败（auto、环境里两个都看不到）→ 两条都试、错误信息里两边的都有
        log.clear()
        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ.pop("DISPLAY", None)
        os.environ.pop("VLT_OVERLAY_GL", None)
        O._GL_BACKENDS["wayland"] = lambda: _Fail("wayland", log)
        try:
            O.create_gl_context()
        except RuntimeError as exc:
            msg = str(exc)
            assert "wayland" in msg and "x11" in msg, msg
        else:
            raise AssertionError("全失败应抛 RuntimeError")
    finally:
        O._GL_BACKENDS.clear()
        O._GL_BACKENDS.update(saved_backends)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("  GL 后端选择 OK（Wayland 优先 · 回退 X11 · 强制开关）")
class _Rec:
    """一条假记录（够 pyopenxr 那种「构造器只装字段」的用法）。"""

    def __init__(self, *args, **kw):
        self.args = args
        self.__dict__.update(kw)


class _FakeXr(types.ModuleType):
    """够用的 pyopenxr 假模块：记调用，并模拟「attach 只能一次」这条规范。

    `bad_path` 里给的子串视为运行时**不认**的路径（`string_to_path` 抛），
    用来验证「某个锚点降级、别的锚点照常」。
    """

    def __init__(self, bad_path: str = ""):
        super().__init__("xr")
        self.calls: list[str] = []
        self.attached = False
        self.bad_path = bad_path
        self.action_names: set[str] = set()
        self.localized_names: set[str] = set()
        # 每个 interaction profile **最终生效**的那一组绑定（见 suggest_... 的替换语义）
        self.suggestions: dict[str, list] = {}
        self.ActionType = types.SimpleNamespace(POSE_INPUT="pose")
        self.SpaceLocationFlags = types.SimpleNamespace(POSITION_TRACKED_BIT=2)
        for name in ("ActionSetCreateInfo", "ActionCreateInfo", "Posef", "Quaternionf",
                     "Vector3f", "InteractionProfileSuggestedBinding", "ActionSuggestedBinding",
                     "SessionActionSetsAttachInfo", "ActionSpaceCreateInfo", "Time"):
            setattr(self, name, _Rec)

    def create_action_set(self, inst, info):
        self.calls.append("create_action_set")
        return _Rec(kind="set")

    def create_action(self, aset, info):
        # 与真 runtime 同款约束：**动作集内** name 与 localizedActionName 都必须唯一
        # （规范 input.adoc：duplicates … must return XR_ERROR_NAME_DUPLICATED /
        #  XR_ERROR_LOCALIZED_NAME_DUPLICATED）。实测踩过：10 个动作共用同一个
        # localized 名字 → 只有第一个建得出来，其余全失败、面板静默回退到 VIEW。
        if info.action_name in self.action_names:
            raise RuntimeError(f"NameDuplicatedError: {info.action_name}")
        if info.localized_action_name in self.localized_names:
            raise RuntimeError("LocalizedNameDuplicatedError: The localized name provided "
                               "was a duplicate of an already-existing resource.")
        self.action_names.add(info.action_name)
        self.localized_names.add(info.localized_action_name)
        self.calls.append(f"create_action:{info.action_name}")
        return _Rec(kind="action", name=info.action_name)

    def create_action_space(self, sess, info):
        self.calls.append("create_action_space")
        return _Rec(kind="space")

    def suggest_interaction_profile_bindings(self, inst, info):
        self.calls.append(f"suggest:{info.interaction_profile}")
        # ★ 规范（input.adoc）：同一个 interaction profile 再次调用会
        #   **丢弃并替换**上一次的全部建议绑定 —— 逐个动作各调一次的话，
        #   最后只剩最后一个动作的绑定（实测：左手就是这样被右手覆盖掉的）。
        self.suggestions[info.interaction_profile] = list(info.suggested_bindings)

    def bound_paths(self) -> dict[str, set[str]]:
        """替换语义算完之后**最终生效**的绑定：动作名 → {绑定路径}。"""
        out: dict[str, set[str]] = {}
        for binds in self.suggestions.values():
            for b in binds:
                out.setdefault(b.action.name, set()).add(b.binding)
        return out

    def attach_session_action_sets(self, sess, info):
        if self.attached:          # ← 规范要求：第二次必须报错（老实现就死在这）
            raise RuntimeError("ActionsetsAlreadyAttachedError: "
                               "The session already has attached action sets.")
        self.attached = True
        self.calls.append("attach")

    def string_to_path(self, inst, s):
        if self.bad_path and self.bad_path in s:
            raise RuntimeError(f"XR_ERROR_PATH_UNSUPPORTED: {s}")
        return s

    @staticmethod
    def wait_frame(sess):
        return _Rec(predicted_display_time=1234)

    @staticmethod
    def locate_space(space, ref, t):
        return _Rec(location_flags=2)          # 一律当作「已追踪」，好验 space 的选择


def _sess_in_fake_xr(fake: _FakeXr, extensions: list[str] | None = None):
    """起一个不碰真 XR 的会话对象（动作相关的方法只用 instance/session 句柄）。

    `extensions` 就是「本会话实际启用的扩展」——tracker 整族要不要建，看的就是它里面
    有没有 `TRACKER_EXT`。
    """
    saved = sys.modules.get("xr")
    sys.modules["xr"] = fake
    sess = XrOverlaySession(None, (64, 32))    # type: ignore[arg-type]
    sess.instance, sess.session, sess.ref_space = object(), object(), object()
    sess.view_space = object()
    sess.extensions = list(extensions or [])
    return sess, saved


def _restore_xr(saved) -> None:
    if saved is None:
        sys.modules.pop("xr", None)
    else:
        sys.modules["xr"] = saved


def test_anchor_actions_are_built_once_and_switchable():
    """★ 回归：**动作集只能 attach 一次**，切锚点不许再建动作/再 attach。

    真机故障（用户日志 `ActionsetsAlreadyAttachedError`）：老实现每换一次锚点就
    `create_action` + `attach_session_action_sets` 一遍，而 OpenXR 规范明写——

      * `xrAttachSessionActionSets`：called more than once → **must** return
        `XR_ERROR_ACTIONSETS_ALREADY_ATTACHED`；
      * `xrCreateAction` / `xrSuggestInteractionProfileBindings`：动作集 attach 之后就
        **immutable**，再调同样返回该错。

    于是「切左手」在真机上静默失败（异常被热重载的 except 吃掉），面板纹丝不动 ——
    表现为「没法把面板绑到左手上」。修法是**开局一次建全**（左右手 + 8 个 tracker role），
    attach 一次，之后切锚点只换用哪个 space。
    """
    fake = _FakeXr()
    sess, saved = _sess_in_fake_xr(fake, extensions=[TRACKER_EXT])
    try:
        sess.ensure_anchor("right_hand", 0)
        right = sess.anchor_space("right_hand", 0)
        assert right is not sess.view_space, "已追踪的右手锚点应当用动作空间"
        assert sess.anchor_tracked("right_hand", 0) is True
        assert fake.calls.count("attach") == 1, "首次应当 attach"

        # 切锚点：绝不能抛（真机上这里就是 ActionsetsAlreadyAttachedError 的位置）
        sess.ensure_anchor("left_hand", 0)
        sess.ensure_anchor("tracker", 3)
        sess.ensure_anchor("hmd", 0)
        sess.ensure_anchor("right_hand", 0)

        assert fake.calls.count("attach") == 1, f"attach 只能调用一次（规范）：{fake.calls}"
        n_actions = sum(1 for c in fake.calls if c.startswith("create_action:"))
        assert n_actions == 2 + len(TRACKER_ROLES), \
            f"动作应当开局一次建全（左右手 + {len(TRACKER_ROLES)} 个 tracker role）：{n_actions}"
        assert len(fake.action_names) == n_actions, "动作名要唯一"
        assert len(fake.localized_names) == n_actions, \
            "localizedActionName 也必须在动作集内唯一（实测：共用名字会让除第一个外的动作全建不出来）"

        # ★ 绑定必须**一次给全**：逐个动作各调一次的话，同一个 profile 上后一次会替换掉
        #   前一次 —— 左右手只能活一个（实测：修好右手之后左手被覆盖，面板跟着头走）。
        bound = fake.bound_paths()
        assert bound.get("pose_left_hand_0") == {"/user/hand/left/input/grip/pose"}, \
            f"左手的绑定被覆盖掉了（同一 profile 上只能调一次、要一次给全）：{bound}"
        assert bound.get("pose_right_hand_0") == {"/user/hand/right/input/grip/pose"}, \
            f"右手的绑定不对：{bound}"
        assert len(fake.suggestions[XrOverlaySession.TRACKER_PROFILE]) == len(TRACKER_ROLES), \
            "8 个 tracker role 的绑定应当在同一次调用里给全"
        first_attach = fake.calls.index("attach")
        last_action = max(i for i, c in enumerate(fake.calls) if c.startswith("create_action:"))
        assert last_action < first_attach, \
            f"attach 之后动作集就 immutable 了，建动作必须在 attach 之前：{fake.calls}"

        left = sess.anchor_space("left_hand", 0)
        assert left is not right and left is not sess.view_space, "左手要用自己的 space"
        assert sess.anchor_space("right_hand", 0) is right, "切回来要拿回原来那个 space"
        assert sess.anchor_space("hmd", 0) is sess.view_space, "hmd 用 VIEW 空间"
        assert sess.anchor_tracked("hmd", 0) is None, "hmd 不该有锚点动作"
    finally:
        _restore_xr(saved)
    print(f"  锚点动作只建一次（{2 + len(TRACKER_ROLES)} 个）+ attach 一次；"
          f"切锚点/切回都取各自的 space OK")


def test_unsupported_tracker_path_degrades_only_that_anchor():
    """运行时不支持 tracker 时：只有 tracker 锚点降级，左右手照常。

    「动作一次建全」把风险也一起放大了：老实现只建当前锚点，所以运行时认不认
    `/user/vive_tracker_htcx/role/...` 都不影响用左右手；改成开局全建之后，
    一个不认的路径若直接抛出去，整条手腕屏都会起不来。所以每个锚点单独 try。

    两种「不支持」都要兜住：
      a) 运行时压根没启用 `XR_HTCX_vive_tracker_interaction` → 整族**不去建**；
      b) 扩展启用了、路径还是解析不了 → 建第一个就失败，**整族跳过**（只留一行痕）。
    """
    for label, extensions, bad_path in (
            ("没启用 tracker 扩展", [], ""),
            ("扩展启用但路径用不了", [TRACKER_EXT], "vive_tracker_htcx")):
        fake = _FakeXr(bad_path=bad_path)
        sess, saved = _sess_in_fake_xr(fake, extensions=extensions)
        try:
            sess.ensure_anchor("left_hand", 0)          # 不该抛
            assert sess.anchor_space("left_hand", 0) is not sess.view_space, \
                f"{label}：左手必须照常可用"
            assert sess.anchor_space("right_hand", 0) is not sess.view_space, \
                f"{label}：右手必须照常可用"
            assert sess.anchor_space("tracker", 0) is sess.view_space, \
                f"{label}：tracker 应当退回 VIEW 空间"
            assert sess.anchor_tracked("tracker", 0) is None, f"{label}：tracker 不该有动作"
            assert not any(c.startswith("create_action:pose_tracker") for c in fake.calls), \
                f"{label}：应当整族跳过 tracker，别对着 8 个 role 各撞一次：{fake.calls}"
            assert fake.calls.count("attach") == 1, f"{label}：一个锚点不可用不影响 attach"
            assert fake.bound_paths().get("pose_left_hand_0") == {"/user/hand/left/input/grip/pose"}, \
                f"{label}：tracker 降级不该影响左右手的绑定"
        finally:
            _restore_xr(saved)
    print("  ★ tracker 不可用（没扩展 / 路径不认）时：只有该锚点降级到 VIEW，左右手绑定不受影响 OK")


# ---------------------------------------------------------------- 面板尺寸热重载（回归）


class _FakeXrSwap(types.ModuleType):
    """只覆盖 swapchain 那几条调用链的假 xr（`set_size` / 重建的回归用）。"""

    def __init__(self) -> None:
        super().__init__("xr")
        self.created: list[tuple[int, int]] = []
        self.destroyed: list[object] = []
        self.SwapchainCreateInfo = _Rec
        self.SwapchainImageOpenGLKHR = object()

    def enumerate_swapchain_formats(self, sess):  # noqa: ANN001
        return [0x8058]                           # GL_RGBA8

    def create_swapchain(self, sess, info):  # noqa: ANN001
        self.created.append((int(info.width), int(info.height)))
        return object()

    def destroy_swapchain(self, sc):  # noqa: ANN001
        self.destroyed.append(sc)

    def enumerate_swapchain_images(self, sc, cls):  # noqa: ANN001
        return [types.SimpleNamespace(image=7)]


def test_session_set_size_rebuilds_swapchain():
    """★ 回归（用户实测）：面板尺寸热重载必须**重建交换链**。

    430 → 440 一步就触发：新图比交换链大 → 上传越界（GL 0x501）+ image_rect 越界
    （SwapchainRectInvalid）→ 每帧提交失败；而自愈重建用的还是旧尺寸 → 232 次
    「已重建 swapchain」也不恢复。这里钉住 set_size 的交换链行为（同尺寸幂等）。
    """
    fake = _FakeXrSwap()
    saved = sys.modules.get("xr")
    sys.modules["xr"] = fake
    try:
        sess = XrOverlaySession(None, (1024, 430))      # type: ignore[arg-type]
        sess.session = object()
        sess._create_swapchain()                        # noqa: SLF001
        assert fake.created == [(1024, 430)], fake.created
        first = sess.swapchain

        sess.set_size((1024, 440))
        assert sess.size_px == (1024, 440)
        assert fake.created[-1] == (1024, 440), f"新尺寸没建出新交换链：{fake.created}"
        assert fake.destroyed == [first], "旧交换链没销毁"
        assert sess.textures == [7], "新交换链的纹理没接管"

        n = len(fake.created)
        sess.set_size((1024, 440))                      # 同尺寸：幂等
        assert len(fake.created) == n, "同尺寸不该重建"
    finally:
        _restore_xr(saved)
    print("  set_size：交换链跟随重建 / 旧链销毁 / 同尺寸幂等 OK")


def test_panel_size_reload_resizes_swapchain():
    """★ 回归（用户实测的入口）：热重载改了 `overlay.size_px` → 会话交换链跟随换。

    老实现只把新图重渲染一遍、交换链不动 —— 这正是「面板高拖过当前值就挂死」的入口。
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="vlt-xr-resize-"))
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text("overlay:\n  size_px: [1024, 430]\n", encoding="utf-8")

    ov = OpenXrOverlay(OverlayConfig(enabled=True, size_px=(1024, 430)),
                       config_path=cfg_path)

    class _Sess:
        def __init__(self) -> None:
            self.size_px = (1024, 430)
            self.sizes: list[tuple[int, int]] = []

        def set_size(self, s):  # noqa: ANN001
            self.sizes.append(tuple(s))
            self.size_px = tuple(s)

    ov._sess = _Sess()                                  # noqa: SLF001
    cfg_path.write_text("overlay:\n  size_px: [1024, 440]\n", encoding="utf-8")
    ov._reload_config_if_changed()                      # noqa: SLF001
    assert ov._sess.sizes == [(1024, 440)], ov._sess.sizes
    assert tuple(ov.cfg.size_px) == (1024, 440)
    print("  热重载改 size_px：会话交换链跟随重建 OK")


def test_escalation_ladder_climbs_to_session_and_instance():
    """★ 回归（用户实测）：自愈必须**逐级升高**（swapchain → session → instance）。

    老实现每做一级就把 `_stage` 放回 "none" → 永远停在第一级（日志实测：
    232 次重建 swapchain、0 次重建会话）。梯子的回落只在真的恢复上传时
    （帧循环 `_fails = 0` 处）——所以这里只验「动作后 stage 不回落、逐级往上」。
    """
    ov = OpenXrOverlay.__new__(OpenXrOverlay)
    calls: list[tuple] = []

    class _S:
        def rebuild_swapchain(self):  # noqa: ANN201
            calls.append(("swapchain",))

    ov._sess = _S()                     # noqa: SLF001
    ov._stage = "none"                  # noqa: SLF001
    ov._fails_in_stage = 3              # noqa: SLF001
    ov._rebuilds = 0                    # noqa: SLF001
    ov._reinits = 0                     # noqa: SLF001
    ov._teardown = lambda *, keep_gl: calls.append(("teardown", keep_gl))        # noqa: SLF001,E731
    ov._bring_up = lambda *, rebuild_gl: calls.append(("bring_up", rebuild_gl))  # noqa: SLF001,E731

    ov._escalate()                      # noqa: SLF001
    assert calls == [("swapchain",)], calls
    assert ov._stage == "swapchain"     # noqa: SLF001
    ov._fails_in_stage = 3              # noqa: SLF001
    ov._escalate()                      # noqa: SLF001
    assert calls[-2:] == [("teardown", True), ("bring_up", False)], calls
    assert ov._stage == "session"       # noqa: SLF001
    ov._fails_in_stage = 3              # noqa: SLF001
    ov._escalate()                      # noqa: SLF001
    assert calls[-2:] == [("teardown", True), ("bring_up", True)], calls
    assert ov._stage == "instance" and ov._reinits == 1   # noqa: SLF001
    print("  自愈阶梯：swapchain → session → instance 逐级升高 OK")


if __name__ == "__main__":
    print("test_openxr_overlay:")
    test_quaternion_matches_matrix_convention()
    test_quaternion_is_normalized()
    test_anchor_paths_mapping()
    test_swapchain_format_prefers_rgba8()
    test_layer_geometry_matches_windows_curvature()
    test_cylinder_pose_offset_rotates_with_panel()
    test_effective_curvature_degrades_without_extension()
    test_extensions_does_not_gamble_on_cylinder_when_unknown()
    test_submit_records_and_uses_enabled_extensions()
    test_should_rebuild_detects_changes()
    test_layer_alpha_flags_value()
    test_apply_overlay_alpha_scales_only_alpha()
    test_panels_have_transparent_margin_and_translucent_plate()
    test_backend_config_field()
    test_composition_layers_declare_alpha_flags()
    test_backend_xr_calls_are_wellformed()
    test_extensions_per_backend_required()
    test_glx_binding_struct_fields()
    test_create_gl_context_selection()
    test_anchor_actions_are_built_once_and_switchable()
    test_unsupported_tracker_path_degrades_only_that_anchor()
    test_session_set_size_rebuilds_swapchain()
    test_panel_size_reload_resizes_swapchain()
    test_escalation_ladder_climbs_to_session_and_instance()
    print("ALL PASSED")
