"""独立复核线上 Release 附件（不依赖 CI 的自检结论）。

用法：.venv/Scripts/python.exe scripts/verify_release.py v0.6.1 "RoomClient"

⚠️ **本仓库只发 Windows 版**：没有 AppImage / Linux 附件，所以本脚本就是全部验收面
（上游那份里的 `verify_appimage.py` 流程不适用于本仓库）。本版新增「多人房间文本中继」，
needle 取新模块的类名 `RoomClient`（`vlt/room/client.py`，解包后在 `vlt/room/client.pyc` 里命中）。

第二个参数 = 本版代码里必定出现的字符串，**可以用逗号给多个**（每个都要命中才算过）。
判据是「在解包出来的字节码里搜得到」—— 不是搜 exe 原始字节（那是压缩过的 PYZ，永远搜不到）。

用法：.venv/Scripts/python.exe scripts/verify_release.py v0.7.4 "needle1,needle2"

⚠️ **本仓库只发 Windows 版**：没有 AppImage / Linux 附件，所以本脚本就是全部验收面
（上游那份里的 `verify_appimage.py` 流程不适用于本仓库）。
修复型发布没有新文案可挑时，用**修复引入的符号名**最可靠。

复核项：
  1. 附件下载（只认 exe）
  2. sha256 实测 vs **GitHub 服务端算的 asset digest**（独立于我们 CI 的那一份）
  3. 真跑一次 `--self-test`（退出码 + GUI_SELFTEST_OK）
  4. 启动日志里的版本行 = 本次 tag（且标明「打包 exe」）
  5. exe 里确实含本版新增的字符串（新功能真在产物里，不是只进了仓库）
  6. exe 图标资源 vs assets/app.ico（32/16 档像素比对；取图标句柄带重试，见该段注释）

⚠️ 复核以 **GitHub 服务端算的 asset digest** 为准，不看我们自己传的 `SHA256SUMS.txt`：
前者是对收到的字节算的，才是能证明「发布出去的确实是我们构建的那个」的独立凭据。
（`SHA256SUMS.txt` 仍会随 Release 一起传，但只为兼容 **v0.2.0 及更早**的客户端 ——
它们的更新检查把「exe + SHA256SUMS.txt 两个附件」写死了，缺一个就静默查不到更新。）
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TAG = sys.argv[1] if len(sys.argv) > 1 else "v0.0.2"
# 本版新功能里必定出现的字符串；**逗号可给多个**（每个都要命中才算过）
NEEDLES = [s.strip() for s in
           (sys.argv[2] if len(sys.argv) > 2 else "俄语").split(",") if s.strip()]
ROOT = Path(__file__).resolve().parents[1]
REPO_SLUG = "Sand-85/vrchat-livetranslate-extra"
WORK = Path(tempfile.mkdtemp(prefix="verify_release_"))

ok: list[str] = []
bad: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (ok if cond else bad).append(f"{name}{('：' + detail) if detail else ''}")
    print(f"  {'✅' if cond else '❌'} {name}{('：' + detail) if detail else ''}")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


print(f"== 复核 {TAG} ==")
print(f"下载目录：{WORK}")
r = subprocess.run(["gh", "release", "download", TAG, "--dir", str(WORK)],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
if r.returncode != 0:
    print("gh release download 失败：", r.stderr)
    raise SystemExit(2)

exes = list(WORK.glob("*.exe"))
check("附件齐了（exe）", bool(exes),
      f"{[p.name + ' ' + f'{p.stat().st_size:,}B' for p in exes]}")

exe = exes[0]
mine = sha256(exe)
# 与 GitHub 的 asset digest 对账：这份是 GitHub 服务端对上传字节算出来的，
# 不经过我们的 CI，才真正能证明「下载到的 === 当初发布的那份」。
_api = subprocess.run(
    ["gh", "api", f"repos/{REPO_SLUG}/releases/tags/{TAG}",
     "--jq", f'.assets[] | select(.name=="{exe.name}") | .digest'],
    capture_output=True, text=True, encoding="utf-8", errors="replace")
declared = (_api.stdout or "").strip().removeprefix("sha256:").lower()
check("sha256 实测 == GitHub 服务端 digest", bool(declared) and declared == mine,
      f"\n     GitHub: {declared or (_api.stderr or '取不到 digest').strip()[:120]!r}"
      f"\n     实测:   {mine}")

# 真跑一次自检
print("  跑 --self-test（可能要十几秒）…")
# ⚠️ 必须用**剥掉 API key 环境变量**的环境跑：应用解析密钥的优先级是
#    配置 → 凭据存储 → 环境变量，而环境变量优先级最低却最容易被顺手注入 ——
#    本地跑过 GUI 测试后，测试里那个假 key 可能还留在会话环境里，一跑自检就是 401
#    （实测踩过：复核因此从 9/9 掉到 7/9，白白怀疑了一遍产物）。
#    剥掉它，应用就会去用自己那份真凭据。
_env = {k: v for k, v in os.environ.items() if k != "DASHSCOPE_API_KEY"}
if _env != dict(os.environ):
    print("  （本机环境里有 DASHSCOPE_API_KEY，已剥掉后运行 —— 用应用自己保存的凭据）")
r = subprocess.run([str(exe), "--self-test"], capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=300, env=_env)
out = (r.stdout or "") + (r.stderr or "")
check("--self-test 退出码 0", r.returncode == 0, f"rc={r.returncode}")
check("--self-test 打出 GUI_SELFTEST_OK", "GUI_SELFTEST_OK" in out,
      out.strip().splitlines()[-1][:90] if out.strip() else "(无输出)")

# 启动日志里的版本行
# 打包 exe 的可写目录是 %APPDATA%\vrchat-livetranslate（不是 exe 旁边、也不是 LOCALAPPDATA），
# 见 vlt/paths.py：exe 旁边放 portable.txt 才会写到 exe 目录。
appdata_logs = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "vrchat-livetranslate" / "logs"
logs = sorted(WORK.rglob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
if appdata_logs.is_dir():
    logs += sorted(appdata_logs.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
ver = f"v{TAG.lstrip('v')}"
found_ver = None
for lg in logs[:20]:
    txt = lg.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"\[startup\]\s*版本\s*(\S+?)（([^）]*)）", txt)
    if m:
        found_ver = (m.group(1), m.group(2), lg)
        break
check("启动日志版本行 = 本次 tag 且标明打包 exe",
      bool(found_ver) and found_ver[0] == ver and "exe" in found_ver[1],
      f"{found_ver[0] if found_ver else '(没找到版本行)'}"
      f"（{found_ver[1] if found_ver else ''}）")

# 新增功能真在产物里。
# ⚠️ 不能直接 grep exe 原始字节：PyInstaller 把 .py 编译后**压缩**塞进 PYZ 归档，
#    源码字符串一个字都搜不到（实测连 v0.0.1 就有的「西班牙语」「赞助」也是 0 命中，
#    拿它当判据会得出「功能没打包进去」的假结论）。必须先解包再搜。
unpacked = WORK / f"{exe.name}_extracted"
xtractor = next((p for p in [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Temp/qrvenv/Scripts/pyinstxtractor-ng.exe",
] if p.exists()), None)
if xtractor is None:
    check("exe 内含新增的「俄语」选项", False, "找不到 pyinstxtractor-ng，无法解包核对（跳过=未验证）")
else:
    # ⚠️ 这个工具**没有** -o 选项（只有 filename / -d / -i），传 -o 会被它忽略参数直接失败；
    #    而且它按 **cwd** 落产物（`<exe名>_extracted/`），必须用 cwd= 指定目录，
    #    否则会把上千个 pyc 解到仓库工作区里。两处都踩过，所以这里校验返回码 + 固定 cwd。
    r = subprocess.run([str(xtractor), exe.name], cwd=str(WORK), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=900)
    check("pyinstxtractor-ng 解包成功", r.returncode == 0 and unpacked.is_dir(),
          f"rc={r.returncode}，产物目录 {'存在' if unpacked.is_dir() else '不存在'}")
    pycs = [p for p in unpacked.rglob("*.pyc")] if unpacked.is_dir() else []
    for nd in NEEDLES:
        hits = [p.relative_to(unpacked).as_posix() for p in pycs
                if nd.encode("utf-8") in p.read_bytes()]
        check(f"exe 内含新增的「{nd}」（解包后在字节码里搜到）", bool(hits),
              f"{len(pycs)} 个 pyc 里命中：{hits[:3]}")
    # 顺带核：包内版本号是这次的、不是上一个版本的残留
    init = next((p for p in pycs if p.name == "__init__.pyc" and "vlt" in p.as_posix()), None)
    if init is not None:
        blob = init.read_bytes()
        want = TAG.lstrip("v").encode()
        check("包内 vlt/__init__.pyc 版本号 = 本次 tag", want in blob and b"0.0.1" not in blob,
              f"含 {TAG.lstrip('v')}：{want in blob}；仍含 0.0.1：{b'0.0.1' in blob}")

# 图标资源比对（SHGetFileInfoW 取图标 → 画到 DIB → 与 assets/app.ico 同尺寸帧比像素）
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int),
                ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", wintypes.WCHAR * 260),
                ("szTypeName", wintypes.WCHAR * 80)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def hicon_to_rgba(hicon, size: int) -> Image.Image:
    hdc_screen = user32.GetDC(0)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, size, size)
    old = gdi32.SelectObject(hdc, bmp)
    user32.DrawIconEx(hdc, 0, 0, hicon, size, size, 0, None, 0x0003)  # DI_NORMAL
    bi = BITMAPINFOHEADER()
    bi.biSize, bi.biWidth, bi.biHeight = ctypes.sizeof(bi), size, -size
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(size * size * 4)
    gdi32.GetDIBits(hdc, bmp, 0, size, buf, ctypes.byref(bi), 0)
    gdi32.SelectObject(hdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(0, hdc_screen)
    return Image.frombuffer("RGBA", (size, size), buf, "raw", "BGRA", 0, 1).convert("RGBA")


shell32.SHGetFileInfoW.restype = ctypes.c_void_p
shell32.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.POINTER(SHFILEINFOW), wintypes.UINT, wintypes.UINT]
user32.DestroyIcon.argtypes = [wintypes.HICON]


def grab_exe_icon(exe_path: str, attempts: int = 5, delay: float = 1.0):
    """取 exe 的图标句柄，失败就重试。

    ⚠️ 这一步会**瞬时**失败：`--self-test` 刚把 exe 加载过，紧接着调 SHGetFileInfoW 会
    「返回成功但 hIcon 为空」。这跟产物好坏无关 —— v0.4.1 发版时就因此误报过一次，
    白查了一轮，所以这里必须重试，别把瞬时失败当成「包坏了」。
    返回 (hIcon 或 None, 尝试次数, 最后一次的返回值)。
    """
    shfi = SHFILEINFOW()
    got = 0
    for i in range(attempts):
        got = shell32.SHGetFileInfoW(exe_path, 0, ctypes.byref(shfi),
                                     ctypes.sizeof(shfi), 0x100)  # SHGFI_ICON
        if got and shfi.hIcon:
            return shfi.hIcon, i + 1, got
        time.sleep(delay)
    return None, attempts, got


# 路径必须是 Windows 原生形式（反斜杠）：正斜杠同样取不到图标。
exe_native = os.path.normpath(os.path.abspath(str(exe)))
hicon, icon_tries, icon_got = grab_exe_icon(exe_native)
ico_path = ROOT / "assets" / "app.ico"
if hicon and ico_path.exists():
    diffs = []
    for size in (32, 16):
        img = hicon_to_rgba(hicon, size)
        ref = Image.open(ico_path)
        ref.size = (size, size)
        ref = ref.convert("RGBA")
        box = [sum(abs(a - b) for a, b in zip(img.getpixel((x, y)), ref.getpixel((x, y))))
               for x in range(size) for y in range(size)]
        diffs.append((size, sum(box) / len(box) / 4))
    user32.DestroyIcon(hicon)
    detail = "；".join(f"{s}px 平均差 {d:.2f}/255" for s, d in diffs)
    if icon_tries > 1:
        detail += f"（第 {icon_tries} 次才取到图标，前 {icon_tries - 1} 次是瞬时失败）"
    check("exe 图标 == assets/app.ico", all(d < 40 for _, d in diffs), detail)
else:
    check("exe 图标 == assets/app.ico", False,
          f"重试 {icon_tries} 次仍取不到图标（got={icon_got}；exe={exe_native}）")

print("\n" + "=" * 64)
print(f"通过 {len(ok)} 项，失败 {len(bad)} 项")
for b in bad:
    print("  ❌", b)
print("下载目录保留在：", WORK)
raise SystemExit(1 if bad else 0)
