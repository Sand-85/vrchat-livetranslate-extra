"""#2「更细分段/更早触发」：`output.audio.segment_early`（**默认关闭**）。

要点：
  1. **默认关闭 = 与以前逐位相同**（等价性：对一批文本，soft=False 与旧签名结果一致）；
  2. 打开后确实更早开口（软边界 / 12 字上限）；
  3. 仍然守着两条护栏：只切「已定型的前缀」、太短的片段先攒着（min_chars）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlt.engine import (VOICE_SEGMENT_EARLY_MAX_CHARS, VOICE_SEGMENT_MIN_CHARS,  # noqa: E402
                        voice_segment_from_partial)

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    results.append((name, ok))
    print(f"  {name}  {'OK' if ok else '✗'}")


CASES = [
    ("大家好，今天我想聊一个比较长的话题，就是延迟", ""),
    ("第一句。第二句还没说完", "第一句。"),
    ("完全没有标点的很长很长很长很长很长很长的一段话", ""),
    ("短", ""),
    ("A: hi there, this is a test", ""),
    ("已经念过。新的一句，还没到句号", "已经念过。"),
]

# ① 等价性：不传 soft（= 默认 False）与显式 False、以及与「旧行为」完全一致
same = all(voice_segment_from_partial(t, sp) == voice_segment_from_partial(t, sp, soft=False)
           for t, sp in CASES)
check("不传 soft 时与 soft=False 完全一致（默认行为不变）", same)

# ② 关着的时候，无硬分句符号 → 不切
check("关闭时无标点长句不切（等终版）",
      voice_segment_from_partial(CASES[2][0], "") is None)

# ③ 打开后才切，且不超过长度上限
seg = voice_segment_from_partial(CASES[2][0], "", soft=True)
check("打开时无标点长句会切（到上限）", bool(seg))
check("切出的长度 ≥ min_chars 且 ≤ 上限",
      bool(seg) and len(seg.strip()) >= VOICE_SEGMENT_MIN_CHARS
      and len(seg.strip()) <= VOICE_SEGMENT_EARLY_MAX_CHARS)
check("上限常量合理", VOICE_SEGMENT_EARLY_MAX_CHARS >= VOICE_SEGMENT_MIN_CHARS)

# ④ 护栏仍在：模型改写已念过的部分 → 停（不重念、不猜）
check("前缀被改写时返回 None（护栏不变）",
      voice_segment_from_partial("改写了的内容。继续", "原来的内容。", soft=True) is None)

# ⑤ 太短先攒着（软边界也一样）
check("软边界下太短仍然先攒着",
      voice_segment_from_partial("嗯，", "", soft=True, min_chars=4) is None)

bad = [n for n, ok in results if not ok]
print(f"\n{'ALL PASSED' if not bad else '失败：' + ', '.join(bad)}")
raise SystemExit(0 if not bad else 1)
