"""专有词库在界面文本框里的**文本格式**（`原文=译名` 一行一条）。

为什么放在这里（而不是留在 gui.py）：
  · 这一层是纯字符串处理，离线可测（见 tests/test_glossary.py），
    与 Tk 无关 —— 单独成模块后 gui.py 只负责「取文本框内容 / 回写文本框」。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 专有词库的文本格式
# 界面上一行一条：`原文=译名`。为什么用这个格式而不是 JSON / YAML：
#   · 用户是主播，不是程序员 —— 敲 `原文=译名` 不需要懂缩进和引号；
#   · 一行一条，删一条就删一行，改坏了也不影响别人（JSON 少个逗号整段报废）；
#   · 与 config.yaml 里的映射表一一对应，肉眼能对上。
# 解析纪律：以 `#` 开头的行是注释、空行忽略；**只按第一个等号切**，
# 这样译名里带 `=`（或中文全角 `＝`）也不会切错。

def _iter_glossary_lines(text: str):
    """逐行分类：产出 `(行号, 原文, 译名, 忽略原因, 原样内容)`。

    - 空行 / `#` 注释 = 正常跳过（原因 `""`）
    - 原文/译名为 `None` 且原因非空 = 用户**写了内容但格式看不懂** —— 以前这类行被静默丢掉，
      用户写了 `原文：译名` 只会觉得「保存没反应」，所以要能报出来。
    """
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            yield lineno, None, None, "", line
            continue
        if "=" in line:
            src, _, tgt = line.partition("=")
        elif "＝" in line:
            # 容忍全角等号（中文输入法下极易打出来），否则用户会以为「保存没反应」
            src, _, tgt = line.partition("＝")
        else:
            yield lineno, None, None, "缺少等号", line
            continue
        src, tgt = src.strip(), tgt.strip()
        if not (src and tgt):
            yield lineno, None, None, "等号有一侧是空的", line
            continue
        yield lineno, src, tgt, "", line


def _parse_glossary_lines(text: str) -> dict[str, str]:
    """把界面文本框的内容解析成 {原文: 译名}（纯函数，离线可测）。

    重复的原文以**后出现的为准**（用户在下面写一条更具体的覆盖上面那条，
    与「后写覆盖先写」的直觉一致）。
    """
    out: dict[str, str] = {}
    for _lineno, src, tgt, _reason, _raw in _iter_glossary_lines(text):
        if src:
            out[src] = tgt
    return out


def _glossary_line_issues(text: str) -> list[tuple[int, str]]:
    """格式看不懂的行 `[(行号, 原样内容)]`（纯函数，离线可测）。

    界面拿它给用户一句提示：以前这些行是**静默**丢掉的，用户很容易以为「保存没反应」。
    """
    return [(n, raw) for n, src, _tgt, reason, raw in _iter_glossary_lines(text) if reason]


def _glossary_to_lines(mapping: dict[str, str] | None) -> list[str]:
    """反向：{原文: 译名} → 界面文本框的行（保持配置里的顺序）。"""
    return [f"{k}={v}" for k, v in (mapping or {}).items()]
