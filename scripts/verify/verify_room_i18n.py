"""房间改造的多语言布局体检（真起窗口，逐控件量宽度）。

**作用**：对齐仓库的「界面显示体检」口径 —— 真起窗口，逐控件量
`winfo_reqwidth()` vs `winfo_width()`，**五种语言**（zh/en/ja/ko/ru）各跑一遍
（俄语最长、最容易裁），并给 en/ru 各截一张设置弹窗「房间」页的图。
只验文案够不够不够：容器不够宽时 Tk 会**从最后打包的控件开始裁**，肉眼与用例都看不出。

**需要的环境**：
- **仅 Windows**（截图用 `scripts/verify/_shot_window.py` 的 PrintWindow）。
- **真实桌面会话**（要能开 Tk 窗口）。
- 不需要联网/麦克风/头显。

**跑法**（在仓库根目录）：
    ./.venv/Scripts/python.exe scripts/verify/verify_room_i18n.py

**会不会写盘**：配置一律写在**临时目录**（每个语言一个 `tempfile.mkdtemp`），截图落在
`out/`；**绝不碰真实 `config.yaml`**。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]        # scripts/verify/ -> 仓库根
sys.path.insert(0, str(REPO))

if sys.platform != "win32":
    print("❌ 本脚本需要 Windows（PrintWindow 截图）", file=sys.stderr)
    raise SystemExit(2)

os.environ.pop("DASHSCOPE_API_KEY", None)

LANGS = ("zh", "en", "ja", "ko", "ru")
rows_bad: list[str] = []
pages_bad: list[str] = []


def run_one(lang: str) -> None:
    import importlib

    tmp = Path(tempfile.mkdtemp(prefix=f"vlt-i18n-{lang}-"))
    cfg = tmp / "config.yaml"
    cfg.write_text(f"session:\n  api_key: ''\nui:\n  lang: {lang}\n",
                   encoding="utf-8", newline="\n")

    import vlt.config as _config
    import vlt.gui as gui_mod
    import vlt.i18n as _i18n
    importlib.reload(_i18n)
    importlib.reload(gui_mod)
    _config.DEFAULT_CONFIG = cfg
    gui_mod.DEFAULT_CONFIG = cfg
    _i18n.detect_system_language = lambda: lang
    _i18n.set_language(lang)

    gui = gui_mod.TranslationGUI()
    root = gui._root
    try:
        root.geometry("940x620")
        root.update_idletasks()
        root.update()

        t = _i18n.t
        print(f"\n=== {lang} ===")
        # ① 主界面房间行
        row = gui._room_row
        for ch in row.winfo_children():
            if not ch.winfo_manager():
                continue
            req, act = ch.winfo_reqwidth(), ch.winfo_width()
            bad = act < req
            rows_bad.append(lang) if bad else None
            print(f"  [行] {ch.winfo_class():9s} req={req:4d} w={act:4d} 裁={bad} "
                  f"{str(ch.cget('text'))[:40]!r}")
        if row.winfo_width() < row.winfo_reqwidth():
            rows_bad.append(lang)
            print(f"  ★ 房间行整体被裁：req {row.winfo_reqwidth()} > {row.winfo_width()}")

        # ② 设置弹窗「房间」页
        gui._open_settings(page="room")
        root.update_idletasks()
        root.update()
        nb = gui._settings_nb
        cur = nb.tab(nb.select(), "text")
        assert cur == t("房间"), f"{lang}: 没停在房间页，实际 {cur!r}"
        tab = gui._settings_tabs[t("房间")]
        inner = [w for w in tab.winfo_children()]           # canvas + 可能的滚动条
        canvas = next(w for w in inner if w.winfo_class() == "Canvas")
        page = canvas.winfo_children()[0]
        # create_window 放进去的 inner frame
        children = page.winfo_children()
        stack = list(children)
        while stack:
            ch = stack.pop()
            if not ch.winfo_manager():
                continue
            req, act = ch.winfo_reqwidth(), ch.winfo_width()
            txt = ""
            try:
                txt = str(ch.cget("text"))[:44]
            except Exception:
                pass
            # ⚠️ Tk 的 `Menu` 控件（下拉弹出菜单）永远报 width≈1、也没被真正布局 ——
            #    把它算成「被裁」会给出假红（实测五种语言全红、全是 Menu）。
            #    未映射的控件同理：没上屏就谈不上被容器裁。
            realized = ch.winfo_ismapped() and ch.winfo_class() != "Menu"
            clip = realized and act < req
            # 输入框/标签在 fill=X 的容器里：只要容器装得下就没事；这里只报“比可视宽还宽”
            overflow = realized and act > canvas.winfo_width() + 1
            if clip or overflow:
                pages_bad.append(lang)
            print(f"    [页] {ch.winfo_class():9s} req={req:4d} w={act:4d} 裁={clip} "
                  f"超宽={overflow} {txt!r}")
            stack.extend(ch.winfo_children())

        # 两个输入框必须左缘对齐（标签长度随语言不同：en "Room code:"/"Nickname:"）
        entries = [w for w in page.winfo_children()
                   if w.winfo_class() == "TFrame"]
        xs = []
        for fr in entries:
            for e in fr.winfo_children():
                if e.winfo_class() == "TEntry":
                    xs.append(e.winfo_x())
        if len(xs) >= 2 and len(set(xs)) != 1:
            pages_bad.append(lang)
            print(f"  ★ 两个输入框左缘没对齐：{xs}")

        if lang in ("en", "ru"):
            import ctypes
            import _shot_window as sw
            (REPO / "out").mkdir(parents=True, exist_ok=True)
            win = gui._settings_win
            win.update_idletasks()
            win.update()
            hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)
            _l, _top, _r, _b = sw.rect_of(hwnd)
            p = REPO / "out" / f"room_btn_settings_{lang}.png"
            sw.capture_printwindow(hwnd, _r - _l, _b - _top).save(p)
            print(f"    {lang} 截图 {_r-_l}x{_b-_top} → {p}")
    finally:
        try:
            root.destroy()
        except Exception:
            pass


for _lang in LANGS:
    run_one(_lang)

print("\n=== 汇总 ===")
print(f"房间行被裁的语言：{sorted(set(rows_bad)) or '无'}")
print(f"房间页被裁/超宽的语言：{sorted(set(pages_bad)) or '无'}")
print("DONE" if not rows_bad and not pages_bad else "SOME FAILED")
