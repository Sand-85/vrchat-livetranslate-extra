"""房间按钮改造的验收脚本（真起 GUI，Windows 桌面会话）。

**作用**：验房间行改造后的界面行为：
① 主界面第三行只有「连接/断开」按钮 + 状态标签，且**没被裁**（winfo_reqwidth <= winfo_width）；
② 按钮文案三态：未连接 → 连接房间；CONNECTING → 连接中（禁用）；ONLINE → 断开连接；
③ 空房间码点按钮：不建 RoomClient、有提示、设置弹窗打开并切到「房间」页；
④ 房间码/昵称输入框已在设置弹窗里、不在主界面行里；
⑤ PrintWindow 截主窗口 + 设置弹窗「房间」页（本机会话断开也能截）。

**需要的环境**：
- **仅 Windows**（PrintWindow 截图；本脚本用 `scripts/verify/_shot_window.py`）。
- **真实桌面会话**（要能开 Tk 窗口）。
- 不需要联网、不需要麦克风/头显。

**跑法**（在仓库根目录）：
    ./.venv/Scripts/python.exe scripts/verify/verify_room_button.py

**会不会写盘**：只写**沙箱** —— 配置被指到 `out/_room_btn_sandbox.yaml`（脚本自己新建），
截图落在 `out/`；**绝不碰真实 `config.yaml`**。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]        # scripts/verify/ -> 仓库根
sys.path.insert(0, str(REPO))

if sys.platform != "win32":
    print("❌ 本脚本需要 Windows（PrintWindow 截图）", file=sys.stderr)
    raise SystemExit(2)

# ---- 隔离：语言钉死 zh + 配置指向沙箱（必须在 import vlt.gui 之前/构造窗口之前）----
_tmp = Path(tempfile.mkdtemp(prefix="vlt-room-btn-"))
os.environ.pop("DASHSCOPE_API_KEY", None)
(REPO / "out").mkdir(parents=True, exist_ok=True)
SANDBOX = REPO / "out" / "_room_btn_sandbox.yaml"
SANDBOX.write_text("session:\n  api_key: ''\nui:\n  lang: zh\n", encoding="utf-8", newline="\n")

import vlt.config as _config          # noqa: E402
import vlt.gui as gui_mod             # noqa: E402
_config.DEFAULT_CONFIG = SANDBOX
gui_mod.DEFAULT_CONFIG = SANDBOX

import vlt.i18n as _i18n              # noqa: E402
_i18n.detect_system_language = lambda: "zh"

import _shot_window as sw             # noqa: E402  同目录下的 Win32 截图助手
from vlt.gui import TranslationGUI    # noqa: E402
from vlt.i18n import t                # noqa: E402
from vlt.room.model import ConnectionState  # noqa: E402

results: dict[str, bool] = {}
gui = TranslationGUI()
root = gui._root


def check(tag: str, cond: bool, detail: str = "") -> bool:
    results[tag] = bool(cond)
    print(f"  [{'OK ' if cond else '★FAIL'}] {tag}{(' — ' + detail) if detail else ''}")
    return bool(cond)


def shot(path: Path) -> None:
    import ctypes
    root.update_idletasks()
    root.update()
    hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
    l, top, r, b = sw.rect_of(hwnd)
    img = sw.capture_printwindow(hwnd, r - l, b - top)
    img.save(path)
    print(f"    截图 {r-l}x{b-top} → {path}")


def shot_toplevel(win, path: Path) -> None:
    import ctypes
    win.update_idletasks()
    win.update()
    hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)
    l, top, r, b = sw.rect_of(hwnd)
    img = sw.capture_printwindow(hwnd, r - l, b - top)
    img.save(path)
    print(f"    截图 {r-l}x{b-top} → {path}")


def m(cond) -> None:
    assert cond, "验收失败"


def step_row() -> None:
    print("=== ① 房间行：只有按钮 + 状态，且未被裁 ===")
    root.geometry("940x620")
    root.update_idletasks()
    root.update()
    row = gui._room_row
    m(row is not None and row.winfo_manager() == "pack")
    classes = [w.winfo_class() for w in row.winfo_children() if w.winfo_manager()]
    print(f"    行内控件：{classes}")
    check("行内有按钮", "TButton" in classes, str(classes))
    check("行内没有勾选框", "Checkbutton" not in classes, str(classes))
    check("行内没有输入框（房间码/昵称已移入设置）", "TEntry" not in classes, str(classes))
    need, have = row.winfo_reqwidth(), row.winfo_width()
    check("房间行未被裁", need <= have + 1, f"req {need} <= width {have}")
    check("状态文案非空", bool(str(gui._room_status.cget("text")).strip()),
          repr(gui._room_status.cget("text")))
    check("初始按钮文案 = 连接房间", gui._room_btn.cget("text") == t("连接房间"),
          repr(gui._room_btn.cget("text")))


def step_btn_states() -> None:
    print("=== ② 按钮文案三态 ===")
    def stub(conn, peers):
        return SimpleNamespace(state=lambda: SimpleNamespace(conn=conn, peer_count=peers),
                               stop=lambda timeout=5.0: None)

    gui._room = None
    gui._refresh_room_btn()
    check("未连接 → 连接房间", gui._room_btn.cget("text") == t("连接房间"),
          repr(gui._room_btn.cget("text")))
    check("未连接 → 按钮可点", str(gui._room_btn.cget("state")) == "normal")

    gui._room = stub(ConnectionState.CONNECTING, 0)
    gui._refresh_room_btn()
    check("连接中 → 连接中且禁用",
          gui._room_btn.cget("text") == t("连接中") and str(gui._room_btn.cget("state")) == "disabled",
          f"{gui._room_btn.cget('text')!r}/{gui._room_btn.cget('state')!r}")

    gui._room = stub(ConnectionState.ONLINE, 3)
    gui._refresh_room_status_label()          # 真实路径：_poll 每 0.5s 调的是这个（它内部会刷按钮）
    check("已连接 → 断开连接", gui._room_btn.cget("text") == t("断开连接"),
          repr(gui._room_btn.cget("text")))
    check("已连接 → 按钮可点", str(gui._room_btn.cget("state")) == "normal")
    check("状态标签含人数", "3" in str(gui._room_status.cget("text")),
          repr(gui._room_status.cget("text")))
    gui._room = None
    gui._refresh_room_btn()


def step_empty_code() -> None:
    print("=== ③ 空房间码点「连接房间」：不连、有提示、跳到设置页 ===")
    gui._room_cfg = gui._room_cfg.with_overrides(room_code="", nickname="")
    if hasattr(gui, "_room_code_var"):
        gui._room_code_var.set("")
    gui._room = None
    gui._on_room_button()
    root.update_idletasks()
    check("没有建 RoomClient", gui._room is None)
    status = str(gui._room_status.cget("text"))
    check("按钮仍是「连接房间」", gui._room_btn.cget("text") == t("连接房间"),
          repr(gui._room_btn.cget("text")))
    print(f"    状态栏文案：{status!r}")
    check("设置弹窗已打开", gui._settings_win.winfo_viewable() == 1)
    try:
        cur = gui._settings_nb.tab(gui._settings_nb.select(), "text")
    except Exception as exc:  # noqa: BLE001
        cur = f"<读取失败 {exc}>"
    print(f"    当前设置页：{cur!r}")
    check("已切到「房间」页", cur == t("房间"), repr(cur))


def step_code_entry_lives_in_settings() -> None:
    print("=== ④ 房间码/昵称输入框在设置弹窗里 ===")
    check("_room_code_var 仍在", hasattr(gui, "_room_code_var"))
    check("_room_nick_var 仍在", hasattr(gui, "_room_nick_var"))
    row = gui._room_row
    in_row = [w for w in row.winfo_children() if w.winfo_class() == "TEntry"]
    check("输入框不在主界面行里", not in_row, f"{len(in_row)} 个")


def step_shots() -> None:
    print("=== ⑤ 截图 ===")
    shot(REPO / "out" / "room_btn_main.png")
    shot_toplevel(gui._settings_win, REPO / "out" / "room_btn_settings.png")
    # 已连接态：按钮应换成红棕的「断开连接」（与蓝色的「连接房间」明显不同色）
    gui._room = SimpleNamespace(
        state=lambda: SimpleNamespace(conn=ConnectionState.ONLINE, peer_count=2),
        stop=lambda timeout=5.0: None)
    gui._refresh_room_status_label()
    root.update_idletasks()
    print(f"    已连接态按钮：{gui._room_btn.cget('text')!r} / 样式 "
          f"{gui._room_btn.cget('style')!r}")
    shot(REPO / "out" / "room_btn_main_connected.png")
    gui._room = None
    gui._refresh_room_btn()


def step_summary() -> None:
    print("=== 汇总 ===")
    for k, v in results.items():
        print(f"    {'OK  ' if v else '★FAIL'} {k}")
    print("DONE" if all(results.values()) else "SOME FAILED")


steps = [step_row, step_btn_states, step_empty_code, step_code_entry_lives_in_settings,
         step_shots]


def run_next() -> None:
    if steps:
        fn = steps.pop(0)
        try:
            fn()
        except AssertionError as exc:
            results[f"{fn.__name__}(断言失败)"] = False
            print(f"  ★ 断言失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            results[f"{fn.__name__}(异常)"] = False
            print(f"  ★ 异常：{type(exc).__name__}: {exc}")
        root.after(400, run_next)
    else:
        step_summary()
        try:
            root.destroy()
        except Exception:
            pass


root.after(200, run_next)
root.mainloop()
print("script end")
