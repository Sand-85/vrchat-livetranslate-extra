"""GUI 的 `--self-test` 自动化验收（单方向）。

为什么放在这里（而不是留在 gui.py）：这段是纯引擎验收流程（喂测试 PCM → 收集译文），
不碰任何控件；抽出来后 gui.py 的 `TranslationGUI.run_self_test` 只剩一行接线。
"""
from __future__ import annotations

import sys
import time

from .config import Direction, load_config
from .engine import Engine, EngineEvents
from .paths import BUNDLE_DIR


def run_self_test() -> int:
    pcm_path = BUNDLE_DIR / "testdata" / "zh_test_16k.pcm"
    if not pcm_path.exists():
        print(f"GUI_SELFTEST_FAIL: 测试音频不存在 {pcm_path}", file=sys.stderr)
        return 1

    results: list[tuple] = []

    def on_text(src, txt, final):
        results.append((src, txt, final))

    def on_status(level, msg):
        print(f"[selftest][{level}] {msg}")

    cfg = load_config()
    cfg.directions["mine"].source_lang = "zh"
    cfg.directions["mine"].target_lang = "en"

    events = EngineEvents(on_text=on_text, on_status=on_status)
    engine = Engine(
        cfg=cfg, direction="mine", source=f"pcm:{pcm_path}",
        sinks={"chatbox"}, events=events, dry_run=True,
    )
    engine.start()
    engine.join(timeout=60)
    engine.stop(timeout=5)

    has_source = any(r[0].strip() for r in results)
    has_target = any(r[1].strip() for r in results)
    if has_source and has_target:
        print("GUI_SELFTEST_OK")
        return 0
    print(f"GUI_SELFTEST_FAIL: source={has_source} target={has_target} rows={len(results)}",
          file=sys.stderr)
    return 1


def run_self_test_dual(gui) -> int:
    """双向同时验收（两个 PCM 驱动两个引擎）。"""
    zh = BUNDLE_DIR / "testdata" / "zh_test_16k.pcm"; en = BUNDLE_DIR / "testdata" / "en_test_16k.pcm"
    for p in (zh, en):
        if not p.exists(): print(f"GUI_SELFTEST_DUAL_FAIL: 测试音频不存在 {p}", file=sys.stderr); return 1
    cfg = load_config(); cfg.directions.setdefault("mine", Direction()); cfg.directions.setdefault("theirs", Direction())
    cfg.output.setdefault("audio", {})["enabled"] = False   # 我们的：自检不碰真实音频设备
    cfg.directions["mine"].source_lang = "zh"; cfg.directions["mine"].target_lang = "en"
    cfg.directions["theirs"].source_lang = "en"; cfg.directions["theirs"].target_lang = "zh"
    engines = []
    for who, direction, pcm_path in (("mine", "mine", zh), ("theirs", "theirs", en)):
        def on_text(src, txt, final, who=who): gui._add_text(src, txt, final, who=who)
        events = EngineEvents(on_text=on_text, on_status=lambda lvl, msg, who=who: print(f"[dualtest][{who}][{lvl}] {msg}"))
        eng = Engine(cfg=cfg, direction=direction, source=f"pcm:{pcm_path}", sinks={"chatbox"}, events=events, dry_run=True)
        engines.append(eng); eng.start(); time.sleep(0.3)
    for eng in engines: eng.join(timeout=90)
    for eng in engines: eng.stop(timeout=5)
    mine = [b for b in gui._bubbles if b.who == "mine"]; theirs = [b for b in gui._bubbles if b.who == "theirs"]
    if mine and theirs: print(f"GUI_SELFTEST_DUAL_OK mine={len(mine)} theirs={len(theirs)}"); return 0
    print(f"GUI_SELFTEST_DUAL_FAIL: mine={len(mine)} theirs={len(theirs)}", file=sys.stderr); return 1
