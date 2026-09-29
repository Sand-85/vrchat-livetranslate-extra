#!/usr/bin/env python
"""接线守卫：**同一模块内**的调用点 vs 函数签名（关键字实参必须真存在）。

## 为什么要有这个（#12 的现场）

`Engine._feed_audio()` 里写着 `run_loopback(..., gate=self._input_gate)`，而
`run_loopback()` 的形参里**根本没有 `gate`** —— 合并（#4 × #8）时被丢掉了。
后果不是「少一个功能」，而是采集腿一启动就 `TypeError`，被上层吞成状态栏的
「运行错误」，整条腿直接死掉（Windows / Linux 都一样）。

这类漂移为什么此前没有任何一层能拦住：

- 仓库没有接入 ruff / pyflakes（CI 里只有 `compileall` + 测试 + 凭据扫描），
  `compileall` 只查语法，**未定义名字 / 多出来的 kwarg 都不算语法错误**；
- 接线用例（`test_input_gate.py`）为了不碰真硬件，把 `run_loopback` 整个换成了
  `fake_*(**kw)` 替身 —— 什么关键字都照收，签名漂移正好被替身吸收掉。

于是这里用标准库 `ast` 补上这一格：纯离线、两个平台都能跑、不引入新依赖。
判据只管「**同模块内**的模块级函数被按名字直接调用」这一种情形（跨模块要解析
import，收益低、假阳性高）：传的关键字实参必须在签名里；位置实参个数不能超过
能接位置参数的形参个数（有 `*args` 时不判）。

## 它不是什么

不是类型检查，也不是替代 pyflakes。它只钉住「#12 这一类的接线错误」——
调用点与定义漂移。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _Sig:
    """一个模块级函数的「能接什么」摘要。"""

    def __init__(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        a = node.args
        self.posonly = [x.arg for x in a.posonlyargs]
        self.pos = [x.arg for x in a.args]
        self.kwonly = [x.arg for x in a.kwonlyargs]
        self.vararg = a.vararg is not None
        self.kwarg = a.kwarg is not None

    @property
    def positional_capacity(self) -> int:
        return len(self.posonly) + len(self.pos)

    def keywords(self) -> set[str]:
        return set(self.posonly) | set(self.pos) | set(self.kwonly)

    @property
    def all_names(self) -> list[str]:
        return self.posonly + self.pos + self.kwonly


def _module_functions(tree: ast.Module) -> dict[str, _Sig]:
    """只取**模块级**函数（类方法 / 内层函数不在此列，它们的名字不构成同名调用）。"""
    out: dict[str, _Sig] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = _Sig(node)
    return out


def _check_module(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    funcs = _module_functions(tree)
    if not funcs:
        return []
    bad: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        sig = funcs.get(node.func.id)
        if sig is None:
            continue                       # 非本模块的模块级函数 / 局部名字：不判
        where = f"{path.relative_to(ROOT)}:{node.lineno}"
        for kw in node.keywords:
            if kw.arg is None:             # **kwargs 展开：交给运行期
                continue
            if not sig.kwarg and kw.arg not in sig.keywords():
                bad.append(f"{where}: {node.func.id}(..., {kw.arg}=...) —— "
                           f"签名里没有这个关键字（签名：{sig.all_names}）")
        if not sig.vararg:
            n_pos = sum(1 for a in node.args if not isinstance(a, ast.Starred))
            if n_pos > sig.positional_capacity:
                bad.append(f"{where}: {node.func.id}() 传了 {n_pos} 个位置实参，"
                           f"但只接 {sig.positional_capacity} 个"
                           f"（签名：{sig.all_names}）")
    return bad


def test_no_call_site_drifts_from_signature() -> None:
    """`vlt/` 里每一个「同模块直呼」的调用点，都必须与真签名对得上。"""
    bad: list[str] = []
    for p in sorted((ROOT / "vlt").rglob("*.py")):
        bad += _check_module(p)
    assert not bad, ("调用点与函数签名漂移（#12 就是这一类：传了不存在的 gate=，"
                     "函数一跑就 TypeError）：\n  " + "\n  ".join(bad))
    print("  接线守卫：vlt/ 内所有同模块调用点与签名一致 OK")


if __name__ == "__main__":
    print("test_wiring_signatures:")
    test_no_call_site_drifts_from_signature()
    print("ALL PASSED")
