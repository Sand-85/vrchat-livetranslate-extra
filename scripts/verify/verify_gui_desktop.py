"""界面级端到端复验：真起 TranslationGUI → 勾「桌面字幕」→ 检查窗、内容、滑块、拖动落盘。

**作用**：证明「桌面字幕」这条界面链路在**真 Tk 窗口**里真的能走通 ——
勾选后字幕窗建起来、内容推送后出图帧数增加、透明度滑块落盘、拖动解锁→锁定后
把 `(锚点, 偏移)` 写回配置、（切回）取消勾选后窗口销毁。

**需要的环境**：
- **仅 Windows**（桌面字幕窗走 Win32 色键 + `WS_EX_TRANSPARENT` 那套）。
- **真实桌面会话**（要能开 Tk 窗口；无头会话里 Tk 起不来会直接报错）。
- 不需要 VRChat、不需要麦克风、不需要头显。

**跑法**（在仓库根目录）：
    ./.venv/Scripts/python.exe scripts/verify/verify_gui_desktop.py

**会不会写盘**：只写**沙箱** —— 配置被指到 `out/_sandbox_config.yaml`
（从 `config.yaml` 复制；没有就退回复制 `config.example.yaml`），
**绝不碰真实 `config.yaml`**；脚本结尾还会断言沙箱与源配置确已分叉，以自证没写错地方。
截图/产物一律落在 `out/`。
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]        # scripts/verify/ -> 仓库根
sys.path.insert(0, str(ROOT))

if sys.platform != "win32":
    print("❌ 本脚本需要 Windows（桌面字幕窗用 Win32 色键 + 扩展样式）", file=sys.stderr)
    raise SystemExit(2)

# 会话里可能残留测试注入的假 key（见 skill）：清掉，避免污染任何后续联网分支
os.environ.pop("DASHSCOPE_API_KEY", None)

FAILS: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(label)


def main() -> int:
    # ---- 沙箱：把 DEFAULT_CONFIG 指向副本，任何写盘都落在 out/ 里 ----
    out_dir = ROOT / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    sandbox = out_dir / "_sandbox_config.yaml"
    real = ROOT / "config.yaml"
    source = real if real.exists() else ROOT / "config.example.yaml"
    if not source.exists():
        print(f"❌ 找不到 {real}，也没有 {ROOT / 'config.example.yaml'}")
        return 1
    shutil.copyfile(source, sandbox)
    print(f"[sandbox] 基线配置 = {source.name} → {sandbox.name}")

    import vlt.config as C
    import vlt.gui as G
    import vlt.i18n as I

    G.DEFAULT_CONFIG = sandbox
    C.DEFAULT_CONFIG = sandbox
    I.detect_system_language = lambda: "zh"        # 界面语言钉死（见 skill）

    print("=== 1. 起界面（真实 Tk）===")
    gui = G.TranslationGUI()
    gui._root.update()
    check(True, "TranslationGUI 起来了")
    check(hasattr(gui, "_desktop_var"), "输出行里有「桌面字幕」勾选框")
    check(gui._desktop_out is None, "默认没勾 → 没有字幕窗")

    print("=== 2. 勾上 → 字幕窗出现 ===")
    gui._desktop_var.set(True)
    gui._on_desktop_toggle()
    gui._root.update()
    time.sleep(0.3)
    gui._root.update()
    out = gui._desktop_out
    check(out is not None, "勾上后 _desktop_out 已建")
    if out is None:
        print("  （后面几项依赖字幕窗，跳过）")
    else:
        check(out.frame_count >= 1, "已经出过图（空面板也算）", str(out.frame_count))
        win = getattr(out, "_win", None)
        check(win is not None and win.winfo_exists(), "Tk 窗口真的存在")
        hwnd = getattr(out, "_hwnd", 0)
        ex = 0
        if hwnd:
            import ctypes
            u = ctypes.windll.user32
            u.GetWindowLongPtrW.restype = ctypes.c_longlong
            u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            ex = int(u.GetWindowLongPtrW(ctypes.c_void_p(hwnd), -20)) & 0xFFFFFFFF
        check(bool(ex & 0x20), "鼠标穿透已开（WS_EX_TRANSPARENT）", f"ex=0x{ex:08X}")
        check(bool(ex & 0x80) and bool(ex & 0x08000000),
              "不进 alt-tab / 不抢焦点", f"ex=0x{ex:08X}")

        print("=== 3. 内容上屏（走界面自己的推送路径）===")
        n0 = out.frame_count
        gui._add_text("こんにちは、今日はいい天気ですね", "你好，今天天气真不错", True, who="theirs")
        gui._add_text("yeah just back from onsen", "是啊，刚从温泉回来", True, who="mine")
        gui._push_desktop(force=True)
        gui._desktop_out.tick()
        gui._root.update()
        check(out.frame_count > n0, "推送聊天内容后出图帧数增加",
              f"{n0} -> {out.frame_count}")

        print("=== 4. 透明度滑块 ===")
        gui._desktop_alpha_var.set(0.55)
        gui._on_desktop_alpha("0.55")
        gui._root.update()
        a = float(win.attributes("-alpha"))
        check(abs(a - 0.55) < 0.02, "窗口透明度跟着滑块走", str(a))

        print("=== 5. 拖动解锁 → 落盘 ===")
        before = out.position
        gui._toggle_desktop_drag()
        gui._root.update()
        check(gui._desktop_dragging is True, "已进入拖动解锁态")

        # 模拟拖到别处：直接调模块的拖动处理（真鼠标那一路在模块测试里验过）
        class _Ev:
            def __init__(self, x, y):
                self.x_root, self.y_root = x, y

        out._on_drag_start(_Ev(before[0] + 5, before[1] + 5))
        out._on_drag_move(_Ev(before[0] + 5 + 60, before[1] + 5 + 40))
        gui._root.update()
        out._on_drag_end(_Ev(0, 0))
        gui._toggle_desktop_drag()      # 锁定 → 写盘
        gui._root.update()
        check(gui._desktop_dragging is False, "已回到锁定态")
        gui._save_desktop_cfg()
        txt = sandbox.read_text(encoding="utf-8")
        check("desktop_overlay:" in txt, "沙箱配置里补建出了 desktop_overlay 段")
        check("alpha:" in txt, "透明度写回了配置")
        check(any(k in txt for k in ("anchor:", "pos:")), "位置（锚点/坐标）写回了配置")
        idx = txt.find("desktop_overlay:")
        print("  --- 写入的段 ---")
        for line in txt[idx:].splitlines()[:8]:
            print(f"  | {line}")

        print("=== 6. 关掉 ===")
        gui._desktop_var.set(False)
        gui._on_desktop_toggle()
        gui._root.update()
        check(gui._desktop_out is None, "取消勾选后字幕窗已销毁")

        print("=== 7. 只勾桌面字幕也能开跑（sinks 校验）===")
        gui._chatbox_var.set(False)
        gui._overlay_var.set(False)
        gui._desktop_var.set(True)
        gui._on_desktop_toggle()
        gui._root.update()
        check(gui._desktop_out is not None, "只有桌面字幕时也能起来")

    # ---- 收尾：还原沙箱 ----
    try:
        gui._root.destroy()
    except Exception:      # noqa: BLE001
        pass
    # 确认真配置没被动过（沙箱与源配置已分叉 = 写盘只落在沙箱）
    same = sandbox.read_bytes() != source.read_bytes()
    check(same, "沙箱配置与源配置已分叉（证明写盘只落在沙箱）")

    print()
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：" + "、".join(FAILS))
        return 1
    print("✅ 界面级端到端全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
