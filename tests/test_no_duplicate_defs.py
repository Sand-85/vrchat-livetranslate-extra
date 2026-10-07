#!/usr/bin/env python
"""重复定义守卫：同一个类/模块里**同名 def 出现两次**（后者静默覆盖前者）。

## 为什么要有这个（2026-10-01 的现场）

`vlt/engine.py` 的 `Engine` 类里，`_speak_lock()` 与 `_speak_stream()` **各有两份定义**
（1228/1448、1351/1460），还有一行 `self._speak_lock_obj = None` 在 `__init__` 里写了两遍。
这是某次合并把一整块 B 模式代码贴了两次留下的，**从何时起已不可考**（0.4.3 那版就有）。

危害不是「多几行」：

- Python 取**后一份**，前一份是**死代码** —— 谁去改前面那份，代码看着对、行为纹丝不动，
  典型的「我明明改了却没生效」，排查起来极费时间（本次就差点据它下错结论）；
- 两份实现当时只差文档串措辞，将来很容易只改一份，形成**两个事实**；
- 仓库没有 ruff / pyflakes（CI 只有 `compileall` + 测试 + 凭据扫描），
  `compileall` **不报**重复定义；这类问题此前没有任何一层能拦住。

## 判据（宁缺勿滥）

只看**同一作用域的直接子节点**：模块体的 `def`/`class`、类体的 `def`。
条件定义（写在 `if` / `try` / `TYPE_CHECKING` 里的）不算 —— 那是平台分支的正常写法，
所以 `vlt/platform/__init__.py` 那种按平台导入的分支不会误报。

**不查**：重复的实例属性赋值（`self.x = None` 写两遍是无害的冗余，且合法写法太多，
误报率高）。本次那行重复赋值是人工清理掉的，不进用例。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TARGETS = [ROOT / "vlt", ROOT / "run_gui.py"]


def _dups_in_body(body: list[ast.stmt]) -> list[tuple[str, list[int]]]:
    """直接子节点里的同名定义 → [(名字, [行号, ...]), ...]（只留重复的）。"""
    seen: dict[str, list[int]] = {}
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # property 的 setter/deleter 与 getter **同名是合法成对定义**（`@x.setter`），
            # 不是「后者静默覆盖前者」→ 跳过（否则会把 `@property` + `@x.setter` 误判成重复）。
            if any(isinstance(d, ast.Attribute) and d.attr in ("setter", "deleter")
                   for d in getattr(node, "decorator_list", [])):
                continue
            seen.setdefault(node.name, []).append(node.lineno)
    return [(name, lines) for name, lines in sorted(seen.items()) if len(lines) > 1]


def _check_file(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src, filename=str(path))
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef)):
            continue
        where = path.relative_to(ROOT).as_posix()
        if isinstance(node, ast.ClassDef):
            where += f"::{node.name}"
        for name, lines in _dups_in_body(list(node.body)):
            bad.append(f"{where}.{name} 定义了 {len(lines)} 次：行 {lines}"
                       f"（生效的是行 {lines[-1]}，前面的都是死代码）")
    return bad


def test_no_duplicate_definitions() -> None:
    files: list[Path] = []
    for t in TARGETS:
        files += sorted(t.rglob("*.py")) if t.is_dir() else ([t] if t.exists() else [])
    bad: list[str] = []
    for p in files:
        bad += _check_file(p)
    assert not bad, ("同一作用域里出现了重复定义（后者静默覆盖前者，前面的改动永远不会生效）：\n  "
                     + "\n  ".join(bad))
    print(f"  重复定义守卫：{len(files)} 个文件、无同名重复 def OK")


if __name__ == "__main__":
    print("test_no_duplicate_defs:")
    test_no_duplicate_definitions()
    print("ALL PASSED")
