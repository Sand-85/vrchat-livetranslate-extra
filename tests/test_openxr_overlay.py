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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.output.openxr_overlay import (  # noqa: E402
    TRACKER_ROLES,
    anchor_paths,
    euler_to_quaternion,
    layer_geometry,
    pick_swapchain_format,
    should_rebuild,
)
from vlt.output.overlay import OverlayConfig  # noqa: E402

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


def test_layer_geometry_quad_and_cylinder():
    """curvature=0 → 平面层；>0 → 柱面层，且**弦长仍是 width_m**。"""
    q = layer_geometry(0.23, 1024 / 440, 0.0)
    assert q["kind"] == "quad"
    w, h = q["size"]
    assert abs(w - 0.23) < 1e-9 and abs(h - 0.23 / (1024 / 440)) < 1e-9

    c = layer_geometry(0.23, 1024 / 440, 0.15)
    assert c["kind"] == "cylinder"
    # 弧长 = 半径 × 圆心角，且弦长应等于 width_m（这里用弧长关系校验半径定义）
    assert abs(c["radius"] * c["central_angle"] - 0.23) < 1e-9, "半径/圆心角与宽度关系不对"
    assert 0.2 <= c["central_angle"] <= 1.5, "圆心角没夹在合理范围"

    # 极端 curvature 要夹住，不能出 NaN / 除零
    for cur in (1.0, 5.0):
        g = layer_geometry(0.23, 0.0, cur)      # aspect=0 也要能算
        assert g["kind"] == "cylinder" and g["radius"] > 0
    print("  层几何换算 OK（平面/柱面 + 夹取）")


def test_should_rebuild_detects_changes():
    """热重载判定：几何/锚点 → 重应用变换；字号/尺寸 → 必须重渲贴图。"""
    base = OverlayConfig()
    assert should_rebuild(base, OverlayConfig()) == (False, False), "没改却说改了"

    import dataclasses
    geo = dataclasses.replace(base, width_m=0.30)
    assert should_rebuild(base, geo) == (True, False), "改宽度没被识别成几何变化"

    anchor = dataclasses.replace(base, anchor="hmd", tracker_index=2)
    assert should_rebuild(base, anchor) == (True, False), "改锚点没被识别"

    render = dataclasses.replace(base, font_size=44)
    assert should_rebuild(base, render) == (False, True), "改字号没被识别成需要重渲"

    both = dataclasses.replace(base, font_size=44, pos=(1.0, 0.0, 0.0))
    assert should_rebuild(base, both) == (True, True)
    print("  热重载判定 OK")


def test_backend_config_field():
    """`overlay.backend` 的默认与解析（null 用来彻底关掉手腕屏）。"""
    assert OverlayConfig().backend == "auto"
    assert OverlayConfig.from_dict({}).backend == "auto"
    assert OverlayConfig.from_dict({"backend": "null"}).backend == "null"
    assert OverlayConfig.from_dict({"backend": None}).backend == "auto"
    print("  backend 配置字段 OK")


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


if __name__ == "__main__":
    print("test_openxr_overlay:")
    test_quaternion_matches_matrix_convention()
    test_quaternion_is_normalized()
    test_anchor_paths_mapping()
    test_swapchain_format_prefers_rgba8()
    test_layer_geometry_quad_and_cylinder()
    test_should_rebuild_detects_changes()
    test_backend_config_field()
    test_backend_xr_calls_are_wellformed()
    print("ALL PASSED")
