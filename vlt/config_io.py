"""config.yaml 就地改写工具：`vlt/gui.py` 与 `vlt/update_check.py` 共用的单一真相。

为什么不 import gui 来复用：gui 的 import 链会拉起 tkinter / devices / engine，
而 update_check 必须保持纯逻辑、离线可测。所以这三个函数住在这个**无依赖**
（只用到 re / yaml / pathlib）的小模块里，双方都从这儿拿。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


def _yaml_set_in_text(text: str, path: list[str], value: str) -> str:
    """在 YAML 文本里**就地**改一个叶子值，保留注释、空行与键的顺序。

    为什么不用 `yaml.safe_load` + `yaml.dump` 整文件重写：那会抹平所有注释和顺序
    （实测把一份带完整中文说明的 config.yaml 变成一坨没有注释的键值对，键还被按字母重排）。
    配置文件是给人读的，程序存个设置不该毁掉它的可读性。
    找不到路径就返回原文——宁可这次没生效，也不退化成整文件重写。
    """
    lines = text.split("\n")

    def _span(key: str, indent: int, lo: int, hi: int):
        head = re.compile(rf"^(\s*){re.escape(key)}:\s*$")
        for i in range(lo, hi):
            m = head.match(lines[i])
            if m is None or len(m.group(1)) != indent:
                continue
            sub_hi = hi
            for j in range(i + 1, hi):
                if lines[j].strip() and not lines[j].startswith(" " * (indent + 1)):
                    sub_hi = j
                    break
            return i, sub_hi
        return None

    lo, hi, indent = 0, len(lines), 0
    for key in path[:-1]:
        got = _span(key, indent, lo, hi)
        if got is None:
            return text
        lo, hi = got[0] + 1, got[1]
        indent += 2

    leaf = path[-1]
    pat = re.compile(rf"^(\s*){re.escape(leaf)}:(\s*)([^#\n]*)(\s*#.*)?$")
    for i in range(lo, hi):
        m = pat.match(lines[i])
        if m and len(m.group(1)) == indent:
            comment = (m.group(4) or "").strip()
            new_line = f"{m.group(1)}{leaf}: {value}" + (f"   {comment}" if comment else "")
            # ⚠️ 关键：如果这一项的旧值是**多行块**（块序列 / 嵌套映射），必须把子行一并删掉，
            # 否则会留下孤立的 `- 0.0` 之类 → 整个文件变成非法 YAML。
            # 用户实测踩过：旧版整文件 yaml.dump 会把 `pos: [0.0, 0.06, 0.02]` 写成
            #    pos:
            #    - 0.0
            #  而本函数当时只换了 `pos:` 那一行，热重载就报
            #  `expected <block end>, but found '-'`，界面上拖滑块完全没效果。
            j = i + 1
            while j < hi and lines[j].strip():
                stripped = lines[j].lstrip()
                ind_j = len(lines[j]) - len(stripped)
                # 更深的缩进 = 属于本键的块；同级但以 "- " 开头 = 块序列（PyYAML 默认就不缩进）
                if ind_j > indent or (ind_j == indent and stripped.startswith("- ")):
                    j += 1
                    continue
                break
            del lines[i + 1:j]
            lines[i] = new_line
            return "\n".join(lines)
    lines.insert(hi, f"{' ' * indent}{leaf}: {value}")
    return "\n".join(lines)


def _yaml_set_or_create(text: str, path: list[str], value: str) -> str:
    """就地改一个（可嵌套的）叶子值；**父级键缺失时按缩进补建整条链**。

    与 `_yaml_set_in_text` 同一取舍（保住注释 / 空行 / 键顺序，只在写前校验合法 YAML），
    区别只在「老配置里整段不存在」：旧函数遇到父键找不到就返回原文（静默 no-op），
    本函数把缺失的层级补到**已存在的父块末尾**（顶层缺就补到文件末尾）。
    场景：GUI 新加的打字译音音色写 `text_input.tts.voice`，而用户的旧 `config.yaml`
    是从更早的模板生成的、根本没有 `text_input` 段 —— 不补建的话这个设置永远存不下去。
    已存在的路径（如 `session.voice`）走就地替换，行为与旧函数一致。
    """
    lines = text.split("\n")

    def _find(key: str, indent: int, lo: int, hi: int):
        """在 [lo,hi) 里找缩进为 indent 的 `key:` → (行号, 子区间 lo, 子区间 hi)；没有返回 None。"""
        head = re.compile(rf"^(\s*){re.escape(key)}:(\s*)([^#\n]*)(\s*#.*)?$")
        for i in range(lo, hi):
            s = lines[i].lstrip()
            if not s or s.startswith("#"):
                continue
            if len(lines[i]) - len(s) != indent:
                continue
            if head.match(lines[i]) is None:
                continue
            child_hi = hi
            for j in range(i + 1, hi):
                s2 = lines[j].lstrip()
                if not s2 or s2.startswith("#"):
                    continue
                if len(lines[j]) - len(s2) <= indent:
                    child_hi = j
                    break
            return i, i + 1, child_hi
        return None

    def _walk(key_path: list[str], indent: int, lo: int, hi: int) -> None:
        got = _find(key_path[0], indent, lo, hi)
        if got is None:                          # 父级缺失 → 在本区间末尾补建整条链
            at = hi
            if at == len(lines) and lines and lines[-1] == "":
                at = len(lines) - 1               # 保住文件末尾的那个换行
            block: list[str] = []
            for d, k in enumerate(key_path):
                pad = " " * (indent + 2 * d)
                block.append(f"{pad}{k}: {value}" if d == len(key_path) - 1 else f"{pad}{k}:")
            lines[at:at] = block
            return
        i, clo, chi = got
        if len(key_path) == 1:                    # 叶子：就地替换，连带删掉旧的块子行（同旧函数）
            m = re.compile(rf"^(\s*){re.escape(key_path[0])}:(\s*)([^#\n]*)(\s*#.*)?$").match(lines[i])
            comment = (m.group(4) or "").strip() if m else ""
            lines[i] = f"{' ' * indent}{key_path[0]}: {value}" + (f"   {comment}" if comment else "")
            j = i + 1
            while j < chi:
                s = lines[j].lstrip()
                if not s:
                    j += 1
                    continue
                ind_j = len(lines[j]) - len(s)
                if ind_j > indent or (ind_j == indent and s.startswith("- ")):
                    j += 1
                    continue
                break
            del lines[i + 1:j]
            return
        _walk(key_path[1:], indent + 2, clo, chi)

    _walk(path, 0, 0, len(lines))
    return "\n".join(lines)


def _write_config_text(path: Path, text: str) -> None:
    """写回配置前先验证仍是合法 YAML。

    宁可这次改动不生效（调用方会 catch 并打印），也**绝不能把用户的配置写坏** ——
    配置坏了影响的是启动，比一个滑块没生效严重得多。
    """
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"生成的新配置不是合法 YAML，已放弃写入：{exc}") from exc
    path.write_text(text, encoding="utf-8")


def _fmt_scalar(x) -> str:  # noqa: ANN001, ANN202
    """None → null；float → 紧凑写法（0.24 而不是 0.24000000000000002）；
    list/tuple → 行内流式 `[a, b]`（更新忽略列表等列表值要用）。"""
    if x is None:
        return "null"
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, float):
        return f"{x:g}"
    if isinstance(x, (list, tuple)):
        return "[" + ", ".join(_fmt_scalar(i) for i in x) + "]"
    return str(x)
