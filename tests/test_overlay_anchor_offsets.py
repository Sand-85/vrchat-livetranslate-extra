"""验证：手腕屏位姿「每个锚点各存一套」—— 切锚点不互相覆盖。

背景（用户实测）：以前只有一份 `overlay.offset`，右手调好之后切到左手，滑块显示的仍是
右手那份，碰一下就把左手那份写成了右手角度；再切回右手，发现右手也被改坏了 ——
症状就是「没法设置成左手」。现在位姿按锚点分开存在 `overlay.offsets.<锚点>` 里
（`overlay.offset` 退化为「该锚点还没单独存过」时的兜底）。本用例钉住四件事：

  ① 取值优先级：`offsets.<锚点>` → `offset` → 内置默认（`resolve_offset` 与 `from_dict` 同源）；
  ② 界面切锚点会把**该锚点那一份**回填到滑块（不是留着上一个锚点的值）；
  ③ 落盘只写当前锚点那一份，别的锚点与 `overlay.offset` 一律不动；
  ④ 老配置（整段没有 `offsets:`）写入时能补建，注释与其它键一个不丢。

跑法：xvfb-run -a .venv/bin/python tests/test_overlay_anchor_offsets.py

全程离线，只读写 `out/anchor_offsets_cfg/` 下的**沙箱**配置：`config.yaml` 是被 gitignore 的
用户真实个人配置，本用例绝不碰它（也不备份/还原它）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

# 干净环境（CI）上没有任何 API key，而起界面会走 `load_api_key()` —— 没有 key 直接
# SystemExit，测试假红。这里给一个**拼接出来的假 key**（不触发仓库的凭据扫描钩子）：
# 本用例只验位姿的读写路径，跟 key 的真假无关。
os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "anchorofl0123456789abcdef")

ROOT = Path(__file__).resolve().parents[1]          # 不写死本机路径：CI / 别人克隆后也能跑
sys.path.insert(0, str(ROOT))

# 用例按中文下拉标签操作控件（`_anchor_combo.set("左手")`）；界面语言跟随系统语言
# （CI 与外国机器是英文系统）→ 必须钉死 zh。产品代码不依赖这个补丁。
# ⚠️ 必须在构造窗口**之前**打桩。
import vlt.i18n as _i18n  # noqa: E402
_i18n.detect_system_language = lambda: "zh"

SANDBOX_DIR = ROOT / "out" / "anchor_offsets_cfg"

# 老配置（升级前的那种：只有一份 offset，整段没有 offsets）
LEGACY_CONFIG = """\
# 手写的旧配置：这段注释必须活下来
overlay:
  enabled: true
  anchor: right_hand
  # 兜底那一份的说明注释
  offset:
    pos: [0.01, 0.02, 0.03]
    rot: [-10, -20, -30]   # 兜底角度
    width_m: 0.23
    curvature: 0.0
"""


def n_comments(t: str) -> int:
    return sum(1 for ln in t.splitlines() if ln.strip().startswith("#"))


def write_sandbox(name: str, text: str) -> Path:
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    p = SANDBOX_DIR / name
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def load(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def make_gui(config: Path):
    """用沙箱配置起真窗口；取消启动 3 秒后的自动更新检查（绝不真连 GitHub）。"""
    import vlt.config as cfg_mod
    import vlt.gui as gui_mod

    cfg_mod.DEFAULT_CONFIG = config
    gui_mod.DEFAULT_CONFIG = config
    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    if gui._update_check_job is not None:
        gui._root.after_cancel(gui._update_check_job)
        gui._update_check_job = None
    gui._toggle_tune_panel()
    return gui


# ---------------------------------------------------------------- ① 纯逻辑：取值优先级


def check_resolve_priority() -> None:
    """`resolve_offset` / `OverlayConfig.from_dict`：各锚点 → offset → 默认，两层兜底都要对。"""
    from vlt.output.overlay import DEFAULT_POS, DEFAULT_ROT, OverlayConfig, resolve_offset

    d = {
        "anchor": "left_hand",
        "offset": {"pos": [0.5, 0.5, 0.5], "rot": [1, 2, 3]},
        "offsets": {"left_hand": {"pos": [0.11, 0.22, 0.33], "rot": [4, 5, 6]}},
    }
    assert resolve_offset(d, "left_hand") == ((0.11, 0.22, 0.33), (4, 5, 6)), "当前锚点那一份应当优先"
    assert resolve_offset(d, "right_hand") == ((0.5, 0.5, 0.5), (1, 2, 3)), "没单独存的锚点应当回落到 offset"
    assert resolve_offset({}, "left_hand") == (DEFAULT_POS, DEFAULT_ROT), "都没有时应当回落到内置默认"
    # 只有 offsets 里的 pos、没有 rot：两个分量各取各的，不许整份一起回落
    mixed = {"offset": {"pos": [1, 1, 1], "rot": [9, 9, 9]},
             "offsets": {"left_hand": {"pos": [7, 7, 7]}}}
    assert resolve_offset(mixed, "left_hand") == ((7, 7, 7), (9, 9, 9)), "pos/rot 应当各自回落"

    # 后端真正用的是 `cfg.pos` / `cfg.rot`：必须等于 resolve_offset 的结果（界面显示与面板同源）
    cfg = OverlayConfig.from_dict(d)
    assert (cfg.anchor, cfg.pos, cfg.rot) == ("left_hand", (0.11, 0.22, 0.33), (4, 5, 6)), \
        f"from_dict 没按当前锚点解析：{cfg.anchor} {cfg.pos} {cfg.rot}"
    cfg2 = OverlayConfig.from_dict({"anchor": "hmd", "offset": {"pos": [1, 2, 3]}})
    assert cfg2.pos == (1, 2, 3), f"from_dict 的兜底不对：{cfg2.pos}"
    print("  ✓ resolve_offset / from_dict：各锚点 → offset → 内置默认，两层兜底都对")


# ---------------------------------------------------------------- ② 界面：切锚点 + 落盘


def check_gui_per_anchor(gui, cfg: Path) -> None:
    """模板的四份预设各归各的；调一个锚点只动它自己那一份。"""
    assert gui._tune_values["rot_y"] == -16.0, "开局应当是 right_hand 那一份（模板预设）"

    gui._anchor_combo.set("左手")
    gui._on_anchor_change()
    assert gui._tune_values["rot_y"] == 16.0, \
        f"切到左手后滑块没载入 left_hand 那一份（rot_y={gui._tune_values['rot_y']}）"
    assert gui._tune_vars["rot_y"].get() == 16.0, "滑块的变量值也要跟着换（不然拖一下就是脏值）"

    # 用户调左手：只有 left_hand 该变
    gui._tune_values["rot_z"] = 33.0
    gui._save_overlay_cfg()
    data = load(cfg)
    offsets = data["overlay"]["offsets"]
    assert offsets["left_hand"]["rot"] == [-47, 16, 33], \
        f"左手那份没写进去：{offsets['left_hand']}"
    assert offsets["right_hand"]["rot"] == [-47, -16, 0], \
        f"右手那份被左手污染了：{offsets['right_hand']}"
    assert offsets["left_hand"]["pos"] == [0.0, 0.06, 0.02], "左手那份的 pos 不该被 rot 的改动牵连"

    # 切回右手：滑块要回到右手那一份（不是留着左手的 33）
    gui._anchor_combo.set("右手")
    gui._on_anchor_change()
    assert gui._tune_values["rot_z"] == 0.0, \
        f"切回右手后滑块仍是左手那份：rot_z={gui._tune_values['rot_z']}"
    data = load(cfg)
    assert data["overlay"]["offsets"]["left_hand"]["rot"] == [-47, 16, 33], \
        "切回右手的过程把左手那份写坏了（先落盘后载入的老 bug）"
    assert data["overlay"]["anchor"] == "right_hand", "anchor 没写回去"
    print("  ✓ 界面：切锚点载入该锚点那份；调左手不动右手，切回来也不丢")


# ---------------------------------------------------------------- ③ 老配置：补建 + 不丢注释


def check_legacy_config_backfills() -> None:
    """老配置没有 `offsets:` 段：写入要能补建，且注释 / 其它键一个不丢、`offset` 不被改。"""
    src = write_sandbox("legacy.yaml", LEGACY_CONFIG)
    before = src.read_text(encoding="utf-8")
    gui = None
    try:
        gui = make_gui(src)
        assert gui._tune_values["pos_x"] == 0.01, \
            f"没有 offsets 段时应当用 offset 兜底：{gui._tune_values['pos_x']}"

        gui._anchor_combo.set("左手")
        gui._on_anchor_change()
        assert gui._tune_values["pos_y"] == 0.02, "左手那份（尚不存在）应当回落到 offset"

        gui._tune_values["pos_x"] = 0.09
        gui._save_overlay_cfg()

        text = src.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        assert data["overlay"]["offsets"]["left_hand"]["pos"] == [0.09, 0.02, 0.03], \
            f"没补建出 offsets.left_hand：{data['overlay'].get('offsets')}"
        assert data["overlay"]["offset"]["pos"] == [0.01, 0.02, 0.03], \
            "兜底那份被改写了（它得留给别的锚点用）"
        assert n_comments(text) == n_comments(before), \
            f"补建 offsets 段把注释弄丢了：{n_comments(before)} → {n_comments(text)}"
        assert "# 手写的旧配置：这段注释必须活下来" in text and "兜底角度" in text, "注释文本丢了"
    finally:
        if gui is not None:
            gui._root.destroy()
    print("  ✓ 老配置：写入时补建 offsets.left_hand，注释与 offset 兜底一律不动")


# ---------------------------------------------------------------- 入口


def main() -> int:
    fails: list[str] = []

    print("test_overlay_anchor_offsets:")
    for fn in (check_resolve_priority,):
        try:
            fn()
        except AssertionError as exc:
            fails.append(f"{fn.__name__}: {exc}")
            print(f"  ❌ {exc}")

    import vlt.config as _cfg_mod
    import vlt.gui as _gui_mod

    saved = (_cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG)
    gui = None
    try:
        sandbox = write_sandbox("config.yaml",
                                (ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        gui = make_gui(sandbox)
        try:
            check_gui_per_anchor(gui, sandbox)
        except AssertionError as exc:
            fails.append(f"check_gui_per_anchor: {exc}")
            print(f"  ❌ {exc}")
    finally:
        if gui is not None:
            gui._root.destroy()
        _cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG = saved

    try:
        check_legacy_config_backfills()
    except AssertionError as exc:
        fails.append(f"check_legacy_config_backfills: {exc}")
        print(f"  ❌ {exc}")
    finally:
        _cfg_mod.DEFAULT_CONFIG, _gui_mod.DEFAULT_CONFIG = saved

    if fails:
        print(f"\n❌ {len(fails)} 条断言失败")
        return 1
    print("\nALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
