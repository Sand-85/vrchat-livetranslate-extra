#!/usr/bin/env python
"""构建期把 git 元数据写成 `build/buildinfo.json`，供 `vlt/version.py` 读。

用法：
    python scripts/gen_buildinfo.py [输出路径]      # 默认 <repo>/build/buildinfo.json

## 为什么要「烘焙」而不是运行时问 git

冻结产物（单文件 exe / AppImage）里**没有 `.git`**，`git describe` 跑不了。所以构建期
（仓库里有 `.git`）先算好、写成一个 JSON，构建脚本再用 `--add-data` 打进产物根
（`BUNDLE_DIR`，见 `vlt/paths.py`）。源码运行则没有这个文件，`vlt/version.py` 直接问 git。

只存**原始 describe 串**，格式化逻辑统一留在 `vlt/version.py`（单处维护、离线可测）。
拿不到（浅克隆取不到 tag / 没装 git）就写 `null` → 产物显示 `<版本>+unknown`，绝不假装正式版。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "build" / "buildinfo.json"


def describe() -> str | None:
    """`git describe --tags --match 'v*' --long --dirty`；不可用返回 None。"""
    try:
        proc = subprocess.run(
            ["git", "describe", "--tags", "--match", "v*", "--long", "--dirty"],
            cwd=str(REPO), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        print(f"[buildinfo] ⚠️ 调 git 失败：{exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"[buildinfo] ⚠️ git describe 非零退出（浅克隆 / 无 tag？）："
              f"{proc.stderr.strip()}", file=sys.stderr)
        return None
    return proc.stdout.strip() or None


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    desc = describe()
    out.write_text(json.dumps({"describe": desc}, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    if desc is None:
        print(f"[buildinfo] ⚠️ 拿不到 git 元数据 → 产物会显示 +unknown（{out}）")
    else:
        print(f"[buildinfo] {desc} → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
