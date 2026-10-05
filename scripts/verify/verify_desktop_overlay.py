"""独立复验：用一个假的「VRChat 窗口」验证桌面字幕窗的贴窗 / 跟随 / 透明度 / 穿透。

**作用**：真起一个标题为 `VRChat` 的假窗口，把 `DesktopOverlay` 贴上去，逐项验：
① `find_game_window` / `window_client_rect` 拿得到目标；② 位置 = 锚点计算结果；
③ 窗口移动时字幕跟随且位移量一致；④ 内容推送后出图；⑤ 透明度 / 拖动解锁时
`WS_EX_TRANSPARENT` 的开合；⑥ PrintWindow 截图自证窗口真画出来了；⑦ `close()` 幂等。

**需要的环境**：
- **仅 Windows**（`WS_EX_*` 扩展样式 + `PrintWindow` 抓图）。
- **真实桌面会话**（要能开 Tk 窗口）。
- 不需要真 VRChat、不需要麦克风/头显。

**跑法**（在仓库根目录）：
    ./.venv/Scripts/python.exe scripts/verify/verify_desktop_overlay.py

**会不会写盘**：只写 `out/verify_overlay_shot.png`（截图自证）；不读也不写任何真实
`config.yaml`（配置在这里是**直接构造**的，不落盘）。
"""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

if sys.platform != "win32":
    print("❌ 本脚本需要 Windows（WS_EX_* 扩展样式 + PrintWindow）", file=sys.stderr)
    raise SystemExit(2)

from PIL import Image  # noqa: E402

from vlt.output.desktop_overlay import (  # noqa: E402
    DesktopOverlay, DesktopOverlayConfig, compute_position,
)
from vlt.platform import find_game_window, window_client_rect, top_level_hwnd  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(label)


def exstyle(hwnd: int) -> int:
    u = ctypes.windll.user32
    u.GetWindowLongPtrW.restype = ctypes.c_longlong
    u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return int(u.GetWindowLongPtrW(ctypes.c_void_p(hwnd), -20)) & 0xFFFFFFFF


def main() -> int:
    root = tk.Tk()
    root.withdraw()

    # 假「VRChat 窗口」：唯一标题含 VRChat 的可见顶层窗口
    fake = tk.Toplevel(root)
    fake.title("VRChat")
    fake.geometry("800x600+300+150")
    fake.configure(bg="#204060")
    tk.Label(fake, text="(假的 VRChat 窗口)", bg="#204060", fg="white").pack(expand=True)
    fake.update()
    fake_hwnd = top_level_hwnd(fake.winfo_id())
    print(f"[verify] 假窗口 hwnd=0x{fake_hwnd:X}")

    print("\n=== 1. 窗口查找 ===")
    found = find_game_window("VRChat")
    check(found is not None, "find_game_window('VRChat') 有结果", str(found))
    rect = window_client_rect(found) if found else None
    check(rect is not None, "window_client_rect 有结果", str(rect))

    print("\n=== 2. 贴到窗口上 ===")
    # 视觉参数全部写在**本段**里（desktop_overlay 段不从 overlay 段继承任何东西）
    cfg = DesktopOverlayConfig.from_dict(
        {"enabled": True, "anchor": "bottom_center", "offset": [0, -40],
         "size_px": [640, 240], "alpha": 0.9, "game_title": "VRChat",
         "font_size": 34, "source_font_size": 26, "max_lines": 2},
    )
    ov = DesktopOverlay(cfg, root=root)
    ok = ov.start()
    check(ok, "start() 成功")
    fake.update()
    root.update()
    gr = ov.game_rect
    check(gr is not None, "overlay 拿到了游戏窗口矩形", str(gr))
    if gr:
        want = compute_position("bottom_center", (0, -40), gr, (640, 240), (0, 0, 1920, 1080))
        got = ov.position
        check(abs(got[0] - want[0]) <= 2 and abs(got[1] - want[1]) <= 2,
              "位置 = 锚点计算结果", f"got={got} want={want}")

    print("\n=== 3. 跟随窗口移动 ===")
    pos_before = ov.position
    fake.geometry("800x600+700+420")
    fake.update()
    root.update()
    ov.tick()
    pos_after = ov.position
    check(pos_after != pos_before, "窗口移动后 overlay 位置跟着变",
          f"{pos_before} -> {pos_after}")
    dx = pos_after[0] - pos_before[0]
    check(abs(dx - 400) <= 2, "位移量 = 窗口位移量（+400px）", f"dx={dx}")

    print("\n=== 4. 内容上屏 ===")
    n0 = ov.frame_count
    ov.update_entries([("theirs", "こんにちは、いい天気ですね", "你好，今天天气真好"),
                       ("mine", "yeah just back from onsen", "是啊，刚从温泉回来")], force=True)
    ov.tick()
    root.update()
    check(ov.frame_count > n0, "出图帧数增加", f"{n0} -> {ov.frame_count}")

    print("\n=== 5. 透明度 / 穿透 ===")
    ov.set_alpha(0.55)
    root.update()
    a = float(fake.master.tk.call("wm", "attributes", ov._win._w, "-alpha")) if hasattr(ov, "_win") else None
    check(a is not None and abs(a - 0.55) < 0.01, "窗口透明度 = 0.55", str(a))
    ov.set_draggable(True)
    root.update()
    ex = exstyle(top_level_hwnd(ov._win.winfo_id())) if hasattr(ov, "_win") else 0
    check(not (ex & 0x20), "解锁拖动后 WS_EX_TRANSPARENT 已关", f"ex=0x{ex:08X}")
    ov.set_draggable(False)
    root.update()
    ex = exstyle(top_level_hwnd(ov._win.winfo_id()))
    check(bool(ex & 0x20), "锁定后 WS_EX_TRANSPARENT 已开", f"ex=0x{ex:08X}")

    print("\n=== 6. 抓图自证（窗口真的画出来了）===")
    hwnd = top_level_hwnd(ov._win.winfo_id())
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    cdc = u.GetWindowDC(hwnd)
    mdc = g.CreateCompatibleDC(cdc)
    bmp = g.CreateCompatibleBitmap(cdc, 640, 240)
    g.SelectObject(mdc, bmp)
    used_printwindow = bool(u.PrintWindow(hwnd, mdc, 1))
    buf = ctypes.create_string_buffer(640 * 240 * 4)

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    bi = BIH(biSize=ctypes.sizeof(BIH), biWidth=640, biHeight=-240, biPlanes=1, biBitCount=32)
    g.GetDIBits(mdc, bmp, 0, 240, buf, ctypes.byref(bi), 0)
    im = Image.frombuffer("RGBA", (640, 240), buf, "raw", "BGRA", 0, 1).convert("RGB")
    colors = sorted(im.getcolors(maxcolors=200000), reverse=True)
    print(f"[verify] PrintWindow={used_printwindow} 不同颜色数={len(colors)} top3={colors[:3]}")
    check(used_printwindow and len(colors) > 5, "截图里有真实内容（颜色数 > 5）")
    out_dir = ROOT / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "verify_overlay_shot.png"
    im.save(out)
    print(f"[verify] 截图 {out}")

    print("\n=== 7. 关闭幂等 ===")
    ov.close()
    ov.close()
    root.update()
    check(True, "close() 重复调用不抛异常")

    fake.destroy()
    root.destroy()
    print("\n" + "=" * 48)
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
