#!/usr/bin/env python
"""overlay_pixels：原生叠加窗共用的像素工具（预乘 BGRA / alpha 夹取）。

两个原生后端（Wayland layer-shell / X11 ARGB）共用这里的一份实现 ——
预乘 alpha 抄错一半的表现（半透明发白 / 发暗）在真机上很难定位，所以用
独立用例把字节级语义钉死。

跑法：.venv/bin/python tests/test_overlay_pixels.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.platform.overlay_pixels import clamp01, premultiplied_bgra  # noqa: E402


def test_clamp01() -> None:
    assert clamp01(0.5) == 0.5
    assert clamp01(-1) == 0.0 and clamp01(2) == 1.0
    assert clamp01(None) == 1.0
    assert clamp01("nope") == 1.0                 # 解析不了 → 默认 1.0（不静默变 0）
    assert clamp01("nope", default=0.25) == 0.25
    assert clamp01(float("nan"), default=0.3) == 0.3
    assert clamp01("0.7") == 0.7                  # 数字字符串认
    print("  clamp01：范围夹取 / 垃圾回落默认值 / NaN / 数字字符串 OK")


def test_premultiplied_bgra_layout_and_values() -> None:
    img = Image.new("RGBA", (2, 1))
    img.putpixel((0, 0), (255, 0, 0, 255))        # 不透明红
    img.putpixel((1, 0), (255, 255, 255, 128))    # 半透明白
    b = premultiplied_bgra(img, 1.0)
    assert len(b) == 2 * 4, len(b)
    assert tuple(b[0:4]) == (0, 0, 255, 255), tuple(b[0:4])       # B,G,R,A
    assert tuple(b[4:8]) == (128, 128, 128, 128), tuple(b[4:8])   # 255×128/255 = 128
    print("  预乘 BGRA：通道序 B,G,R,A / 半透明乘 alpha OK")


def test_premultiplied_bgra_global_alpha() -> None:
    img = Image.new("RGBA", (1, 1), (255, 0, 0, 255))
    assert tuple(premultiplied_bgra(img, 0.0)) == (0, 0, 0, 0)
    assert tuple(premultiplied_bgra(img, 0.5)) == (0, 0, 128, 128), \
        tuple(premultiplied_bgra(img, 0.5))
    # 垃圾 alpha → 默认 1.0：配置里一个怪值不该把整块面板弄黑
    assert tuple(premultiplied_bgra(img, "nope")) == (0, 0, 255, 255)
    print("  整层 alpha：0 全零 / 0.5 正确缩放 / 垃圾值回落 1.0 OK")


def main() -> int:
    tests = [test_clamp01,
             test_premultiplied_bgra_layout_and_values,
             test_premultiplied_bgra_global_alpha]
    print("test_overlay_pixels:")
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
