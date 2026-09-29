"""验证：手腕屏微调面板的旋转滑块范围是整圈 ±180°（issue #6）。

背景：`rot_x / rot_y / rot_z` 三个滑块原本被卡在 ±90°，而欧拉角三轴的定义域本就是
整圈 ±180° —— 面板朝上/朝下超过半圈、反戴侧戴、tracker/hmd 挂点初始就要转半圈以上，
这些可达姿态在界面上根本拖不到。更糟的是 `config.yaml` 里的 `rot` 本来**没有**范围校验
（`OverlayConfig.from_dict` 原样读取），手写 180 是好的，可只要在界面上碰一下滑块，
就会按滑块值回写，把 180 夹回 90 —— 用户手写的好配置被界面改坏了。

跑法：.venv/Scripts/python.exe tests/test_tune_rot_range.py

全程离线。只读写 `out/rot_range_cfg/` 下的**沙箱**配置：`config.yaml` 是被 gitignore 的
用户真实个人配置，本用例绝不碰它（也不备份/还原它）。
"""
from __future__ import annotations

import os
import re
import sys
import tkinter as tk
from pathlib import Path

import yaml

# 干净环境（CI）上没有任何 API key，而起界面会走 `load_api_key()` —— 没有 key 直接
# SystemExit，测试假红。这里给一个**拼接出来的假 key**（不触发仓库的凭据扫描钩子）：
# 本用例只验滑块范围，跟 key 的真假无关。
os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "rotrange0123456789abcdef")

ROOT = Path(__file__).resolve().parents[1]          # 不写死本机路径：CI / 别人克隆后也能跑
sys.path.insert(0, str(ROOT))

# 这个用例按**创建顺序**认第 4/5/6 个滑块是 rot_x/rot_y/rot_z；标签宽度按当前语言最长
# 的那条算，界面语言跟随系统语言（CI 与外国机器是英文系统）→ 必须钉死 zh，否则同一份
# 代码在不同机器上控件树排布可能不同。产品代码不依赖这个补丁。
# ⚠️ 必须在构造窗口**之前**打桩。
import vlt.i18n as _i18n  # noqa: E402
_i18n.detect_system_language = lambda: "zh"

SANDBOX_DIR = ROOT / "out" / "rot_range_cfg"
SANDBOX = SANDBOX_DIR / "config.yaml"

# 一组**同时超出旧 ±90° 范围三个方向**的值：正端越界（180）、负端越界（-135）、
# 以及一个旧范围内但会被夹到端点的值（95）。旧滑块范围下它们分别会变成 90 / -90 / 90。
WANT_ROT = [180, -135, 95]

SPEC_KEYS = ["pos_x", "pos_y", "pos_z", "rot_x", "rot_y", "rot_z", "width_m",
             "curvature", "alpha", "font_size", "source_font_size", "panel_h"]
ROT_IDX = (3, 4, 5)                                 # rot_x / rot_y / rot_z 在 specs 里的位置


# ---------------------------------------------------------------- 沙箱配置


def make_sandbox() -> None:
    """把模板复制成沙箱配置，并把 `rot: [...]` 改成一组越界值。

    用行级替换而不是整文件重写：沙箱要跟用户手写配置一样**保留注释**，
    这样后面「回写不丢值」那条才验的是真实场景。
    """
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    want = "[" + ", ".join(str(v) for v in WANT_ROT) + "]"
    new, n = re.subn(r"(?m)^(\s*rot:\s*)\[[^\]]*\]", rf"\g<1>{want}", text)
    assert n == 1, f"模板里应有且只有 1 行 `rot: [...]`，实际命中 {n} 处"
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    # .gitattributes 规定源码 LF：显式传 newline，别让 Windows 把整份配置写成 CRLF
    SANDBOX.write_text(new, encoding="utf-8", newline="\n")
    got = yaml.safe_load(SANDBOX.read_text(encoding="utf-8"))["overlay"]["offset"]["rot"]
    assert got == WANT_ROT, f"沙箱配置里的 rot 没写对：{got!r}（期望 {WANT_ROT}）"
    print(f"沙箱配置 → {SANDBOX}（rot: {got}）")


def read_sandbox_rot() -> list:
    data = yaml.safe_load(SANDBOX.read_text(encoding="utf-8"))
    return list(data["overlay"]["offset"]["rot"])


def collect_scales(win) -> list:
    """按创建顺序递归收集一棵控件树里的 tk.Scale（= specs 顺序）。"""
    out = []
    for w in win.winfo_children():
        if isinstance(w, tk.Scale):
            out.append(w)
        out.extend(collect_scales(w))
    return out


# ---------------------------------------------------------------- 三条用例


def check_rot_slider_range(scales: list) -> None:
    """① 旋转三轴滑块的定义域必须是整圈 ±180°；步长与位置滑块不许被顺手改动。"""
    assert len(scales) == len(SPEC_KEYS), \
        f"微调面板应有 {len(SPEC_KEYS)} 个滑块，实际 {len(scales)} 个 —— specs 顺序变了？"
    for i in ROT_IDX:
        lo, hi = float(scales[i].cget("from")), float(scales[i].cget("to"))
        assert (lo, hi) == (-180.0, 180.0), \
            f"{SPEC_KEYS[i]} 滑块范围是 {lo:g} ~ {hi:g}，期望 -180 ~ 180（issue #6）"
        assert float(scales[i].cget("resolution")) == 1.0, \
            f"{SPEC_KEYS[i]} 的步长被改动了：{scales[i].cget('resolution')}"
    # 位置滑块不在本次改动面里：范围仍是 ±0.30（顺手改到别的值也得红）
    for i in (0, 1, 2):
        lo, hi = float(scales[i].cget("from")), float(scales[i].cget("to"))
        assert (lo, hi) == (-0.30, 0.30), f"{SPEC_KEYS[i]} 滑块范围被误改：{lo:g} ~ {hi:g}"
    print(f"  ✓ rot_x/rot_y/rot_z 三个滑块范围均为 −180 ~ 180°，步长仍 1.0；"
          f"位置滑块未被动过")


def check_out_of_range_not_clamped(scales: list) -> None:
    """② 配置里的越界角度不许被 Tk 在建控件时夹到端点。

    旧范围下 Tk 会在构造 Scale 时就把超范围的变量夹到端点，三个值分别变成
    90 / -90 / 90 —— 用户手写的 180° 一进界面就没了。
    """
    got = [scales[i].get() for i in ROT_IDX]
    assert got == [float(v) for v in WANT_ROT], \
        f"滑块显示的角度被夹了：{got}（期望 {WANT_ROT}）"
    print(f"  ✓ 越界值没被夹：滑块停在 {got}（旧范围下会是 [90.0, -90.0, 90.0]）")


def fire_scale_command(gui, scale) -> None:
    """按 Tk 的方式触发滑块的 `-command` —— 也就是「用户拖动一下」时 Tk 真正干的事：
    把滑块的**当前值**喂给回调，回调再写 `_tune_values`。

    不用合成鼠标事件：拖到哪个像素换算成什么值受主题/DPI 影响，实测不稳。
    """
    gui._root.tk.call(scale.cget("command"), str(scale.get()))


def check_round_trip_keeps_rot(gui, scales: list) -> None:
    """③ 回写路径不许把 rot 夹成 ±90 写进配置 —— 这正是 issue 里最坑的一条。

    分两段：
    a) 拖**别的**滑块（位置）→ 200ms 防抖落盘：rot 不该被牵连；
    b) 真碰一下 rot 滑块本身：Tk 会把当前值喂给 `-command`，回调据此回写配置。
       旧范围下 (b) 就是 180 → 90 的那一下（用户手写的好配置被界面改坏）。
    """
    gui._tune_values["pos_x"] = -0.075
    gui._save_overlay_cfg()
    rot = read_sandbox_rot()
    assert rot == WANT_ROT, f"改 pos_x 落盘把 rot 改了：{rot}（期望 {WANT_ROT}）"
    pos = yaml.safe_load(SANDBOX.read_text(encoding="utf-8"))["overlay"]["offset"]["pos"]
    assert pos[0] == -0.075, f"pos_x 没写进去：{pos!r}（回写这条路径本身失效了？）"
    print(f"  ✓ 拖位置滑块落盘后 rot 仍是 {rot}")

    for i in ROT_IDX:
        fire_scale_command(gui, scales[i])
    got_vals = [gui._tune_values[k] for k in ("rot_x", "rot_y", "rot_z")]
    assert got_vals == [float(v) for v in WANT_ROT], \
        f"碰了一下 rot 滑块后内存里的角度被夹了：{got_vals}（期望 {WANT_ROT}）"
    gui._save_overlay_cfg()
    rot = read_sandbox_rot()
    assert rot == WANT_ROT, (f"碰过 rot 滑块后配置被夹成 {rot}（期望 {WANT_ROT}）"
                             f"—— issue #6 的回写夹值")
    print(f"  ✓ 碰过 rot 滑块再落盘，配置里 rot 仍是 {rot}"
          f"（旧范围下三个轴都会被夹到端点，变成 [90, 90, -90]）")


# ---------------------------------------------------------------- 入口


def main() -> int:
    import vlt.config as _cfg_mod
    import vlt.gui as _gui_mod

    make_sandbox()
    # 两个模块的常量都得指到沙箱：config.load_config 用前者，gui._save_overlay_cfg 用后者
    saved_paths = (_cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG)
    _cfg_mod.DEFAULT_CONFIG = SANDBOX
    _gui_mod.DEFAULT_CONFIG = SANDBOX

    from vlt.gui import TranslationGUI

    fails: list[str] = []
    gui = None
    try:
        gui = TranslationGUI()
        if gui._update_check_job is not None:      # 启动 3 秒后会自动查更新：绝不真连 GitHub
            gui._root.after_cancel(gui._update_check_job)
            gui._update_check_job = None
        gui._toggle_tune_panel()

        scales = collect_scales(gui._tune_body)
        print("test_tune_rot_range:")
        for fn in (lambda: check_rot_slider_range(scales),
                   lambda: check_out_of_range_not_clamped(scales),
                   lambda: check_round_trip_keeps_rot(gui, scales)):
            try:
                fn()
            except AssertionError as exc:
                fails.append(str(exc))
                print(f"  ❌ {exc}")
    finally:
        if gui is not None:
            gui._root.destroy()
        _cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG = saved_paths
        # 沙箱文件留在 out/ 下即可（已 gitignore）；用户的 config.yaml 全程没被碰过

    if fails:
        print(f"\n❌ {len(fails)} 条断言失败")
        return 1
    print("\nALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
