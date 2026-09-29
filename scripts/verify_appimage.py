#!/usr/bin/env python
"""AppImage 的**独立验收脚本**（既能本地跑，也进 CI）。

用法：
    python scripts/verify_appimage.py dist/VRChatLiveTranslate-x86_64.AppImage
    python scripts/verify_appimage.py <img> --no-render      # 跳过离线渲染
    python scripts/verify_appimage.py <img> --no-font        # 跳过 Tk 字体检查

退出码：0 = 全过；1 = 有检查没过；2 = 用法/产物问题。

## 为什么单独一个脚本（而不是塞在 build_appimage.sh 里）

构建脚本只该负责「把东西做出来」；**验收**要能被单独调用（CI 直接跑它就够了，
不必重新构建一遍），也要能对着**别人给的** AppImage 跑。构建脚本第 5 步只是转调这里。

## 四项检查（都不碰 VR 运行时、不碰音频设备）

1. **平台纯度** —— 转调 `scripts/check_platform_purity.py --platform linux`：
   模块级（不许有 `vlt.platform.win` / openvr 后端 / Windows 依赖）+ 实现字样级。
   这是「Linux 上只用 OpenXR、Windows 上只用 SteamVR」这条假设的**红灯门禁**。
2. **包内导入 + 反向排除** —— 用**包内那份解释器**（不是宿主的）导入核心模块，
   并断言 Windows 独占模块**不在**包里（`vlt.platform.win` / `vlt.output.openvr_overlay`）。
3. **离线渲染一帧** —— `python -m vlt.output.overlay --out <png>`：验证 Pillow + 字体
   链路真的能出图。不需要显示器。
4. **Tk 中日韩字体可见** —— 只有**有显示器**时才跑（无显示器 → 明确提示「跳过 = 未验证」）。
   CI 里用 xvfb-run 让它真的跑起来；本地不想开显示就不跑。构建脚本换的那份 Xft 版 Tk
   对不对，只有这一步能验。

⚠️ 全程只读：解包到临时目录、跑完即删。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

FAILED: list[str] = []


def _step(title: str) -> None:
    print(f"\n=== {title} ===")


def _fail(msg: str) -> None:
    FAILED.append(msg)
    print(f"  ❌ {msg}")


def _ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def _find(root: Path, pattern: str) -> Path | None:
    hits = sorted(root.glob(pattern))
    return hits[0] if hits else None


def verify(img: Path, *, do_render: bool = True, do_font: bool = True,
           expect_slim: bool = True) -> bool:
    from check_platform_purity import appdir, check as purity_check

    # ① 平台纯度（模块 + 字样）
    _step("① 平台纯度（Linux 判据）")
    if not purity_check(img, "linux"):
        FAILED.append("平台纯度检查未通过")

    with appdir(img) as root:
        py = _find(root, "usr/python/bin/python3.*")
        app = root / "usr" / "app"
        site = _find(root, "usr/python/lib/python*/site-packages")
        if py is None or site is None or not app.is_dir():
            _fail(f"AppDir 结构不符合预期（py={py}, app={app}, site={site}）")
            return False
        env = dict(os.environ, PYTHONPATH=f"{app}:{site}", PYTHONUTF8="1")

        def _run(args: list[str], timeout: int = 240) -> subprocess.CompletedProcess:
            # ⚠️ `-P` + cwd=AppDir 都是必须的：`python -c/-m` 会把**当前目录**放在
            #    sys.path 最前，而 PYTHONPATH 排在它后面 —— 从仓库里跑本脚本时，
            #    `import vlt` 会解析到**仓库源码**而不是包内代码，检查等于白做
            #    （这个坑真踩过：② 一直报「包里还有 vlt.platform.win」，
            #     其实是它读的是仓库里那份）。
            return subprocess.run([str(py), "-P", *args], env=env, cwd=str(root),
                                  capture_output=True, text=True, timeout=timeout)

        # ② 包内导入 + 反向排除 + 瘦身
        _step("② 包内导入 + 反向排除 + xr 瘦身（用包内解释器）")
        slim_expect = (
            "site = Path(sysconfig.get_paths()['purelib'])\n"
            "assert (site/'xr/library/x86_64/libopenxr_loader.so').exists(), \\\n"
            "    '缺 pyopenxr 在 linux/x86_64 上要 dlopen 的 loader'\n"
            "assert (site/'xr/api_layer/x86_64').is_dir(), \\\n"
            "    '缺 api_layer/x86_64 目录（import xr 时 expose_packaged_api_layers 需要）'\n"
        ) if expect_slim else ""
        slim_gone = (
            "gone = ['xr/api_layer/android','xr/api_layer/aarch64','xr/api_layer/win32',\n"
            "        'xr/api_layer/windows','xr/api_layer/linux','xr/library/aarch64',\n"
            "        'xr/library/android','xr/library/win32']\n"
            "left = [p for p in gone if (site/p).exists()]\n"
            "assert not left, f'xr 瘦身没生效（这些还在包里）：{left}'\n"
        ) if expect_slim else ""
        probe = (
            "import importlib.util as u, sysconfig\n"
            "from pathlib import Path\n"
            "import vlt.gui, vlt.engine, vlt.output.openxr_overlay, vlt.platform\n"
            "for bad in ('vlt.platform.win', 'vlt.output.openvr_overlay'):\n"
            "    assert u.find_spec(bad) is None, f'Linux 产物里不该有 {bad}'\n"
            + slim_expect + slim_gone +
            "print('OK')\n"
        )
        res = _run(["-c", probe])
        if res.returncode == 0:
            tail = "，且 Windows 独占模块不在包里" + ("，xr 只留 linux x86_64" if expect_slim else "")
            _ok("核心模块导入正常" + tail)
        else:
            _fail(f"包内导入/反向排除/瘦身检查失败：{(res.stderr or res.stdout).strip()[:400]}")

        # ③ 离线渲染一帧（不碰 VR / 音频 / 显示器）
        if do_render:
            _step("③ 离线渲染一帧（不需要显示器）")
            out = Path(tempfile.mkdtemp(prefix="vlt-verify-render-")) / "panel.png"
            res = _run(["-m", "vlt.output.overlay", "--out", str(out)])
            if res.returncode == 0 and out.exists() and out.stat().st_size > 0:
                _ok(f"渲染成功（{out.stat().st_size} 字节）")
            else:
                _fail(f"离线渲染失败：{(res.stderr or res.stdout).strip()[:400]}")
        else:
            _step("③ 离线渲染（已按 --no-render 跳过）")

        # ④ Tk 字体（需要显示器；没有就明确「跳过 = 未验证」）
        if do_font:
            _step("④ Tk 中日韩字体可见（需要显示器）")
            font_probe = (
                "import sys\n"
                "try:\n"
                "    import tkinter as tk, tkinter.font as tkfont\n"
                "    root = tk.Tk(); root.withdraw()\n"
                "except Exception as exc:\n"
                "    print(f'SKIP:{type(exc).__name__}'); sys.exit(2)\n"
                "fams = list(tkfont.families(root)); root.destroy()\n"
                "cjk = [f for f in fams if any(k in f for k in ('CJK','Source Han','Noto Sans SC','WenQuanYi'))]\n"
                "print(f'FAMS:{len(fams)} CJK:{len(cjk)}' + (f' FIRST:{cjk[0]}' if cjk else ''))\n"
                "sys.exit(0 if cjk else 1)\n"
            )
            res = _run(["-c", font_probe])
            line = (res.stdout or "").strip().splitlines()[-1] if res.stdout.strip() else ""
            if res.returncode == 0:
                _ok(f"中日韩字体正常（{line}）")
            elif res.returncode == 2:
                print(f"  ⚠️ 无显示器 → **跳过 = 未验证**（{line}）；"
                      f"CI 里用 xvfb-run 会真的跑")
            else:
                _fail(f"Tk 起来了但看不到中日韩字体（界面会是豆腐块）：{line}")
        else:
            _step("④ Tk 字体检查（已按 --no-font 跳过）")

    print()
    if FAILED:
        print(f"== 验收失败：{len(FAILED)} 项 ==")
        for m in FAILED:
            print(f"   ✗ {m}")
        return False
    print("== 验收通过 ==")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="AppImage 独立验收（平台纯度 + 导入 + 渲染 + 字体）")
    ap.add_argument("image", type=Path, help="AppImage 产物路径")
    ap.add_argument("--no-render", action="store_true", help="跳过离线渲染")
    ap.add_argument("--no-font", action="store_true", help="跳过 Tk 字体检查")
    ap.add_argument("--allow-fat", action="store_true",
                    help="允许未瘦身（跳过「xr 只留 linux x86_64」的断言）")
    args = ap.parse_args()

    if not args.image.exists():
        print(f"找不到产物：{args.image}")
        return 2
    if not args.image.name.endswith(".AppImage"):
        print(f"这不是 AppImage：{args.image.name}")
        return 2
    ok = verify(args.image, do_render=not args.no_render, do_font=not args.no_font,
                expect_slim=not args.allow_fat)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
