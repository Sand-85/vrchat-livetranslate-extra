"""GUI 的 `--self-test` 自动化验收（单方向）。

为什么放在这里（而不是留在 gui.py）：这段是纯引擎验收流程（喂测试 PCM → 收集译文），
不碰任何控件；抽出来后 gui.py 的 `TranslationGUI.run_self_test` 只剩一行接线。
"""
from __future__ import annotations

import sys

from .config import load_config
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
