"""最终自证：字幕窗叠在**另一个窗口**上，从桌面 DC 抓屏看真实合成效果。

**作用**：与 `verify_desktop_overlay.py` 的区别 —— 那边抓的是字幕窗自己（PrintWindow），
这边抓的是**屏幕**（BitBlt 桌面 DC），能直接看出「色键区域是不是真的透过去了」，
也就是用户在 VRChat 里会看到的样子。判据：面板四角应是下方假游戏窗的底色
（说明色键透明了），中心区域应是深色面板（说明面板本身画出来了）。

**需要的环境**：
- **仅 Windows**（BitBlt 桌面 DC）。
- **真实桌面会话（且不是断开/锁屏的会话）**：断开时 BitBlt 抓到全黑，脚本会明确报错
  退出而不是给个假结果。
- 不需要真 VRChat / 麦克风 / 头显。

**跑法**（在仓库根目录）：
    ./.venv/Scripts/python.exe scripts/verify/verify_overlay_over_desktop.py

**会不会写盘**：只写 `out/overlay_over_desktop.png`（截图证据）；不读也不写真实配置。
"""
from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

if sys.platform != "win32":
    print("❌ 本脚本需要 Windows（BitBlt 桌面 DC）", file=sys.stderr)
    raise SystemExit(2)

from PIL import Image  # noqa: E402

from vlt.output.desktop_overlay import DesktopOverlay, DesktopOverlayConfig  # noqa: E402

GAME_BG = "#3a2f6b"          # 假 VRChat 窗口的底色（一眼能认出是不是透过去了）
FAKE_TITLE = "VLT_FAKE_GAME_FOR_SHOT"


def grab_screen(rect: tuple[int, int, int, int]) -> Image.Image:
    """从桌面 DC 抓一块屏幕区域（真实合成结果，不是 PrintWindow）。"""
    l, t, r, b = (int(v) for v in rect)
    w, h = r - l, b - t
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    u.SetProcessDPIAware()
    sdc = u.GetDC(0)
    mdc = g.CreateCompatibleDC(sdc)
    bmp = g.CreateCompatibleBitmap(sdc, w, h)
    g.SelectObject(mdc, bmp)
    SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
    g.BitBlt(mdc, 0, 0, w, h, sdc, l, t, SRCCOPY | CAPTUREBLT)

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    bi = BIH(biSize=ctypes.sizeof(BIH), biWidth=w, biHeight=-h, biPlanes=1, biBitCount=32)
    buf = ctypes.create_string_buffer(w * h * 4)
    g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    g.DeleteObject(bmp)
    g.DeleteDC(mdc)
    u.ReleaseDC(0, sdc)
    return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")


def main() -> int:
    root = tk.Tk()
    root.withdraw()

    # 假"VRChat 窗口"：一块纯色大窗（字幕要叠在它上面）
    game = tk.Toplevel(root)
    game.title(FAKE_TITLE)
    game.geometry("1100x620+60+40")
    game.configure(bg=GAME_BG)
    tk.Label(game, text="（这是模拟的 VRChat 窗口底色）", bg=GAME_BG, fg="#c8c0ff").pack(expand=True)
    game.attributes("-topmost", True)     # 保证它盖住旁边的终端/编辑器，抓屏才看得到底色
    game.update()
    time.sleep(0.4)

    # 视觉参数全写在**本段**（desktop_overlay 段不从 overlay 段继承任何东西）
    cfg = DesktopOverlayConfig.from_dict(
        {"enabled": True, "anchor": "bottom_center", "offset": [0, -40],
         "size_px": [1000, 300], "alpha": 0.92, "game_title": FAKE_TITLE,
         "attach_to_game": True, "follow": True,
         "font_size": 40, "source_font_size": 30, "max_lines": 2},
    )
    ov = DesktopOverlay(cfg, root=root)
    assert ov.start(), "字幕窗没起来"
    ov.update_entries([
        ("theirs", "こんにちは、今日はいい天気ですね", "你好，今天天气真不错呢"),
        ("mine", "yeah I just came back from the hot spring", "是啊，我刚从温泉回来"),
    ], force=True)
    ov.tick()
    root.update()
    time.sleep(0.8)
    root.update()

    pos = ov.position
    w, h = cfg.size_px
    shot = grab_screen((pos[0], pos[1], pos[0] + w, pos[1] + h))
    out_dir = ROOT / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "overlay_over_desktop.png"
    shot.save(out)

    # 判据：面板四角应当是假游戏窗的底色（说明色键真的透过去了），
    # 中心区域应当是深色面板（说明面板本身画出来了）
    corners = [shot.getpixel((3, 3)), shot.getpixel((w - 4, 3)),
               shot.getpixel((3, h - 4)), shot.getpixel((w - 4, h - 4))]
    center = shot.getpixel((w // 2, h // 2))
    want = tuple(int(GAME_BG[i:i + 2], 16) for i in (1, 3, 5))

    def near(a, b, tol=26):
        return all(abs(x - y) <= tol for x, y in zip(a, b))

    print(f"[shot] 字幕位置={pos} 尺寸={w}x{h}")
    print(f"[shot] 四角像素={corners}（假游戏窗底色 {want}）")
    print(f"[shot] 中心像素={center}")
    print(f"[shot] 截图 {out}")

    # 无真实桌面会话（BitBlt 全黑）时明确报错，不给假结果
    if all(sum(c) == 0 for c in corners) and sum(center) == 0:
        print("❌ 抓到的整块是纯黑 —— 疑似**没有真实桌面会话**（断开/锁屏）。"
              "本脚本需要真实可见的桌面会话，请在有桌面的会话里重跑。")
        ov.close()
        game.destroy()
        root.destroy()
        return 2

    ok = all(near(c, want) for c in corners) and not near(center, want)
    print("✅ 色键区域真的透明、面板画在上面" if ok else "❌ 合成结果不对（见上面的像素值）")

    ov.close()
    game.destroy()
    root.destroy()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
