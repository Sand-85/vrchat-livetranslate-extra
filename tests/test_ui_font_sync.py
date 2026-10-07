"""界面字体常量「不许 import 期快照」守卫（issue #61）。

## 为什么要有这条结构判据

`vlt/ui_tk.py` 里的 `FONT*` 是**可变全局**：建 Tk root 之后 `apply_ui_font()` 把它们从
占位字族（`("Microsoft YaHei UI", 9)`，Windows 专有）改成**解析后**的字族
（Linux 上实测 `("Noto Sans CJK SC", 9)`）。

子模块若在 import 期写 `from .ui_tk import FONT_UI`，拿到的是**快照** —— 永远停在占位字族。
后果（Linux 实测）：该字族不存在 → Tk **静默回落**到另一个字体 → `_char_width_for()` 量出来的
「需要多宽」与运行时真正渲染的字体对不上 → 凡是按 `width=` 定宽的控件就裁字。
issue #61 抓到的正是它：ru 的「发送」按钮 `需 11 字符宽，只给了 10`。

⚠️ `tests/test_i18n.py` 的布局守卫**只在 runner 的字体环境下才抓得到**（同一份代码在本机
全绿）—— 所以这里补一条**与环境无关**的结构判据，把根因钉住，不依赖字体环境。

## 判据

模块级（`tree.body`）不许出现：

  1. `from .ui_tk import FONT…`            —— 名字快照
  2. `FONT… = ui_tk.FONT…`                 —— 属性快照（`vlt/gui.py` 例外，见下）

**函数体内的延迟 import 是允许的**：它跑在 `apply_ui_font()` 之后
（`vlt/gui_layout.py` / `vlt/gui_update.py` 里那几处就是这个形态，正确）。
正确写法见 `vlt/gui_desktop.py`：`from . import ui_tk`，用的时候写 `ui_tk.FONT_UI`。

`vlt/gui.py` 是唯一例外：它的模块级赋值由 `gui_layout.apply_ui_font()` 在
`apply_ui_font()` 之后**重新 setattr 同步**（那是拆分前 `_apply_ui_font` 的同步逻辑，
现在落在 `gui_layout`）；它同时是外部 `from vlt.gui import FONT_UI` 的落点。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: 允许模块级快照的模块（唯一：有 gui_layout.apply_ui_font() 的二次同步兜着）
ALLOWED = {"gui.py"}


def _violations() -> list[str]:
    """扫 `vlt/gui*.py` 的**模块级**语句，报出所有字体快照。"""
    out: list[str] = []
    for path in sorted((ROOT / "vlt").glob("gui*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:                      # 只看模块级；函数体内的延迟 import 安全
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("ui_tk"):
                bad = [a.name for a in node.names if a.name.startswith("FONT")]
                if bad:
                    out.append(f"{path.name}: from .ui_tk import {', '.join(bad)}")
            elif isinstance(node, ast.Assign):
                value = node.value
                if not (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                        and value.value.id == "ui_tk"):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.startswith("FONT"):
                        if path.name not in ALLOWED:
                            out.append(f"{path.name}: {target.id} = ui_tk.{value.attr}")
    return out


def test_no_module_level_font_snapshots() -> None:
    """模块级不许捕获 FONT 快照（函数体内的延迟 import 可以）。"""
    bad = _violations()
    assert not bad, (
        "有模块级字体快照 —— Linux 上会静默回落到别的字体，按 width= 定宽的控件会裁字"
        "（issue #61）。改成 `from . import ui_tk` + 用的时候写 `ui_tk.FONT_*`"
        "（照 vlt/gui_desktop.py 的写法）：\n  - " + "\n  - ".join(bad))
    print("  ✓ 无模块级字体快照（子模块全部运行时取 ui_tk.FONT_*）")


def test_send_button_width_uses_live_font() -> None:
    """★ 直接判据（issue #61 那一条）：ru 下「发送」按钮的 width 必须由**运行时**字体算出。

    两件事一起断言（**只建一个 Tk root** —— Tk 不喜欢一个进程里起两个根）：

      ① 前提自检：`apply_ui_font()` 确实把 `ui_tk.FONT_UI` 改成了**解析后**的字族
         （不再是占位字族 `"Microsoft YaHei UI"`）；
      ② 建控件时用的是 `_char_width_for(t("发送"), ui_tk.FONT_UI, 8)`，这里按同一条再算一遍做等值
         断言 —— 谁把它改回 import 快照，**在两种字族度量不同的机器上**（CI 就是）就会红。

    ⚠️ 本机（Arch）两种字族度量恰好相同（都量出 11），所以 ② 在**本机**区分不出来 ——
    真正与环境无关的那条判据是上面的结构守卫。②的价值在字体环境不同的机器上。

    ⚠️ 必须让窗口**在 ru 下建起来**：`TranslationGUI.__init__` 会按 `ui.lang` 重设语言，
    所以配置里得写 `lang: ru`（只造一份最小临时配置，绝不碰仓库里的真配置）。
    """
    import os
    import tempfile

    # 假 key 按仓库约定**拆开写**：整串字面量会被 scripts/check_no_secrets.py 拦下
    os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "uifontsync0123456789abcdef")

    import vlt.config as cfg_mod
    import vlt.gui as gui_mod
    import vlt.i18n as i18n
    from vlt import ui_tk

    tmp_cfg = Path(tempfile.mkdtemp(prefix="vlt-uifont-cfg-")) / "config.yaml"
    tmp_cfg.write_text("ui:\n  lang: ru\n", encoding="utf-8")
    saved_cfg = (cfg_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG)
    saved_lang = i18n.current_language()
    cfg_mod.DEFAULT_CONFIG = tmp_cfg
    gui_mod.DEFAULT_CONFIG = tmp_cfg
    gui = None
    try:
        gui = gui_mod.TranslationGUI()
        assert i18n.current_language() == "ru", \
            f"前提：界面语言应是 ru，实际 {i18n.current_language()!r}"

        # ① 前提自检：live 全局已解析（不是占位字族）
        fam = ui_tk.resolve_ui_family(gui._root)
        assert ui_tk.FONT_UI == (fam, 9), \
            f"前提：apply_ui_font 之后 ui_tk.FONT_UI 应是解析后的 ({fam!r}, 9)，实际 {ui_tk.FONT_UI!r}"

        # ② 直接判据
        txt = i18n.t("发送")
        assert txt != "发送", "前提：这条断言要的是**译文**，不是中文原文"
        want = ui_tk._char_width_for(txt, ui_tk.FONT_UI, 8)
        got = int(gui._send_btn.cget("width"))
        assert got == want, (f"「发送」按钮宽度不是用运行时字体算的：给了 {got}、"
                             f"按 ui_tk.FONT_UI（解析后 {fam!r}）应 {want}（issue #61）")
        print(f"  ✓ ru 的「发送」= {txt!r}：width={got} 与运行时字体（{fam!r}）算出来的一致")
    finally:
        if gui is not None:
            try:
                gui._on_close()
            except Exception:            # noqa: BLE001
                pass
        cfg_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG = saved_cfg
        i18n.set_language(saved_lang)


if __name__ == "__main__":
    print("test_ui_font_sync:")
    test_no_module_level_font_snapshots()
    test_send_button_width_uses_live_font()
    print("ALL PASSED")
