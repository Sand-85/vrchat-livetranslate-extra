#!/usr/bin/env python3
"""检查仓库内 Markdown 的**相对**链接与锚点是否指得着。

用法：
    python scripts/check_doc_links.py                # 查本仓库
    python scripts/check_doc_links.py <另一个仓库根>   # 查别的仓库（自测用）

退出码：0 = 全部指得着；1 = 有坏链（CI 里当红灯用）。

## 为什么要这个脚本

「文档归整 docs/」那次搬目录（commit 333694a 用 git mv 把 9 份 md 从根目录挪进 docs/），
只改了**一部分**相对链接：有的地方漏了、有的地方多补了一层 `../`、有的地方把
`docs/GUIDE.linux.md` 留在了 docs/ 里面（于是解析成 `docs/docs/GUIDE.linux.md`）。
结果就是**一大批「指向其他文档」的链接指空** —— 而断链不会让任何测试变红，
只有读者点进去才发现。

搬文件、改文件名、改小节标题都会制造这类坏链。所以把它变成**可执行的断言**，
本地和 CI 都跑一遍。

## 判据

* 文件：相对链接（含图片）的目标文件必须存在。
* 锚点：`xxx.md#anchor` 的 anchor 必须能在目标文件的标题里找到，按 GitHub 的
  slug 规则算（小写 → 去掉标点 → 空格转 `-`）。非 .md 目标只查存在性。
* 范围：只查 `git ls-files` 里**已跟踪**的 `*.md`。`build/` 下是 vendored 的 tcltk
  源码文档（在 .gitignore 里），不属于本项目文档，天然不参与判据。
* 外部链接（`http(s)://` / `mailto:` 等）**只跳过、不联网**：CI 里跑联网检查会因
  网络抖动变红，那是假红灯的常见来源。
"""
from __future__ import annotations

import argparse
import functools
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

# `](target)` —— 链接和图片都是这个形状。不依赖前面的 `[...]`，这样嵌套写法
# `[![badge](img.png)](docs/GUIDE.md)` 里的**两个**目标都能被单独取到。
LINK = re.compile(r"\]\(\s*(<[^>]*>|[^)\s]+)\s*\)")
# ATX 标题（`#` 后必须跟空白，避免把 `#tag` 之类误当标题）
ATX = re.compile(r"^#{1,6}\s+(.*?)\s*$")
# 围栏代码块（``` / ~~~）：里面的 `# 注释` 是 YAML 注释，不是标题，不能进锚点集合
FENCE = re.compile(r"^\s*(?:```|~~~)")
# 带 scheme 的绝对地址（http:、mailto:、data: …）以及协议相对地址（//host）
ABSOLUTE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*:|//)")


def slug(text: str) -> str:
    """按 GitHub 的规则把标题正文算成锚点。

    GitHub 的算法是「转小写 → 去掉标点 → 空格转连字符」，且**保留**中日韩文字与
    下划线。Python 的 `\\w` 正好等价于「字母 / 数字 / 下划线」（含 Unicode 字母），
    所以用它来「去标点」能同时干掉 ASCII 标点（`. ( ) :`）和全角标点
    （`、 ： ？`）—— 后者是中文标题里最常见的那类。
    """
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text)


def headings_of(path: Path) -> set[str]:
    """文件里所有标题对应的锚点（跳过围栏代码块内部）。"""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()

    anchors: set[str] = set()
    in_fence = False
    for line in lines:
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = ATX.match(line)
        if m:
            anchors.add(slug(m.group(1)))
    return anchors


@functools.lru_cache(maxsize=None)
def anchors(path: Path) -> frozenset[str]:
    return frozenset(headings_of(path))


def tracked_markdown(root: Path) -> list[Path]:
    """已跟踪的 *.md（相对 root）。取不到 git 时退化为遍历目录。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.md"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        hidden = {".git", ".venv", "__pycache__"}
        return sorted(
            p for p in root.rglob("*.md") if not hidden & set(p.relative_to(root).parts)
        )
    return [root / name for name in out.decode("utf-8", "surrogateescape").split("\0") if name]


def check_file(path: Path, root: Path) -> list[str]:
    problems: list[str] = []
    here = path.parent
    text = path.read_text(encoding="utf-8", errors="replace")

    in_fence = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        for m in LINK.finditer(line):
            raw = m.group(1)
            if raw.startswith("<") and raw.endswith(">"):
                raw = raw[1:-1]
            if not raw or ABSOLUTE.match(raw):
                continue

            target, _, anchor = raw.partition("#")
            dest = path if not target else here / unquote(target)
            where = f"{path.relative_to(root)}:{lineno}"

            if not dest.exists():
                problems.append(f"{where}  链接目标不存在: {raw}")
                continue

            if not anchor or dest.suffix != ".md":
                continue

            wanted = unquote(anchor).lower()
            have = anchors(dest)
            if wanted not in have:
                near = sorted(a for a in have if a.startswith(wanted[:4]))
                hint = f"  相近锚点: #{near[0]}" if near else ""
                problems.append(f"{where}  锚点不存在: {raw}{hint}")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 Markdown 相对链接与锚点")
    parser.add_argument(
        "root",
        nargs="?",
        default=Path(__file__).resolve().parent.parent,
        type=Path,
        help="仓库根目录（默认：本脚本所在仓库）",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    docs = tracked_markdown(root)
    if not docs:
        print(f"⚠️ 在 {root} 没找到已跟踪的 *.md（什么都没查）")
        return 0

    problems: list[str] = []
    for doc in docs:
        problems.extend(check_file(doc, root))

    if problems:
        print("\n" + "=" * 62)
        print("❌ 文档链接检查失败：")
        print("=" * 62)
        for p in problems:
            print(f"  {p}")
        print(f"\n共 {len(problems)} 处坏链（检查了 {len(docs)} 份文档）。")
        print("相对链接是相对**当前文件所在目录**解析的：")
        print("  · docs/ 里的文件指同目录的文档，直接写 `GUIDE.en.md`，不要再加 `docs/` 或 `../`")
        print("=" * 62 + "\n")
        return 1

    print(f"✅ 文档链接检查通过（{len(docs)} 份文档）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
