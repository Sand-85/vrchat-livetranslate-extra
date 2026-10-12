#!/usr/bin/env python
"""版本展示口径的守卫（离线、不依赖真实 git）。

## 钉住什么

`vlt/version.py` 把「正式发布 / 测试构建 / 未知」三种身份映射成展示串。规则一旦漂移，
用户看到的版本号就会骗人（例如把源码构建显示成正式版）。所以这里喂**假的** describe
串，逐条断言 `parse_describe()` 的解析与 `display`：

  * HEAD 正好在 tag 且干净 → 正式 → 干净版本号；
  * 有提交差 / dirty     → `X.Y.Z+<n>.g<hash>[.dirty]`；
  * 拿不到元数据          → `X.Y.Z+unknown`（绝不假装正式）。

⚠️ 本文件**不碰真实 git**：只测纯函数 `parse_describe`，所以任何机器/浅克隆都稳定。
真正的 git 调用（`_git_describe` / 烘焙文件）由构建脚本与真机启动覆盖。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt import version as V  # noqa: E402
from vlt.version import BuildMeta, parse_describe  # noqa: E402

BASE = "0.10.0"          # 纯夹具基准（parse 用例的输入）；与真实 __version__ 无关


def test_official_shapes() -> None:
    """正好在 tag：`v0.10.0` 与 `--long` 的 `v0.10.0-0-g<hash>` 都算正式。"""
    for describe in ("v0.10.0", "0.10.0", "v0.10.0-0-gb04129b"):
        meta = parse_describe(describe, BASE)
        assert meta.official, f"{describe!r} 应判为正式：{meta}"
        assert meta.display == BASE, f"{describe!r} 展示应为 {BASE}：{meta.display!r}"
    print("  正好在 tag（含 --long 的 -0-）→ 正式、干净版本号 OK")


def test_test_build_shapes() -> None:
    """有提交差 → `X.Y.Z+<n>.g<hash>`；dirty 追加 `.dirty`。"""
    meta = parse_describe("v0.10.0-7-gb04129b", BASE)
    assert not meta.official
    assert (meta.count, meta.hash, meta.dirty) == (7, "b04129b", False), meta
    assert meta.display == "0.10.0+7.gb04129b", meta.display

    dirty = parse_describe("v0.10.0-7-gb04129b-dirty", BASE)
    assert dirty.dirty and not dirty.official
    assert dirty.display == "0.10.0+7.gb04129b.dirty", dirty.display

    # 正好在 tag 但工作区脏 → 也是测试构建（不能当正式版发布）
    on_tag_dirty = parse_describe("v0.10.0-0-gb04129b-dirty", BASE)
    assert not on_tag_dirty.official
    assert on_tag_dirty.display == "0.10.0+0.gb04129b.dirty", on_tag_dirty.display
    print("  有提交差 / dirty → +<n>.g<hash>[.dirty] OK")


def test_unknown_fallback() -> None:
    """拿不到 git 元数据（None / 空串 / 乱码）→ `+unknown`，绝不假装正式。"""
    for describe in (None, "", "   ", "not-a-describe", "v0.10.0-7-XXXX", "v0.10"):
        meta = parse_describe(describe, BASE)
        assert not meta.known, f"{describe!r} 应判为「无元数据」：{meta}"
        assert not meta.official, describe
        assert meta.display == "0.10.0+unknown", f"{describe!r} → {meta.display!r}"
    print("  无元数据 → +unknown（不假装正式版）OK")


def test_base_is_independent_of_tag() -> None:
    """展示串的「底」用 `__version__`（base 参数），提交数/哈希来自 describe。"""
    meta = parse_describe("v0.10.0-3-gdeadbee", "0.11.0")
    assert meta.display == "0.11.0+3.gdeadbee", meta.display
    print("  底版本用 __version__、后缀用 git 元数据 OK")


def test_dataclass_defaults() -> None:
    """默认 BuildMeta 是不可用状态（防呆：忘了 known 就显 +unknown，而不是正式版）。"""
    m = BuildMeta(base=BASE)
    assert not m.known and not m.official and m.display == "0.10.0+unknown"
    print("  BuildMeta 默认值 = 未知（安全兜底）OK")


def test_baked_buildinfo_is_read() -> None:
    """冻结产物路径：`BUNDLE_DIR/buildinfo.json` 里的 describe 要被读进来。

    这是构建脚本 `--add-data` 落地的形态（产物根 = `BUNDLE_DIR`）。源码运行没有这个文件、
    走 git；这里用临时目录 + 打桩 `BUNDLE_DIR` 模拟冻结产物，两态都覆盖。
    """
    saved_dir = V.BUNDLE_DIR
    tmp = Path(tempfile.mkdtemp(prefix="vlt-buildinfo-"))
    # CI 会设 VLT_VERSION_NO_GIT / VLT_VERSION_DESCRIBE —— 本测试要验「烘焙文件」路径，
    # 必须先把环境覆盖摘掉，否则会被 env 抢先生效（见 build_meta 的优先级）。
    saved_env = {k: os.environ.pop(k, None) for k in (V._ENV_DESCRIBE, V._ENV_NO_GIT)}
    try:
        # 测试构建：有提交差
        (tmp / V.BUILDINFO_NAME).write_text(
            json.dumps({"describe": "v0.10.0-5-gabc1234"}), encoding="utf-8")
        V.BUNDLE_DIR = tmp
        V.build_meta.cache_clear()
        # 期望串从 __version__ 派生（原写死版本号 → 每次发版必红；本文件 71 行的契约也是「底用 __version__」）
        assert V.display_version() == f"{V.__version__}+5.gabc1234", V.display_version()
        assert not V.is_official()

        # 正式构建：正好在 tag → 干净版本号
        (tmp / V.BUILDINFO_NAME).write_text(
            json.dumps({"describe": "v0.10.0-0-gabc1234"}), encoding="utf-8")
        V.build_meta.cache_clear()
        assert V.display_version() == V.__version__, V.display_version()
        assert V.is_official()

        # 烘焙为 null（构建时拿不到 git）→ unknown，且**权威、不回退去问 git**：
        # 把 git 探针换成「一被调用就炸」，若实现偷偷回退这里会立刻红。
        (tmp / V.BUILDINFO_NAME).write_text(
            json.dumps({"describe": None}), encoding="utf-8")
        V.build_meta.cache_clear()
        saved_git = V._git_describe
        V._git_describe = lambda: (_ for _ in ()).throw(
            AssertionError("describe=null 时不应回退去问 git"))
        try:
            assert V.display_version() == f"{V.__version__}+unknown", V.display_version()
        finally:
            V._git_describe = saved_git
    finally:
        V.BUNDLE_DIR = saved_dir
        V.build_meta.cache_clear()
        for k, v in saved_env.items():
            if v is not None:
                os.environ[k] = v
    print("  烘焙 buildinfo.json（测试/正式/unknown）读取正确 OK")


def test_env_override_skips_git() -> None:
    """环境变量覆盖（CI/测试）：`VLT_VERSION_DESCRIBE` / `VLT_VERSION_NO_GIT` 都不起 git。

    这是 Windows CI 卡死的修复点：测试期绝不 spawn `git describe`。把 git 探针换成
    「一被调用就炸」，若实现偷偷回退这里立刻红。
    """
    saved_dir = V.BUNDLE_DIR
    tmp = Path(tempfile.mkdtemp(prefix="vlt-noenv-"))     # 空目录：没有 buildinfo.json
    saved_git = V._git_describe
    saved_env = {k: os.environ.get(k) for k in (V._ENV_DESCRIBE, V._ENV_NO_GIT)}
    V._git_describe = lambda: (_ for _ in ()).throw(
        AssertionError("env 覆盖时不应调用 git"))
    try:
        V.BUNDLE_DIR = tmp
        os.environ.pop(V._ENV_NO_GIT, None)

        # ① VLT_VERSION_DESCRIBE 直接当 describe
        os.environ[V._ENV_DESCRIBE] = "v0.10.0-3-gdeadbee"
        V.build_meta.cache_clear()
        assert V.display_version() == f"{V.__version__}+3.gdeadbee", V.display_version()

        # ② VLT_VERSION_NO_GIT=1 且无 describe → unknown（权威、不问 git）
        os.environ.pop(V._ENV_DESCRIBE, None)
        os.environ[V._ENV_NO_GIT] = "1"
        V.build_meta.cache_clear()
        assert V.display_version() == f"{V.__version__}+unknown", V.display_version()
    finally:
        V._git_describe = saved_git
        V.BUNDLE_DIR = saved_dir
        V.build_meta.cache_clear()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("  env 覆盖（DESCRIBE / NO_GIT）都不起 git OK")


if __name__ == "__main__":
    print("test_version:")
    test_official_shapes()
    test_test_build_shapes()
    test_unknown_fallback()
    test_base_is_independent_of_tag()
    test_dataclass_defaults()
    test_baked_buildinfo_is_read()
    test_env_override_skips_git()
    print("ALL PASSED")
