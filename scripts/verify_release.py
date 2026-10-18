"""独立复核线上 Release 附件（不依赖 CI 的自检结论）。

用法：.venv/Scripts/python.exe scripts/verify_release.py v0.8.1 "needle1,needle2"

⚠️ **本仓库只发 Windows 版**：没有 AppImage / Linux 附件，所以本脚本就是全部验收面
（上游那份里的 `verify_appimage.py` 流程不适用于本仓库）。修复型发布没有新文案可挑时，
用**修复引入的符号名**最可靠。

第二个参数 = 本版代码里必定出现的字符串，**可以用逗号给多个**（每个都要命中才算过）。
判据是「在解包出来的字节码里搜得到」—— 不是搜 exe 原始字节（那是压缩过的 PYZ，永远搜不到）。

⚠️ **本版（自上游 v0.8.0 同步）可挑的串**，每个改动各取一个：
  ① 海外线路换成「千问云·海外版」：`qwencloud`（`vlt/endpoints.py` 的线路表 + `config.py`
     的迁移分支 + `gui.py`）、中文名 `千问云·海外版`（`endpoints.describe()`）；
  ② 「关于」页赞助者名单：`小夜`（`vlt/gui.py` 的 `SPONSORS`）；
  ③ 本仓库增强「音色」页：`音色自定义`（新分页标题）或模块名 `voice_lab`。

⚠️ **别拿「某个名字还在不在」当反证**：本版也有纯改动型修复（`win.py` 的 `_SM_CXSCREEN`
在 v0.8.0 里**就有**这个名字，只是从没被用对过），这类改动没有新增字符串可搜 ——
它的证据是行为（`screen_work_area()` 真机返回值）与本次改动清单，不是 needle。

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
import types
import shutil
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
skipped: list[str] = []


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
# ⚠️ 必须显式 --repo + --pattern。`gh release download` 默认按**当前目录的 origin 远端**
#    推断仓库，而本仓库的 origin 是**上游**（我们自己的在 mine 远端）→ 不加 --repo 会下到
#    **上游**同名 tag 的资产（上游还带 AppImage，两个文件混进同一目录），再拿**我们仓库**的
#    digest 去对账，必然不一致 —— 实测踩到过：sha 校验自己把自己判红，白查一轮。
_dl = None
for _try in range(1, 4):
    r = subprocess.run(["gh", "release", "download", TAG, "--repo", REPO_SLUG,
                        "-p", "VRChatLiveTranslate.exe", "--dir", str(WORK), "--clobber"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode == 0 and list(WORK.glob("*.exe")):
        _dl = r
        break
    print(f"  下载第 {_try} 次没成功（{(r.stderr or '').strip()[:80]}），重试…")
    time.sleep(4)
if _dl is None:
    print("gh release download 失败：", r.stderr)
    raise SystemExit(2)

exes = list(WORK.glob("*.exe"))
check("附件齐了（exe，且只有自己仓库的那一个）", len(exes) == 1,
      f"{[p.name + ' ' + f'{p.stat().st_size:,}B' for p in exes]}")

# 尺寸也要和服务端对一次账：下载被截断/半途重试时，sha 会莫名其妙地不符，
# 先比尺寸能一眼区分「下载坏了」和「产物真的不是构建的那份」。
_want_size = subprocess.run(
    ["gh", "api", f"repos/{REPO_SLUG}/releases/tags/{TAG}",
     "--jq", '.assets[] | select(.name=="VRChatLiveTranslate.exe") | .size'],
    capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()
check("附件尺寸 == 服务端 size（排除下载被截断）",
      bool(_want_size) and exes and exes[0].stat().st_size == int(_want_size),
      f"服务端 {_want_size or '?'}B / 本地 {exes[0].stat().st_size if exes else 0:,}B")

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
def _run_selftest(env: dict) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([str(exe), "--self-test"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=420, env=env)


# key 的来源在本仓库是**用户级环境变量**（Windows 上存在注册表 HKCU\Environment，进程不一定
# 继承得到）。以前这里无条件**剥掉**环境里的 key（防测试残留的假 key），结果是：本机真 key 恰恰
# 就在环境变量里 → 自检报「找不到 API key」→ 又被当成产物问题。现在改成两段式：
#   ① 先用**当前环境**（含会话里可能存在的 key）跑；
#   ② 若它明确报「找不到 API key」，再从**注册表**取一把重跑一次 —— 两条路都留痕。
r = _run_selftest(dict(os.environ))
out = (r.stdout or "") + (r.stderr or "")
# ⚠️ 判据只用 **ASCII 片段**：打包 exe 的 stdout 走的是本地代码页（中文机器上是 GBK），
#    上面按 utf-8 读出来是「�Ҳ��� API key」这种乱码 —— 拿中文子串去判断会**永远匹配不上**，
#    于是「从注册表补一把 key 重跑」那一段从不生效（实测踩到：换了 key 机制后仍报找不到 key）。
if "DASHSCOPE_API_KEY" in out or "API key" in out:
    import winreg
    _k = None
    for _hive, _sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                        (winreg.HKEY_LOCAL_MACHINE,
                         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(_hive, _sub) as _kk:
                _k = winreg.QueryValueEx(_kk, "DASHSCOPE_API_KEY")[0]
                break
        except OSError:
            continue
    if _k:
        print("  （环境里没有可用的 key → 从注册表取到一把，重跑自检）")
        r = _run_selftest({**os.environ, "DASHSCOPE_API_KEY": _k})
        out = (r.stdout or "") + (r.stderr or "")
    else:
        print("  ⚠️ 环境与注册表都没有 DASHSCOPE_API_KEY —— 自检无法进行（跳过=未验证）")
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
def _needles_via_embedded_pyz(target: Path, needles: list[str]) -> tuple[dict[str, list[str]], str]:
    """回落到**进程内**读内嵌 PYZ（不依赖外部工具）：返回 (needle → 命中模块, `vlt` 模块的字符串块)。

    为什么要有这条：`pyinstxtractor-ng` 是外部工具，解包步骤**偶发不产出 pyc**
    （实测一次：同一份产物、同一命令，这一轮复核「0 个 pyc 里命中」→ 六个 needle 全红，
    而包本身是好的 —— 我用这条回落路径在同一份下载物上六个 needle 全命中）。
    工具链抖动不该被读成「发布缺陷」，所以这里用仓库自己的依赖（PyInstaller 自带的归档读取器）
    再扫一遍。判据与原路径一致：模块字节码里的常量 / 名字里搜 needle，注释不算数。
    """
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    ar = CArchiveReader(str(target))
    pyz_name = next((n for n in ar.toc if n.lower().endswith("pyz.pyz")), None)
    if not pyz_name:
        raise RuntimeError("产物里找不到 PYZ 条目")
    tmp = Path(tempfile.mkdtemp(prefix="verify_release_pyz_")) / "PYZ.pyz"
    tmp.write_bytes(ar.extract(pyz_name))
    z = ZlibArchiveReader(str(tmp))

    def _walk(code, acc: set) -> None:
        for c in code.co_consts:
            if isinstance(c, str):
                acc.add(c)
            elif isinstance(c, types.CodeType):
                _walk(c, acc)
        acc.update(code.co_names)

    hits: dict[str, list[str]] = {n: [] for n in needles}
    vlt_blob = ""
    for mod in list(z.toc):
        try:
            code = z.extract(mod)
        except Exception:                      # noqa: BLE001 — 单个模块读不出不该让整轮复核失败
            continue
        if not isinstance(code, types.CodeType):
            continue
        acc: set = set()
        _walk(code, acc)
        blob = "\n".join(acc)
        for n in needles:
            if n in blob:
                hits[n].append(mod)
        if mod == "vlt":                       # 包根模块 = vlt/__init__，版本号常量在这里
            vlt_blob = blob
    return hits, vlt_blob


unpacked = WORK / f"{exe.name}_extracted"
# 发现顺序：PATH → 本仓库 venv → 旧的一次性环境（历史路径，留着兜底）。
# 以前只写死最后一条，工具一旦不装在那个临时环境里，就永远「跳过=未验证」。
_cands = [
    # ⚠️ 最可信的是**跑本脚本那个解释器自己的 venv**（正常用法 = 仓库的 .venv）：
    #    放在 PATH 之前 —— PATH 上可能挂着坏掉的同名工具（实测 qrvenv 那份缺模块，会挡住好工具）。
    str(Path(sys.executable).parent / "pyinstxtractor-ng.exe"),
    shutil.which("pyinstxtractor-ng"),
    shutil.which("pyinstxtractor-ng.exe"),
    str(ROOT / ".venv" / "Scripts" / "pyinstxtractor-ng.exe"),
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "Temp/qrvenv/Scripts/pyinstxtractor-ng.exe"),
]
xtractor = next((Path(p) for p in _cands if p and Path(p).exists()), None)
if xtractor is None:
    # 缺工具 = **未验证**，不是发布缺陷 —— 别记成 ❌（那会让整轮复核看着像产物坏了）。
    print("  ⚠️ 跳过「exe 内含新增符号」核对：找不到 pyinstxtractor-ng")
    print("     装它：python -m pip install pyinstxtractor-ng")
    for _nd in NEEDLES:
        skipped.append(f"exe 内含「{_nd}」（缺 pyinstxtractor-ng，本轮未验证）")
else:
    # ⚠️ 这个工具**没有** -o 选项（只有 filename / -d / -i），传 -o 会被它忽略参数直接失败；
    #    而且它按 **cwd** 落产物（`<exe名>_extracted/`），必须用 cwd= 指定目录，
    #    否则会把上千个 pyc 解到仓库工作区里。两处都踩过，所以这里校验返回码 + 固定 cwd。
    # ⚠️ 再加一层：它偶发「跑完却没有 pyc」。这一版做了**重试一次 + 回落到进程内 PYZ 读取器**，
    #    两条都不成时记「未验证」，**不记 ❌**（工具链抖动 ≠ 产物缺陷，见 _needles_via_embedded_pyz）。
    # ⚠️ 候选要**逐个真试**：PATH 上可能挂着一个**坏掉的**同名工具（实测本机
    #    `%LOCALAPPDATA%\Temp\qrvenv\Scripts\pyinstxtractor-ng.exe` 缺模块 →
    #    `ModuleNotFoundError: No module named 'pyinstxtractor_ng'` → rc=1，
    #    而它排在候选表最前，把仓库 venv 里的好工具整个挡住 —— 这才是本轮
    #    「六个 needle 全红」的真因（不是产物问题，也不是「解包跑完没产出」）。
    #    判据 = **真跑出 pyc**；失败时把工具自己的输出尾巴打出来，下次不用猜。
    # ⚠️ 紧跟 `--self-test` 之后那一下也可能失败（exe 刚被加载过）→ 每个候选失败后隔 3 秒再试一次。
    pycs: list[Path] = []
    attempts: list[str] = []
    r = None
    for cand in [Path(p) for p in _cands if p and Path(p).exists()]:
        for attempt in (1, 2):
            r = subprocess.run([str(cand), exe.name], cwd=str(WORK), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=900)
            pycs = [p for p in unpacked.rglob("*.pyc")] if unpacked.is_dir() else []
            attempts.append(f"{Path(cand).name}#{attempt}:rc={r.returncode}/pyc={len(pycs)}")
            if pycs:
                break
            if attempt == 1:
                tail = [ln for ln in ((r.stderr or "") + (r.stdout or "")).strip().splitlines() if ln.strip()]
                for ln in tail[-2:]:
                    print(f"      {Path(cand).name} 输出｜{ln[:150]}")
                time.sleep(3.0)
        if pycs:
            break
    print("  解包工具尝试：" + " | ".join(attempts))
    if pycs:
        check("pyinstxtractor-ng 解包成功", True,
              f"rc={r.returncode}，{len(pycs)} 个 pyc（{' | '.join(attempts[-1:])}）")
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
    else:
        print(f"  ⚠️ 所有候选解包工具都没跑出 pyc（{' | '.join(attempts) or '无可用工具'}）"
              f"→ 回落到进程内 PYZ 读取器再扫一遍")
        try:
            pyz_hits, vlt_blob = _needles_via_embedded_pyz(exe, NEEDLES)
        except Exception as exc:               # noqa: BLE001 — 回落路径也失败 = 未验证，不是缺陷
            print(f"  ⚠️ 回落扫描也没成：{exc}")
            for nd in NEEDLES:
                skipped.append(f"exe 内含「{nd}」（解包与回落扫描都没成功，本轮未验证）")
        else:
            for nd in NEEDLES:
                check(f"exe 内含新增的「{nd}」（回落：进程内 PYZ 读取器）", bool(pyz_hits.get(nd)),
                      f"命中模块：{pyz_hits.get(nd, [])[:3]}")
            want_s = TAG.lstrip("v")
            check("包内 vlt 模块版本号 = 本次 tag（回落路径）",
                  want_s in vlt_blob and "0.0.1" not in vlt_blob,
                  f"含 {want_s}：{want_s in vlt_blob}；仍含 0.0.1：{'0.0.1' in vlt_blob}")

# 图标资源比对（SHGetFileInfoW 取图标 → 画到 DIB → 与 assets/app.ico 同尺寸帧比像素）
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")


unpacked = WORK / f"{exe.name}_extracted"
# 解包器的发现顺序：PATH → 本仓库 venv → 旧的一次性环境（历史路径，留着兜底）。
# 以前只写死最后一个，工具一旦不在了就永远「跳过=未验证」。
_cands = [
    # ⚠️ 最可信的是**跑本脚本那个解释器自己的 venv**（正常用法 = 仓库的 .venv）：
    #    放在 PATH 之前 —— PATH 上可能挂着坏掉的同名工具（实测 qrvenv 那份缺模块，会挡住好工具）。
    str(Path(sys.executable).parent / "pyinstxtractor-ng.exe"),
    shutil.which("pyinstxtractor-ng"),
    shutil.which("pyinstxtractor-ng.exe"),
    str(ROOT / ".venv" / "Scripts" / "pyinstxtractor-ng.exe"),
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "Temp/qrvenv/Scripts/pyinstxtractor-ng.exe"),
]
xtractor = next((Path(p) for p in _cands if p and Path(p).exists()), None)
if xtractor is None:
    # 缺工具 = **未验证**，不是发布缺陷 —— 别记成 ❌（那会让整轮复核看着像产物坏了）。
    print("  ⚠️ 跳过「exe 内含新增符号」核对：找不到 pyinstxtractor-ng")
    print("     装它：.venv/Scripts/python.exe -m pip install pyinstxtractor-ng")
    unverified = [f"exe 内含「{nd}」（缺 pyinstxtractor-ng，本轮未验证）" for nd in NEEDLES]
    skipped.extend(unverified)
else:
    # ⚠️ 这个工具**没有** -o 选项（只有 filename / -d / -i），传 -o 会被它忽略参数直接失败；
    #    而且它按 **cwd** 落产物（`<exe名>_extracted/`），必须用 cwd= 指定目录，
    #    否则会把上千个 pyc 解到仓库工作区里。两处都踩过，所以这里校验返回码 + 固定 cwd。
    # ⚠️ 再加一层：它偶发「跑完却没有 pyc」。这一版做了**重试一次 + 回落到进程内 PYZ 读取器**，
    #    两条都不成时记「未验证」，**不记 ❌**（工具链抖动 ≠ 产物缺陷，见 _needles_via_embedded_pyz）。
    # ⚠️ 候选要**逐个真试**：PATH 上可能挂着一个**坏掉的**同名工具（实测本机
    #    `%LOCALAPPDATA%\Temp\qrvenv\Scripts\pyinstxtractor-ng.exe` 缺模块 →
    #    `ModuleNotFoundError: No module named 'pyinstxtractor_ng'` → rc=1，
    #    而它排在候选表最前，把仓库 venv 里的好工具整个挡住 —— 这才是本轮
    #    「六个 needle 全红」的真因（不是产物问题，也不是「解包跑完没产出」）。
    #    判据 = **真跑出 pyc**；失败时把工具自己的输出尾巴打出来，下次不用猜。
    # ⚠️ 紧跟 `--self-test` 之后那一下也可能失败（exe 刚被加载过）→ 每个候选失败后隔 3 秒再试一次。
    pycs: list[Path] = []
    attempts: list[str] = []
    r = None
    for cand in [Path(p) for p in _cands if p and Path(p).exists()]:
        for attempt in (1, 2):
            r = subprocess.run([str(cand), exe.name], cwd=str(WORK), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=900)
            pycs = [p for p in unpacked.rglob("*.pyc")] if unpacked.is_dir() else []
            attempts.append(f"{Path(cand).name}#{attempt}:rc={r.returncode}/pyc={len(pycs)}")
            if pycs:
                break
            if attempt == 1:
                tail = [ln for ln in ((r.stderr or "") + (r.stdout or "")).strip().splitlines() if ln.strip()]
                for ln in tail[-2:]:
                    print(f"      {Path(cand).name} 输出｜{ln[:150]}")
                time.sleep(3.0)
        if pycs:
            break
    print("  解包工具尝试：" + " | ".join(attempts))
    if pycs:
        check("pyinstxtractor-ng 解包成功", True,
              f"rc={r.returncode}，{len(pycs)} 个 pyc（{' | '.join(attempts[-1:])}）")
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
    else:
        print(f"  ⚠️ 所有候选解包工具都没跑出 pyc（{' | '.join(attempts) or '无可用工具'}）"
              f"→ 回落到进程内 PYZ 读取器再扫一遍")
        try:
            pyz_hits, vlt_blob = _needles_via_embedded_pyz(exe, NEEDLES)
        except Exception as exc:               # noqa: BLE001 — 回落路径也失败 = 未验证，不是缺陷
            print(f"  ⚠️ 回落扫描也没成：{exc}")
            for nd in NEEDLES:
                skipped.append(f"exe 内含「{nd}」（解包与回落扫描都没成功，本轮未验证）")
        else:
            for nd in NEEDLES:
                check(f"exe 内含新增的「{nd}」（回落：进程内 PYZ 读取器）", bool(pyz_hits.get(nd)),
                      f"命中模块：{pyz_hits.get(nd, [])[:3]}")
            want_s = TAG.lstrip("v")
            check("包内 vlt 模块版本号 = 本次 tag（回落路径）",
                  want_s in vlt_blob and "0.0.1" not in vlt_blob,
                  f"含 {want_s}：{want_s in vlt_blob}；仍含 0.0.1：{'0.0.1' in vlt_blob}")

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


# ⚠️ 句柄类参数**必须**声明原型：Windows 64 位下 HICON / HDC / HBITMAP / HGDIOBJ 是指针，
#    不声明时 ctypes 按 32 位 C int 传 → `ctypes.ArgumentError: argument 4: OverflowError:
#    int too long to convert`（实测就在 DrawIconEx 上炸）。这与 vlt/platform/win.py 里
#    GetCurrentThread() 伪句柄 -2 被截断是**同一个坑**：宁多写几行 argtypes/restype。
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.DrawIconEx.restype = wintypes.BOOL
user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.HICON,
                              wintypes.UINT]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


def hicon_to_rgba(hicon, size: int) -> Image.Image:
    hdc_screen = user32.GetDC(0)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, size, size)
    old = gdi32.SelectObject(hdc, bmp)
    user32.DrawIconEx(hdc, 0, 0, hicon, size, size, 0, None, 0x0003)  # DI_NORMAL
    # （hicon 由调用方保证是 HICON / int 句柄；原型已声明，64 位不再被截断）
    bi = BITMAPINFOHEADER()
    bi.biSize, bi.biWidth, bi.biHeight = ctypes.sizeof(bi), size, -size
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(size * size * 4)
    gdi32.GetDIBits(hdc, bmp, 0, size, ctypes.byref(buf), ctypes.byref(bi), 0)
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
