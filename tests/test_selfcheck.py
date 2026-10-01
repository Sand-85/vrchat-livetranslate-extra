#!/usr/bin/env python
"""`vlt/selfcheck.py` 的离线测试（只测与平台无关的部分）。

`--verify-imports` 的断言只在**冻结产物**里才有意义（源码运行时 xr 是全平台
安装的，必然报「瘦身未生效」）；真实验收由 `scripts/verify_appimage.py` 驱动
产物跑。这里钉的是「开关语义 / 渲染探针」这些能离线跑的边角：

* 没给开关 / 少了输出路径 → 用法错误返回 2（不许静默退成 0）
* `--verify-render` 出的 PNG 必须真的存在（验收脚本的 ③ 直接依赖它）
"""
from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt import selfcheck as sc  # noqa: E402


def test_usage_error() -> None:
    assert sc.main([]) == 2, "没给开关应返回用法错误 2"
    assert sc.main(["--verify-render"]) == 2, "少了输出路径应返回用法错误 2"


def test_render_probe() -> None:
    with tempfile.TemporaryDirectory(prefix="vlt-selfcheck-") as tmp:
        out = Path(tmp) / "panel.png"
        assert sc.verify_render(str(out)) == 0, "渲染探针应返回 0"
        assert out.exists() and out.stat().st_size > 0, f"没产出图：{out}"


def main() -> int:
    print("test_selfcheck:")
    tests = [test_usage_error, test_render_probe]
    bad = []
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except Exception:  # noqa: BLE001 — 任何一个断言挂都要退出码非 0，但其余用例照跑
            bad.append(fn.__name__)
            traceback.print_exc()
    print("ALL PASSED" if not bad else f"FAILED: {', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
