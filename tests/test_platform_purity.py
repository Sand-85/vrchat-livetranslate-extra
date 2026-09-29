#!/usr/bin/env python
"""平台隔离的**源码级**守卫：不必打包就能发现「混进另一个平台实现」的漂移。

## 为什么还要这一层（`check_platform_purity.py` 不是已经查了吗）

`check_platform_purity.py` 查的是**产物**（exe / AppImage），只在打包时/CI 的
package 阶段跑。等它红的时候，往往是「已经构建完了才发现白干一场」。
这里用**同一套判据**（同一份 `FORBIDDEN`、同一个 `_string_blobs`，包括「docstring 不算」
的语义）直接扫仓库源码，把同一件事提前到离线测试里。

## 还钉住一件容易漂移的事：禁列清单 ↔ 构建排除清单必须同步

`FORBIDDEN` 里写了「Windows 不该有 `vlt.output.openxr_overlay`」，而
`scripts/build_exe.py` 的 `EXCLUDE_WIN` 才真正把它排掉 —— 两处**只改一边**就会出现
「断言了一条永远不为真的规则」（禁列写了但没排除 → 打包才炸）或者
「排除了但没人验证」。所以这里做一致性检查。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import check_platform_purity as C  # noqa: E402


def _source_entries(excluded: tuple[str, ...]):
    """把仓库里的 `.py` 当成「产物里的条目」：(模块名, 实现字样)。

    `excluded` 里的模块视为「产物里没有」，直接跳过（与被排除的产物一致）。
    """
    for p in sorted((ROOT / "vlt").rglob("*.py")):
        mod = C._appdir_module_name(p.relative_to(ROOT))
        if any(mod == m or mod.startswith(m + ".") for m in excluded):
            continue
        code = compile(p.read_text(encoding="utf-8"), str(p), "exec")
        for blob in C._string_blobs(code):
            yield mod, blob


def _check_platform(platform: str) -> None:
    rules = C.FORBIDDEN[platform]
    names = [C._appdir_module_name(p.relative_to(ROOT))
             for p in sorted((ROOT / "vlt").rglob("*.py"))]

    # 禁列里的 vlt.* 模块**必须真的存在**（否则规则是死的：写了个不存在的名字，
    # 排除清单跟着一起写错也没人发现）。
    missing = [m for m in rules["modules"] if m.startswith("vlt.") and m not in names]
    assert not missing, (f"{platform} 的禁列里有仓库中不存在的模块"
                         f"（规则形同虚设，八成是改名后没同步）：{missing}")

    hits: dict[bytes, list[str]] = {}
    for mod, blob in _source_entries(tuple(rules["modules"])):
        for needle in rules["strings"]:
            if needle in blob:
                hits.setdefault(needle, []).append(mod)
    assert not hits, (
        f"{platform} 侧出现不该有的**实现**字样（docstring 不计）：\n"
        + "\n".join(f"  {k.decode()} ← {sorted(set(v))}" for k, v in hits.items()))
    print(f"  ✓ {platform} 侧源码干净（{len(rules['modules'])} 条禁列模块 / "
          f"{len(rules['strings'])} 条禁列字样）")


def test_source_is_platform_clean() -> None:
    """两个方向都扫：Linux 侧不含 Windows 实现，Windows 侧不含 Linux 实现。"""
    _check_platform("linux")
    _check_platform("windows")


def test_forbidden_lists_match_build_excludes() -> None:
    """`FORBIDDEN` 的禁列模块必须**真的**被构建脚本排除/删除。"""
    # Windows：EXCLUDE_WIN 必须覆盖 FORBIDDEN["windows"]["modules"] 里 vlt.* 的项
    src = (ROOT / "scripts" / "build_exe.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    exclude_win: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "EXCLUDE_WIN" for t in node.targets):
            exclude_win = [e.value for e in node.value.elts]      # type: ignore[union-attr]
    assert exclude_win, "没解析出 scripts/build_exe.py 的 EXCLUDE_WIN"
    for mod in C.FORBIDDEN["windows"]["modules"]:
        if mod.startswith("vlt."):
            assert mod in exclude_win, (f"{mod} 在 FORBIDDEN[windows] 里，"
                                        f"却没进 build_exe.py 的 EXCLUDE_WIN：{exclude_win}")

    # Linux：build_appimage.sh 的反向删除必须覆盖 FORBIDDEN["linux"]["modules"] 里 vlt.* 的项
    sh = (ROOT / "scripts" / "build_appimage.sh").read_text(encoding="utf-8")
    for mod in C.FORBIDDEN["linux"]["modules"]:
        if mod.startswith("vlt."):
            as_path = mod.replace(".", "/") + ".py"
            assert as_path in sh, (f"{mod} 在 FORBIDDEN[linux] 里，"
                                   f"却没在 build_appimage.sh 里被反向删除（应出现 {as_path}）")
    print("  ✓ 禁列清单与两侧构建排除清单同步")


if __name__ == "__main__":
    print("test_platform_purity:")
    test_source_is_platform_clean()
    test_forbidden_lists_match_build_excludes()
    print("ALL PASSED")
