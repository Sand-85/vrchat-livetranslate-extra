#!/usr/bin/env python
"""Wayland 原生桌面叠加窗验收：纯逻辑 + 手写接口签名 + **真协议**（自拉 headless sway）。

三个层次，缺一不可：

1. **纯逻辑**（离线）：输出选择 / 边距换算 / 逻辑尺寸 / buffer 取整；
2. **手写接口签名**（离线）：与 wlr-protocols / wayland-protocols 的 XML 一字不差 ——
   数组式 marshalling 错一个槽就是「把 size 当 fd」那类事故，必须钉死；
3. **真协议**（嵌套 headless sway）：
   - 私有 `XDG_RUNTIME_DIR` + `WLR_BACKENDS=headless`，**绝不碰用户会话**；
   - sway 会忽略传入的 `WAYLAND_DISPLAY`、自行回落 `wayland-N` → 用「启动前后
     socket 差集」认新会话；
   - `grim` 截图验：面板画出来了 / 透明处透出背景 / 50% 混色正确 / move 生效 / 改尺寸生效；
   - `zwlr_virtual_pointer_v1` 注入指针：穿透时收不到点击、解锁后能拖动；
     两种拖动源都验：relative-pointer（sway 系）+ 本地坐标法（niri 系，
     `VLT_WAYLAND_NO_RELATIVE=1` 模拟「不发 relative_motion」的合成器）；
   - 跨屏拖动：拖到另一块屏松开后自动换面落位（双输出嵌套 sway）。

缺 sway / grim / 起不来时**跳过**第 3 层（不判红，但会在输出里说清原因）。

跑法：.venv/bin/python tests/test_wayland_window.py
"""
from __future__ import annotations

import ctypes
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.platform import wayland as W  # noqa: E402
from vlt.platform.wayland import (  # noqa: E402
    OutputInfo,
    buffer_dims,
    drag_target,
    layer_margins,
    logical_size,
    pick_output,
)

BACKDROP = (51, 102, 153)          # 嵌套 sway 的背景色（#336699）


# ---------------------------------------------------------------- 1. 纯逻辑


def test_pure_logic() -> None:
    outs = [OutputInfo(0, 0, 1920, 1080, 1, "A"),
            OutputInfo(1920, 0, 2560, 1440, 1, "B")]
    assert pick_output((100, 100), (400, 200), outs).name == "A"
    assert pick_output((2000, 100), (400, 200), outs).name == "B"
    # 跨两屏：相交面积 B 更大（B 侧 440px）→ 选 B
    assert pick_output((1800, 100), (400, 200), outs).name == "B"
    # 都不相交：按中心距离取最近
    assert pick_output((-500, -500), (100, 100), outs).name == "A"
    assert pick_output((5000, 0), (100, 100), outs).name == "B"
    # 空输出表：None（别崩）
    assert pick_output((0, 0), (10, 10), []) is None
    # 负坐标副屏（左侧）
    left = [OutputInfo(-1920, 0, 1920, 1080, 1, "L"), OutputInfo(0, 0, 1920, 1080, 1, "R")]
    assert pick_output((-1000, 100), (200, 100), left).name == "L"
    assert pick_output((-1900, 100), (400, 100), left).name == "L"

    # 边距 = 全局坐标 − 输出原点（可为负；负坐标副屏也按同一公式）
    assert layer_margins((100, 80), outs[0]) == (80, 100)
    assert layer_margins((-10, -20), left[0]) == (-20, 1910)

    # 逻辑尺寸 / buffer 取整（scale=2 时物理减半、奇数上取整）
    assert logical_size((1024, 440), 1) == (1024, 440)
    assert logical_size((1024, 440), 2) == (512, 220)
    assert logical_size((1025, 441), 2) == (513, 221)
    assert buffer_dims((1024, 440), 1) == (1024, 440)
    assert buffer_dims((1025, 441), 2) == (1026, 442)

    # 拖动公式（本地坐标法）：锚点 + 本地差。用用户实机抓包的真实数据验证：
    # 按下时本地 (635.63, 292.26)、松开前 (1696.40, -154.91) → (2158,930) 应移动到 (3219,483)
    assert drag_target((2158, 930), (635.63, 292.26), (1696.40, -154.91)) == (3219, 483)
    assert drag_target((100, 200), (10.0, 10.0), (10.0, 10.0)) == (100, 200)    # 没动
    assert drag_target((0, 0), (0.0, 0.0), (-5.2, 7.6)) == (-5, 8)              # 负向 + 四舍五入

    # 有效几何：xdg-output 优先（wlroots 的 geometry x/y 恒为 0，只有 logical_position
    # 能区分多屏）；没有 xdg-output 时回退 geometry/mode÷scale。
    _eg = W.LayerShellWindow._effective_geom
    assert _eg({"x": 0, "y": 0, "width": 1280, "height": 720, "scale": 1,
                "lx": 2000, "ly": 0, "lw": 1280, "lh": 720}) == (2000, 0, 1280, 720)
    assert _eg({"x": 60, "y": 70, "width": 2560, "height": 1440, "scale": 2,
                "lx": None, "ly": None, "lw": None, "lh": None}) == (60, 70, 1280, 720)
    assert _eg({"width": 0, "height": 0}) is None
    print("  纯逻辑：输出选择（命中/跨屏/最近/空表/负坐标）+ 边距 + 尺寸取整 + 拖动公式 + 有效几何 OK")


# ---------------------------------------------------------------- 2. 接口签名


def test_interface_signatures() -> None:
    # ⚠️ Windows / 没装 libwayland-client 的机器上加载必然失败（FileNotFoundError）——
    # 签名断言没有库数据可读，**明确跳过**而不是把测试判红（CI 的 Windows job 跑这份
    # 用例，Linux job 上库在、断言照常真跑）。与下面 `_have("sway")` 的跳过同款口径。
    try:
        W._load_lib()
    except (OSError, AttributeError) as exc:
        print(f"  ⚠️ 没有可用的 libwayland-client（{type(exc).__name__}: {exc}）→ 跳过签名断言")
        return
    shell = W.read_interface(W.iface_layer_shell)
    assert shell["name"] == "zwlr_layer_shell_v1", shell
    assert shell["version"] == W._LAYER_SHELL_VERSION, shell
    assert shell["methods"] == [("get_layer_surface", "no?ous"), ("destroy", "")], shell

    surf = W.read_interface(W.iface_layer_surface)
    assert surf["name"] == "zwlr_layer_surface_v1", surf
    assert surf["methods"] == [
        ("set_size", "uu"), ("set_anchor", "u"), ("set_exclusive_zone", "i"),
        ("set_margin", "iiii"), ("set_keyboard_interactivity", "u"),
        ("get_popup", "o"), ("ack_configure", "u"), ("destroy", ""),
        ("set_layer", "u"),
    ], surf
    assert surf["events"] == [("configure", "uuu"), ("closed", "")], surf

    mgr = W.read_interface(W.iface_rel_manager)
    assert mgr["methods"] == [("destroy", ""), ("get_relative_pointer", "no")], mgr
    rel = W.read_interface(W.iface_rel_pointer)
    assert rel["events"] == [("relative_motion", "uuffff")], rel

    xm = W.read_interface(W.iface_xdg_manager)
    assert xm["methods"] == [("destroy", ""), ("get_xdg_output", "no")], xm
    xo = W.read_interface(W.iface_xdg_output)
    assert xo["events"] == [("logical_position", "ii"), ("logical_size", "ii"),
                            ("done", ""), ("name", "s"), ("description", "s")], xo
    print("  手写接口签名：layer-shell（9 请求/2 事件）+ relative-pointer + xdg-output "
          "与 XML 一字不差 OK")


# ---------------------------------------------------------------- 3. 真协议（嵌套 sway）


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


class _NestedSway:
    """自拉一个私有 headless sway —— 与用户会话完全隔离。"""

    def __init__(self, outputs: int = 1) -> None:
        self.rt = tempfile.mkdtemp(prefix="vlt-wl-test-")
        os.chmod(self.rt, 0o700)
        self.conf = Path(self.rt) / "sway.conf"
        conf = "output * bg #336699 solid_color\n"
        if outputs > 1:
            # 跨屏用例：两块输出并排（放远一点，避免不同 wlroots 版本的默认尺寸把两块叠一起）
            conf += "output HEADLESS-1 position 0 0\n"
            conf += "output HEADLESS-2 position 2000 0\n"
        self.conf.write_text(conf, encoding="utf-8")
        before = set(os.listdir(self.rt))
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = self.rt
        env["WLR_BACKENDS"] = "headless"
        env["WLR_LIBINPUT_NO_DEVICES"] = "1"
        env["WLR_HEADLESS_OUTPUTS"] = str(max(1, int(outputs)))
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_SOCKET", None)
        self.log_path = Path(self.rt) / "sway.log"
        self.log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(["sway", "-c", str(self.conf), "-d"], env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        self.sock = None
        deadline = time.time() + 15.0
        while time.time() < deadline:
            new = set(os.listdir(self.rt)) - before
            cand = sorted(n for n in new if n.startswith("wayland-"))
            if cand:
                self.sock = cand[0]
                break
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)

    @property
    def ok(self) -> bool:
        return bool(self.sock)

    def env(self) -> dict:
        e = dict(os.environ)
        e["XDG_RUNTIME_DIR"] = self.rt
        e["WAYLAND_DISPLAY"] = self.sock
        e.pop("DISPLAY", None)
        return e

    def stop(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.log.close()
        except Exception:  # noqa: BLE001
            pass

    def tail(self, n: int = 12) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-n:])
        except Exception:  # noqa: BLE001
            return "(没读到日志)"


def _grim(env: dict) -> Image.Image:
    png = subprocess.run(["grim", "-"], env=env, check=True,
                         capture_output=True, timeout=15).stdout
    return Image.open(io.BytesIO(png)).convert("RGB")


def _px(img: Image.Image, xy: tuple[int, int]) -> tuple[int, int, int]:
    return tuple(img.getpixel((int(xy[0]), int(xy[1]))))       # type: ignore[return-value]


def _near(got: tuple, want: tuple, tol: int = 8) -> bool:
    return all(abs(int(a) - int(b)) <= tol for a, b in zip(got, want))


def _pump(win: W.LayerShellWindow, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        win.tick()
        time.sleep(0.01)


def _make_panel(size: tuple[int, int]) -> Image.Image:
    """上半不透明红、下半全透明：一图同时验「画出来了」和「真透明」。"""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, size[0], size[1] // 2], fill=(255, 0, 0, 255))
    return img


def _build_vp_ifaces(lib) -> tuple:  # noqa: ANN001
    """测试用：welr-virtual-pointer 的最小手写接口表（对照 XML）。"""
    seat = W._core_iface(lib, "wl_seat_interface")
    out = W._core_iface(lib, "wl_output_interface")
    vp = W._WlInterface()
    vp.name = b"zwlr_virtual_pointer_v1"
    vp.version = 2
    vp_methods = (W._WlMessage * 9)(
        W._mk_msg("motion", "uff", [0, 0, 0]),
        W._mk_msg("motion_absolute", "uuuuu", [0] * 5),
        W._mk_msg("button", "uuu", [0, 0, 0]),
        W._mk_msg("axis", "uuf", [0, 0, 0]),
        W._mk_msg("frame", "", []),
        W._mk_msg("axis_source", "u", [0]),
        W._mk_msg("axis_stop", "uu", [0, 0]),
        W._mk_msg("axis_discrete", "uufi", [0, 0, 0, 0]),
        W._mk_msg("destroy", "", []),
    )
    vp.method_count = 9
    vp.methods = vp_methods
    vp.event_count = 0
    vp.events = None
    mgr = W._WlInterface()
    mgr.name = b"zwlr_virtual_pointer_manager_v1"
    mgr.version = 2
    mgr_methods = (W._WlMessage * 3)(
        W._mk_msg("create_virtual_pointer", "?on", [seat, ctypes.addressof(vp)]),
        W._mk_msg("create_virtual_pointer_with_output", "?o?on",
                  [seat, out, ctypes.addressof(vp)]),
        W._mk_msg("destroy", "", []),
    )
    mgr.method_count = 3
    mgr.methods = mgr_methods
    mgr.event_count = 0
    mgr.events = None
    W._KEEP.extend([vp_methods, mgr_methods, vp, mgr])
    return mgr, vp


class _VirtualPointer:
    """往嵌套 sway 里注入指针事件（headless 没有真鼠标）。"""

    def __init__(self) -> None:
        self.lib = W._load_lib()
        self.display = self.lib.wl_display_connect(None)
        if not self.display:
            raise RuntimeError("虚拟指针：连不上嵌套 sway")
        self.globals: dict[str, tuple[int, int]] = {}
        self._keep: list = []

        def on_global(_d, _r, name, iface, version):  # noqa: ANN001
            key = iface.decode() if isinstance(iface, (bytes, bytearray)) else str(iface)
            self.globals[key] = (int(name), int(version))

        listener = W._RegistryListener(
            W._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
                  ctypes.c_uint32)(on_global),
            W._cb(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)(lambda *a: None))
        self._keep.append(listener)
        self.registry = W.marshal_request(
            self.lib, self.display, W._WL_DISPLAY_GET_REGISTRY, [("n", None)],
            interface=W._core_iface(self.lib, "wl_registry_interface"), version=1)
        self.lib.wl_proxy_add_listener(W._ptr(self.registry), ctypes.cast(
            ctypes.pointer(listener), ctypes.c_void_p), None)
        self.lib.wl_display_roundtrip(W._ptr(self.display))
        if "zwlr_virtual_pointer_manager_v1" not in self.globals:
            raise RuntimeError("嵌套 sway 没提供 zwlr_virtual_pointer_manager_v1")
        self.mgr_iface, self.vp_iface = _build_vp_ifaces(self.lib)
        name, ver = self.globals["zwlr_virtual_pointer_manager_v1"]
        self.manager = W.marshal_request(
            self.lib, self.registry, W._WL_REGISTRY_BIND,
            [("u", name), ("s", "zwlr_virtual_pointer_manager_v1"), ("u", min(ver, 2)),
             ("n", None)],
            interface=ctypes.addressof(self.mgr_iface), version=min(ver, 2))
        self.vp = W.marshal_request(self.lib, self.manager, 0,        # create_virtual_pointer
                                    [("o", None), ("n", None)],
                                    interface=ctypes.addressof(self.vp_iface), version=1)
        if not self.vp:
            raise RuntimeError("create_virtual_pointer 失败")
        self._flush()

    def _flush(self) -> None:
        self.lib.wl_display_flush(W._ptr(self.display))

    @staticmethod
    def _t() -> int:
        return int(time.monotonic() * 1000) & 0xFFFFFFFF

    def motion_abs(self, x: int, y: int, x_extent: int, y_extent: int) -> None:
        W.marshal_request(self.lib, self.vp, 1,
                          [("u", self._t()), ("u", x), ("u", y),
                           ("u", x_extent), ("u", y_extent)])
        self._flush()

    def motion(self, dx: int, dy: int) -> None:
        W.marshal_request(self.lib, self.vp, 0,
                          [("u", self._t()), ("f", dx * 256), ("f", dy * 256)])
        self._flush()

    def button(self, state: int) -> None:
        W.marshal_request(self.lib, self.vp, 2,
                          [("u", self._t()), ("u", 0x110), ("u", state)])
        self._flush()

    def frame(self) -> None:
        W.marshal_request(self.lib, self.vp, 4, [])
        self._flush()

    def sync(self) -> None:
        """roundtrip：确保 sway 已经处理完前面注入的请求。"""
        self.lib.wl_display_roundtrip(W._ptr(self.display))


def test_live_window() -> None:
    missing = [c for c in ("sway", "grim") if not _have(c)]
    if missing:
        print(f"  SKIP：缺 {missing}（真协议层跳过）")
        return
    sway = _NestedSway()
    if not sway.ok:
        print(f"  SKIP：嵌套 sway 没起来（{sway.tail(6)}）")
        sway.stop()
        return

    saved = {k: os.environ.get(k) for k in ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY")}
    win = None
    try:
        os.environ["XDG_RUNTIME_DIR"] = sway.rt
        os.environ["WAYLAND_DISPLAY"] = sway.sock
        env = sway.env()

        drag_ends: list[tuple[int, int]] = []
        size = (200, 96)
        win = W.LayerShellWindow(size=size, alpha=1.0, click_through=True,
                                 on_drag_end=lambda x, y: drag_ends.append((x, y)))
        assert win.available, "原生窗建不起来（看上面的 [desktop:wayland] 日志）"
        outs = win._output_infos()
        assert outs, "嵌套 sway 没有输出"
        out = outs[0]

        pos = (60, 40)
        win.move(*pos)
        win.set_panel(_make_panel(size))
        _pump(win, 0.6)

        # ① 面板画出来了（不透明红）
        shot = _grim(env)
        got = _px(shot, (pos[0] + 100, pos[1] + 20))
        assert _near(got, (255, 0, 0), 6), f"面板没画出来？{got}"

        # ② 下半透明：透出合成器背景
        got = _px(shot, (pos[0] + 100, pos[1] + 80))
        assert _near(got, BACKDROP, 6), f"透明区没透出背景：{got}"

        # ③ 整层 alpha=0.5：红与背景按 50% 混合
        win.set_alpha(0.5)
        _pump(win, 0.5)
        shot = _grim(env)
        got = _px(shot, (pos[0] + 100, pos[1] + 20))
        want = (round(255 * 0.5 + BACKDROP[0] * 0.5),
                round(BACKDROP[1] * 0.5), round(BACKDROP[2] * 0.5))
        assert _near(got, want, 14), f"50% 混色不对：{got} vs {want}"
        win.set_alpha(1.0)
        _pump(win, 0.3)

        # ④ move 生效：新位置有面板、旧位置回到背景
        win.move(300, 120)
        _pump(win, 0.5)
        shot = _grim(env)
        got_new = _px(shot, (300 + 100, 120 + 20))
        got_old = _px(shot, (pos[0] + 100, pos[1] + 20))
        assert _near(got_new, (255, 0, 0), 6), f"move 后新位置没有面板：{got_new}"
        assert _near(got_old, BACKDROP, 6), f"move 后旧位置没清掉：{got_old}"

        # ⑤ set_size 生效
        big = (240, 120)
        win.set_size(big)
        win.set_panel(_make_panel(big))
        _pump(win, 0.6)
        shot = _grim(env)
        got_top = _px(shot, (300 + 120, 120 + 20))
        got_bottom = _px(shot, (300 + 120, 120 + 100))
        assert _near(got_top, (255, 0, 0), 6), f"改尺寸后上半没面板：{got_top}"
        assert _near(got_bottom, BACKDROP, 6), f"改尺寸后下半没透明：{got_bottom}"

        # ⑥ 穿透：默认 click_through=True → 点不到（收不到按键）
        vp = _VirtualPointer()
        win.set_draggable(False)
        _pump(win, 0.2)
        vp.motion_abs(300 + 120, 120 + 20, out.width, out.height)
        vp.frame()
        vp.button(1)
        vp.button(0)
        vp.frame()
        vp.sync()
        _pump(win, 0.3)
        assert win._buttons_seen == 0, f"穿透开着还收到了点击：{win._buttons_seen}"

        # ⑦ 解锁拖动：收得到点击 + relative-pointer 位移真的移动面板
        win.set_draggable(True)
        _pump(win, 0.2)
        before = win.position
        vp.motion_abs(before[0] + 120, before[1] + 20, out.width, out.height)
        vp.frame()
        vp.button(1)
        vp.frame()
        vp.sync()
        _pump(win, 0.3)
        assert win._buttons_seen >= 1, "解锁后点击没到面板"
        vp.motion(80, 40)
        vp.frame()
        vp.sync()
        _pump(win, 0.3)
        vp.button(0)
        vp.frame()
        vp.sync()
        _pump(win, 0.3)
        assert drag_ends, "拖动结束回调没触发"
        end = drag_ends[-1]
        assert abs(end[0] - (before[0] + 80)) <= 4 and abs(end[1] - (before[1] + 40)) <= 4, \
            f"拖动位移不对：落点 {end}，起点 {before}（期望 +80,+40）"

        # ⑧ 本地坐标法（模拟 niri：niri 全程不发 relative_motion，只发 wl_pointer.motion）。
        # 用 VLT_WAYLAND_NO_RELATIVE=1 让窗口不建 relative-pointer 对象，强制走本地坐标差。
        win.close()
        os.environ["VLT_WAYLAND_NO_RELATIVE"] = "1"
        try:
            drag_ends2: list[tuple[int, int]] = []
            win = W.LayerShellWindow(size=size, alpha=1.0, click_through=True,
                                     on_drag_end=lambda x, y: drag_ends2.append((x, y)))
            assert win.available, "本地坐标法：窗口建不起来"
            win.move(300, 120)
            win.set_panel(_make_panel(size))
            win.set_draggable(True)
            _pump(win, 0.3)
            start2 = win.position
            vp.motion_abs(start2[0] + 100, start2[1] + 20, out.width, out.height)
            vp.frame()
            vp.sync()
            _pump(win, 0.2)
            vp.button(1)
            vp.frame()
            vp.sync()
            _pump(win, 0.15)                      # 等过 80ms 观察窗（第一条 motion 才设锚点）
            vp.motion(30, 0)
            vp.frame()
            vp.sync()
            _pump(win, 0.1)
            vp.motion(30, 25)
            vp.frame()
            vp.sync()
            _pump(win, 0.2)
            vp.button(0)
            vp.frame()
            vp.sync()
            _pump(win, 0.2)
            assert drag_ends2, "本地坐标法：拖动结束回调没触发"
            end2 = drag_ends2[-1]
            assert abs(end2[0] - (start2[0] + 30)) <= 4 \
                and abs(end2[1] - (start2[1] + 25)) <= 4, \
                f"本地坐标法位移不对：落点 {end2}，起点 {start2}（期望 +30,+25）"
        finally:
            os.environ.pop("VLT_WAYLAND_NO_RELATIVE", None)
        print(f"  真协议：画图/透明/50% 混色/move/改尺寸/穿透/拖动 OK"
              f"（相对指针 落点 {end}；本地坐标法 落点 {drag_ends2[-1]}）")
    finally:
        if win is not None:
            try:
                win.close()
                win.close()                     # 幂等
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠️ 关窗出错：{type(exc).__name__}: {exc}")
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        sway.stop()


def test_cross_output_drag() -> None:
    """跨屏拖动：拖动中面板夹在本屏边缘（中途换面会打断指针 grab）→ **松手立即落到
    指针所在的那块屏**（在松开分支里补一次 move()，走安全的重建面路径）。

    用双输出嵌套 sway（`WLR_HEADLESS_OUTPUTS=2`）验证：输出切换 + 落点在新屏上 + 旧屏干净。
    """
    missing = [c for c in ("sway", "grim") if not _have(c)]
    if missing:
        print(f"  SKIP：缺 {missing}（跨屏用例跳过）")
        return
    sway = _NestedSway(outputs=2)
    if not sway.ok:
        print(f"  SKIP：双输出嵌套 sway 没起来（{sway.tail(6)}）")
        sway.stop()
        return

    saved = {k: os.environ.get(k) for k in ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY")}
    win = None
    try:
        os.environ["XDG_RUNTIME_DIR"] = sway.rt
        os.environ["WAYLAND_DISPLAY"] = sway.sock
        env = sway.env()

        drag_ends: list[tuple[int, int]] = []
        size = (200, 96)
        win = W.LayerShellWindow(size=size, alpha=1.0, click_through=True,
                                 on_drag_end=lambda x, y: drag_ends.append((x, y)))
        assert win.available, "跨屏用例：窗口建不起来"
        outs = win._output_infos()
        if len(outs) < 2:
            print(f"  SKIP：嵌套 sway 只给了 {len(outs)} 块输出")
            return
        a, b = outs[0], outs[-1]
        win.move(a.x + 50, a.y + 40)
        win.set_panel(_make_panel(size))
        win.set_draggable(True)
        _pump(win, 0.5)
        g_before = win._output_global
        assert win._output is not None and win._output.x == a.x, (win._output, a)

        vp = _VirtualPointer()
        xs0 = min(o.x for o in outs)
        ys0 = min(o.y for o in outs)
        span_w = max(o.x + o.width for o in outs) - xs0
        span_h = max(o.y + o.height for o in outs) - ys0
        press = (a.x + 50 + 100, a.y + 40 + 20)              # 面板中心
        target = (b.x + 300, b.y + 200)                      # 另一块屏上
        vp.motion_abs(press[0] - xs0, press[1] - ys0, span_w, span_h)
        vp.frame()
        vp.sync()
        _pump(win, 0.2)
        vp.button(1)
        vp.frame()
        vp.sync()
        _pump(win, 0.2)
        # 一次相对位移直接把指针带到另一块屏（拖到被 grab 的面上的相对事件不会因出屏丢）
        vp.motion(target[0] - press[0], target[1] - press[1])
        vp.frame()
        vp.sync()
        _pump(win, 0.4)
        vp.button(0)
        vp.frame()
        vp.sync()
        _pump(win, 0.6)                                       # 松手 → 补 move → 换面 + 等 configure

        assert drag_ends, "跨屏拖动：结束回调没触发"
        assert win._output_global != g_before, "跨屏松开后没有换到另一块屏"
        assert win._output is not None and win._output.x == b.x, (win._output, b)
        pos = win.position
        assert b.x - 8 <= pos[0] <= b.x + b.width + 8, (pos, b)

        shot = _grim(env)
        got = _px(shot, (pos[0] + 100, pos[1] + 20))
        assert _near(got, (255, 0, 0), 8), f"换面后面板没画在新屏上：{got}"
        got_old = _px(shot, (a.x + 50 + 100, a.y + 40 + 20))
        assert _near(got_old, BACKDROP, 8), f"旧屏位置没有清掉：{got_old}"
        print(f"  跨屏拖动：松手从屏 A 落到屏 B（落点 {pos}，"
              f"输出切换 {g_before}→{win._output_global}）OK")
    finally:
        if win is not None:
            try:
                win.close()
            except Exception:  # noqa: BLE001
                pass
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        sway.stop()


def main() -> int:
    tests = [test_pure_logic, test_interface_signatures, test_live_window,
             test_cross_output_drag]
    print("test_wayland_window:")
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
