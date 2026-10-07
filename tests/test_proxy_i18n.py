"""代理状态文案的 **i18n 契约**：代理发出的每条中文模板都必须能翻译。

## 为什么要有它

代理的状态走 `gui._on_proxy_status()`：**日志打中文原文、状态栏查 i18n 词条**
（`t(模板, **params)`）。所以「代理里新加一条文案、忘了加词条」的后果是
**英文界面下状态栏闪中文** —— 而本仓库的 `tests/test_i18n.py` 扫的是界面构造出来的
控件文案，代理的运行期状态**不在它的覆盖里**（测试进程根本不启动代理）。

这条缝只能靠「从源码里数出来」来堵：本文件 **AST 扫描** `vlt/output/micproxy*.py`
（以及 `virtualmic.py` 里属于代理那条 provider 分支的文案）的 `_on_status(...)` 调用，
把中文模板逐条拿来核对：

  1. 4 份词表（en / ja / ko / ru）里**都有**该条目；
  2. 英文译文里**没有汉字**（否则等于漏翻）；
  3. 译文的 `{占位符}` 集合与原文**完全一致**（否则 `t()` 会抛 KeyError 或告警，
     症状是状态栏显示未格式化的生模板）。

顺带钉住一条更宽的：4 份词表的 key 集合必须**完全相同**（防单边漂移）。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.locales import en as _en, ja as _ja, ko as _ko, ru as _ru  # noqa: E402

CATALOGS = {"en": _en.STRINGS, "ja": _ja.STRINGS, "ko": _ko.STRINGS, "ru": _ru.STRINGS}
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
PREFIX = "[proxy] "

#: 代理那条腿的源码。`virtualmic.py` 只取「麦克风代理…」开头的文案 ——
#: 同文件里还有**引擎自建译音输出**那条腿的文案（`译音输出已接到…`），不归本文管。
SOURCES = (
    ("vlt/output/micproxy.py", None),
    ("vlt/output/micproxy_linux.py", None),
    ("vlt/output/virtualmic.py", "麦克风代理"),
)


def _templates(rel: str, only_prefix: str | None) -> set[str]:
    """AST 收集该文件里 `_on_status(level, "<模板>", …)` 的模板字符串。"""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "_on_status"):
            continue
        if len(node.args) < 2:
            continue
        val = node.args[1]
        cands = [val.body, val.orelse] if isinstance(val, ast.IfExp) else [val]
        for c in cands:
            if isinstance(c, ast.Constant) and isinstance(c.value, str):
                text = c.value
                body = text[len(PREFIX):] if text.startswith(PREFIX) else text
                if only_prefix is not None and not body.startswith(only_prefix):
                    continue
                found.add(text)
    return found


def test_catalogs_have_identical_key_sets() -> None:
    """4 份词表的 key 集合必须完全一致（单边漂移 = 某语言少一条）。"""
    base_name, base = next(iter(CATALOGS.items()))
    for name, table in CATALOGS.items():
        missing = set(base) - set(table)
        extra = set(table) - set(base)
        assert not missing, f"{name} 比 {base_name} 少 {len(missing)} 条，例如：{sorted(missing)[:3]}"
        assert not extra, f"{name} 比 {base_name} 多 {len(extra)} 条，例如：{sorted(extra)[:3]}"
    print(f"  ✓ 4 份词表 key 集合一致（各 {len(base)} 条）")


def test_every_proxy_message_is_translated() -> None:
    """代理发出的每条模板：词条在、英文无汉字、占位符与原文一致。"""
    keys: list[str] = []
    for rel, only_prefix in SOURCES:
        for raw in sorted(_templates(rel, only_prefix)):
            body = raw[len(PREFIX):] if raw.startswith(PREFIX) else raw
            keys.append(body)
    assert keys, "没扫到任何 _on_status 模板 —— AST 判据失效了（扫描规则该跟着代码改）"

    problems: list[str] = []
    for key in keys:
        en = CATALOGS["en"].get(key)
        if en is None:
            problems.append(f"en 缺词条：{key!r}")
            continue
        if CJK.search(en):
            problems.append(f"en 译文仍含汉字：{key!r} → {en!r}")
        want = set(PLACEHOLDER.findall(key))
        for name, table in CATALOGS.items():
            if key not in table:
                problems.append(f"{name} 缺词条：{key!r}")
                continue
            got = set(PLACEHOLDER.findall(table[key]))
            if got != want:
                problems.append(f"{name} 占位符不一致：{key!r} 要 {sorted(want)} 实为 {sorted(got)}")
    assert not problems, "代理状态文案的 i18n 契约被破坏：\n  - " + "\n  - ".join(problems)
    print(f"  ✓ 代理的 {len(keys)} 条状态文案：4 语言词条齐、英文无汉字、占位符一致")


def test_status_bar_uses_translation_not_raw_chinese() -> None:
    """★ 端到端一条：英文界面下 `_on_proxy_status()` 推给状态栏的文本不许含汉字。"""
    import queue

    from vlt import i18n

    saved_lang = i18n.current_language()
    i18n.set_language("en")
    try:
        class _FakeGUI:
            """只要能跑 `_on_proxy_status` 的最小替身（真实实现只用到 `_q` 与 `t()`）。"""

            def __init__(self) -> None:
                self._q: queue.Queue = queue.Queue()

        from vlt.gui import TranslationGUI

        gui = _FakeGUI()
        TranslationGUI._on_proxy_status(gui, "info", "[proxy] 已切到「译音」档")
        TranslationGUI._on_proxy_status(
            gui, "warn", "[proxy] 虚拟声卡 #{idx} 打不开：{err}", idx=7, err="boom")
        texts = []
        while not gui._q.empty():
            _tag, _lvl, body = gui._q.get_nowait()
            texts.append(body)
        assert texts, "状态栏一条都没收到"
        bad = [x for x in texts if CJK.search(x)]
        assert not bad, f"英文界面下状态栏出现汉字：{bad!r}"
    finally:
        i18n.set_language(saved_lang)
    print(f"  ✓ 英文界面下状态栏无汉字（{len(texts)} 条：{texts}）")


if __name__ == "__main__":
    print("test_proxy_i18n:")
    test_catalogs_have_identical_key_sets()
    test_every_proxy_message_is_translated()
    test_status_bar_uses_translation_not_raw_chinese()
    print("ALL PASSED")
