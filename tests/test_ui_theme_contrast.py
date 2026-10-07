"""深色主题的对比度守卫：钉住「白底白字」这一类坑，别再犯。

## 为什么要这个文件

踩过的两类实况（都是用户看出来的，不是测试看出来的）：

1. **输入框白底白字**：`apply_theme()` 一度用 `style.configure(".", foreground=TEXT)`
   把近白前景铺给所有 ttk 控件，但只有显式配过 `fieldbackground` 的类才有深色输入区 ——
   于是 `ttk.Spinbox`（麦克风代理的两个缓冲框）成了「白底 + 近白字」，内容完全看不见。
   实测当时 `ttk::style lookup TEntry -fieldbackground` 是**空串**（回落 clam 的白色默认底）。

2. **Tk 自带文件对话框白底白字**：Tk 的文件列表（C 实现的 `::tk::IconList`）把每条文件名的
   canvas item `-fill` 取成**根样式的前景**，而它那块 canvas 的底色是**写死的 `#ffffff`**
   （无选项可改、`*Canvas.background` 也够不着）。所以全局近白前景 = 文件名看不见。

结论（本文件钉住的不变量）：
- **根样式 `.` 里不许出现「在白底上看不清」的前景** —— 因为 Tk 的对话框会把它画在白底上；
- 我们**自己用到的** ttk 输入类必须显式配深色输入区，且与自己的前景对比度达标。

需要 Tk（无显示环境会跳过，与 tests/test_gui_rows.py 同口径）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: 最低对比度（WCAG 常规文本下限 4.5 对深色 UI 偏严，取 3.0 = 大字号/图形下限）。
MIN_RATIO = 3.0

#: 我们自己代码里真正用到的 ttk 输入类（加新输入控件时这里要一起加）。
INPUT_CLASSES = ("TEntry", "TSpinbox", "TCombobox")


# ---------------------------------------------------------------- 工具


def _rgb(color: str) -> tuple[int, int, int]:
    """#rrggbb → RGB 三元组。**具名颜色（black 等）走 Tk 解析**（见 `contrast`）。"""
    c = str(color).lstrip("#")
    if len(c) != 6:
        raise ValueError(f"不是 #rrggbb：{color!r}")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 相对亮度。"""
    out = []
    for v in rgb:
        s = v / 255.0
        out.append(s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4)
    r, g, b = out
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _as_rgb(root, color: str) -> tuple[int, int, int]:
    """任意 Tk 颜色规格（`#rrggbb` / 具名色）→ RGB。具名色交给 Tk 自己解析。"""
    if str(color).startswith("#"):
        return _rgb(color)
    r16, g16, b16 = (int(v) for v in root.tk.splitlist(root.winfo_rgb(color)))
    return r16 >> 8, g16 >> 8, b16 >> 8


def contrast(root, a: str, b: str) -> float:
    """两色对比度（1.0 ~ 21.0）。具名颜色也能算（经 Tk 解析）。"""
    la, lb = _luminance(_as_rgb(root, a)), _luminance(_as_rgb(root, b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _root_or_skip():
    """建一个根 + 应用主题；无显示环境返回 None（跳过，不算失败）。"""
    import tkinter as tk

    from vlt.ui_tk import apply_theme

    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 无可用显示（{type(exc).__name__}: {exc}）→ 跳过本用例")
        return None
    root.withdraw()
    apply_theme(root)
    return root


def _fake_file_dialog(root, parent=None, *, contents_name="contents", icons_name="icons"):
    """造一棵与 Tk 文件名对话框**同形状**的树，放在 `parent`（默认根）之下。

    真对话框是模态的（测试里开不了），但补色逻辑只认两件事 —— 末段 `__tk_filedialog`
    的窗口 + 它子树里末段 `cHull.canvas` 的 canvas —— 所以照抄一份即可。

    ⚠️ 两个参数正是踩过的坑：
    - `parent`：Tk 把对话框挂在 `-parent` 指定的窗口下（我们的调用点给的是**设置弹窗**，
      不是根窗口）—— 第一版只搜根窗口的孩子，于是真机上一条都命不中；
    - `contents_name`/`icons_name`：名字不写死，证明查找是"搜出来的"。
    """
    import tkinter as tk

    dlg = tk.Toplevel(parent if parent is not None else root, name="__tk_filedialog")
    contents = tk.Frame(dlg, name=contents_name)
    icons = tk.Frame(contents, name=icons_name)
    hull = tk.Frame(icons, name="cHull")
    canvas = tk.Canvas(hull, name="canvas", background="#ffffff")
    canvas.pack()
    text_item = canvas.create_text(10, 10, text=".git", fill="#ffffff")
    rect_item = canvas.create_rectangle(0, 0, 5, 5, fill="#123456")
    root.update_idletasks()
    return dlg, canvas, text_item, rect_item


# ---------------------------------------------------------------- ① 根样式前景不许在白底上看不清


def test_root_style_foreground_is_readable_on_white() -> bool:
    """★ 根样式 `.` 的前景：要么没设，要么在白底上也得看得清。

    需要 Tk（无显示环境跳过）。
    """
    root = _root_or_skip()
    if root is None:
        return True
    try:
        from tkinter import ttk

        style = ttk.Style(root)
        fg = str(style.lookup(".", "foreground") or "")
        if not fg:
            print("  ✓ 根样式没设前景 → Tk 文件对话框回落自己的默认深色（白底可读）")
            return True
        ratio = contrast(root, fg, "#ffffff")
        ok = ratio >= MIN_RATIO
        print(f"  {'✓' if ok else '✗'} 根样式前景 {fg} 对白底对比度 {ratio:.2f}（需 ≥ {MIN_RATIO}）"
              f" —— Tk 文件对话框会把文件名画在白底上，这条不达标就是白底白字")
        return ok
    finally:
        root.destroy()


# ---------------------------------------------------------------- ② 用到的输入类必须有深色输入区


def test_input_classes_have_dark_field_with_contrast() -> bool:
    """★ `TEntry` / `TSpinbox` / `TCombobox` 必须有 `fieldbackground`，且与前景对比度达标。

    「非空」这一条是关键：空串 = 回落 clam 的**白色**默认输入区（白底白字事故的根因）。
    """
    root = _root_or_skip()
    if root is None:
        return True
    try:
        from tkinter import ttk

        style = ttk.Style(root)
        all_ok = True
        for cls in INPUT_CLASSES:
            field = str(style.lookup(cls, "fieldbackground") or "")
            fg = str(style.lookup(cls, "foreground") or "")
            if not field:
                print(f"  ✗ {cls} 没有 fieldbackground（会回落 clam 的白色默认底！）")
                all_ok = False
                continue
            if not fg:
                print(f"  ✗ {cls} 没有 foreground")
                all_ok = False
                continue
            ratio = contrast(root, field, fg)
            ok = ratio >= MIN_RATIO
            print(f"  {'✓' if ok else '✗'} {cls:10} 输入区 {field} / 文字 {fg} 对比度 {ratio:.2f}"
                  f"（需 ≥ {MIN_RATIO}）")
            all_ok &= ok
        return all_ok
    finally:
        root.destroy()


# ---------------------------------------------------------------- ③ 文件对话框补色


def test_darken_file_dialog_works_and_degrades() -> bool:
    """★ 给 Tk 文件对话框那块写死白底的 canvas 补色；没有对话框时**安静地返回 0**、不抛。"""
    import tkinter as tk

    from vlt.ui_tk import SURFACE, TEXT, darken_file_dialog

    root = _root_or_skip()
    if root is None:
        return True
    try:
        all_ok = True

        # ① 没有对话框 → 0，且不抛（结构对不上时不谎报）
        n = darken_file_dialog(root)
        all_ok &= (n == 0)
        print(f"  {'✓' if n == 0 else '✗'} 没有对话框时返回 {n}（应为 0，且不抛异常）")

        # ② 挂在根下 → 底色与文字都改掉，非文字 item 不动
        dlg, canvas, text_item, rect_item = _fake_file_dialog(root)
        n = darken_file_dialog(root)
        bg = str(canvas.cget("background"))
        fill = str(canvas.itemcget(text_item, "fill"))
        rect_fill = str(canvas.itemcget(rect_item, "fill"))
        ok = n == 1 and bg == SURFACE and fill == TEXT and rect_fill == "#123456"
        all_ok &= ok
        print(f"  {'✓' if ok else '✗'} 根窗口下的对话框：命中 {n} 块（应 1）· 底色 {bg}（应 {SURFACE}）"
              f" · 文字 {fill}（应 {TEXT}）· 图形 item 不动 {rect_fill}")
        dlg.destroy()

        # ③ ★ 回归：对话框挂在**别的窗口**下（我们的调用点给的是设置弹窗 → Tk 建在它下面）
        #    第一版只搜 `winfo children "."`，真机上一条都命不中。
        host = tk.Toplevel(root, name="settingswin")
        dlg, canvas, text_item, _ = _fake_file_dialog(root, parent=host)
        n = darken_file_dialog(root)
        bg = str(canvas.cget("background"))
        fill = str(canvas.itemcget(text_item, "fill"))
        ok = n == 1 and bg == SURFACE and fill == TEXT
        all_ok &= ok
        print(f"  {'✓' if ok else '✗'} 挂在设置窗下的对话框（parent=设置弹窗）：命中 {n} 块（应 1）"
              f" · 底色 {bg} · 文字 {fill}")
        dlg.destroy()
        host.destroy()

        # ④ 路径名字不同也要能找到（证明是搜出来的，不是写死 contents/icons）
        dlg, canvas, _, _ = _fake_file_dialog(root, contents_name="contents9", icons_name="iconlist")
        n = darken_file_dialog(root)
        bg = str(canvas.cget("background"))
        ok = n == 1 and bg == SURFACE
        all_ok &= ok
        print(f"  {'✓' if ok else '✗'} 路径名不同（contents9/iconlist）：命中 {n} 块（应 1）· 底色 {bg}")
        dlg.destroy()

        return all_ok
    finally:
        root.destroy()


def test_watch_file_dialog_stops_after_dialog_closes() -> bool:
    """★ 轮询**有终**：对话框一直不出现时到 `give_up_after` 就自己收工（不留死循环）。

    用**真实时间**泵事件循环（`update()` 不会等到定时器到点，只 `update()` 空转是假时钟）。
    """
    import time

    from vlt.ui_tk import watch_file_dialog

    root = _root_or_skip()
    if root is None:
        return True
    try:
        before = set(root.tk.splitlist(root.tk.call("after", "info")))
        watch_file_dialog(root, interval_ms=1, give_up_after=3)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        left = set(root.tk.splitlist(root.tk.call("after", "info"))) - before
        ok = not left
        print(f"  {'✓' if ok else '✗'} 对话框不存在时轮询自行收工（残留 after = {sorted(left)}）")
        return ok
    finally:
        root.destroy()


# ---------------------------------------------------------------- 入口


def main() -> int:
    tests = [
        ("根样式前景在白底上可读", test_root_style_foreground_is_readable_on_white),
        ("输入类有深色输入区且对比度达标", test_input_classes_have_dark_field_with_contrast),
        ("文件对话框补色 + 静默降级", test_darken_file_dialog_works_and_degrades),
        ("文件对话框轮询有终", test_watch_file_dialog_stops_after_dialog_closes),
    ]
    print("test_ui_theme_contrast:")
    failed = 0
    for name, fn in tests:
        try:
            if not fn():
                failed += 1
                print(f"  ❌ {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {name}: {type(exc).__name__}: {exc}")
    print()
    print("ALL PASSED" if not failed else f"❌ {failed} 个用例失败")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
