"""首次启用麦克风代理时给一次说明（它会常驻占用虚拟声卡的输出流）；只提示一次。

## 为什么要有这个弹窗

代理默认是**开**的，而且程序一启动就打开虚拟声卡的输出流（见 ``micproxy`` 模块头「生命周期」）。
用户看到的现象却是「装完这软件，我声卡就一直响/有回声」，却不知道是谁占的 ——
尤其**默认播放设备也指向那块虚拟声卡**的人，会一直听到自己的声音。

所以第一次真正把代理跑起来时，明说一次：占用了什么、回声从哪来、去哪儿关掉。
提示过就落盘（``output.audio.proxy.hint_shown``），以后不再打扰。

## 纪律

- ``headless``（跑用例 / ``--self-test`` / 打包自检）**既不弹窗、也不写标记** ——
  否则自检会把用户配置里的标记写成「已提示」，用户永远看不到这条说明。
- 弹窗是**非模态**（``transient`` + ``lift``，**不** ``grab_set``）：翻译正在跑时也不许卡住界面，
  与 ``gui_update.show_update_dialog`` 同一口径。
- 本模块**不许** ``import vlt.gui``（防循环引用），控件与回调都从 ``gui`` 属性上取。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .i18n import t

def maybe_show(gui, *, on_shown=None, opener=None) -> bool:
    """首次成功启用代理时弹一次说明，返回是否真的弹了。

    - ``on_shown``：弹窗**已显示**后调用（用来落盘「已提示」标记）；不传就只弹。
    - ``opener``：开窗函数替身（用例里传假的，免得在 CI 上真开 Tk 窗）。

    三条早退：``headless`` / 配置里已标记提示过 / 窗口被销毁后重建——都不弹。
    """
    if getattr(gui, "_headless", False):
        return False
    proxy_cfg = (gui._proxy_audio_cfg().get("proxy") or {}) if hasattr(gui, "_proxy_audio_cfg") else {}
    if proxy_cfg.get("hint_shown"):
        return False
    (opener or _open_window)(gui)
    print("[proxy] 首次启用说明已弹出（占用虚拟声卡输出流 / 默认播放设备指向它会听到回声）",
          flush=True)
    if on_shown is not None:
        on_shown()
    return True


def _open_window(gui) -> None:
    """懒建非模态 Toplevel：标题 + 正文 + 「知道了」。已存在就抬起来。"""
    win = getattr(gui, "_proxy_hint_win", None)
    if win is not None:
        try:
            if win.winfo_exists():
                win.lift()
                return
        except Exception:                                  # noqa: BLE001
            pass

    from .ui_theme import PANEL
    from .ui_tk import FONT_BOLD_MD

    win = tk.Toplevel(gui._root)
    win.title(t("麦克风代理已启用"))
    win.configure(bg=PANEL)
    win.transient(gui._root)
    win.resizable(False, False)
    win.protocol("WM_DELETE_WINDOW", lambda: close(gui))
    win.bind("<Escape>", lambda _e: close(gui))
    gui._proxy_hint_win = win

    body = ttk.Frame(win, padding=(20, 16, 20, 14))
    body.pack(fill=tk.BOTH, expand=True)
    ttk.Label(body, text=t("麦克风代理已启用"), font=FONT_BOLD_MD).pack(anchor=tk.W)
    # ⚠️ 正文必须是**字面量**（相邻字符串会被编译器拼成一个常量）：`t(BODY)` 这种动态 key
    #    不受 i18n 守卫的覆盖检查 —— 常量改了而词表没跟上时，界面会静默回落成中文原文（踩过）。
    ttk.Label(body, text=t(
        "程序会把真实麦克风直通到虚拟声卡（VRChat 里麦克风固定选它），并常驻占用它的输出流。\n\n"
        "如果你的「默认播放设备」也是这块虚拟声卡，会听到自己的声音（回声）。\n"
        "不需要的话：设置 → 麦克风代理 → 取消勾选（立即释放声卡）。"),
        wraplength=420, justify=tk.LEFT).pack(anchor=tk.W, pady=(10, 0))

    btns = ttk.Frame(body)
    btns.pack(fill=tk.X, pady=(16, 0))
    ok = ttk.Button(btns, text=t("知道了"), style="Accent.TButton",
                    command=lambda: close(gui))
    ok.pack(side=tk.LEFT)
    ok.focus_set()

    win.update_idletasks()
    rx, ry = gui._root.winfo_x(), gui._root.winfo_y()
    rw = gui._root.winfo_width()
    win.geometry(f"+{rx + max((rw - win.winfo_reqwidth()) // 2, 20)}+{ry + 60}")
    try:
        gui._apply_dark_titlebar(win)
    except Exception:                                      # noqa: BLE001
        pass
    win.lift()


def close(gui) -> None:
    """关掉弹窗（幂等）：退出程序、按 Esc、点「知道了」都走这里。"""
    win, gui._proxy_hint_win = getattr(gui, "_proxy_hint_win", None), None
    if win is None:
        return
    try:
        win.destroy()
    except Exception:                                      # noqa: BLE001
        pass
