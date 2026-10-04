"""平台门面：宿主子进程的环境清理 + `open_path` 失败可见（官方 AppImage 回归）。

## 背景（2026-10 真机故障，官方发布版「打开日志文件夹」点了没反应）

PyInstaller 会把包内目录**前置**进 `LD_LIBRARY_PATH`（原值存 `LD_LIBRARY_PATH_ORIG`），
所有子进程都会继承。官方 AppImage（Ubuntu 构建）→ `xdg-open` 拉起宿主 thunar →
thunar 先加载包内旧版 `libfontconfig`（2.15，缺 `FcConfigSetDefaultSubstitute`，
而 Arch 的 libpangoft2 需要）→ `symbol lookup error` 秒退；stderr 又被 /dev/null
吞掉、退出码没人看 → 用户只看到「点了没反应」。本地构建因包内库与宿主同源才没事。

## 本用例守住三件事（全部打桩，不真开文件管理器）

1. `child_env()` 还原/剔除 `LD_LIBRARY_PATH` 的各种情形；
2. `open_path` 把清理过的 env 传给子进程；
3. `open_path` 对「秒退非零」抛出带 stderr 的异常（不再静默），对「还活着」正常返回。

Windows 上本机制不适用（那侧是 `os.startfile`），整个文件明确跳过。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt import platform as platform_mod  # noqa: E402

_ENV_KEYS = ("LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG")


def _save_env() -> dict:
    return {k: os.environ.get(k) for k in _ENV_KEYS}


def _restore_env(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _save_frozen() -> tuple[bool, object]:
    return hasattr(sys, "frozen"), getattr(sys, "frozen", None)


def _restore_frozen(saved: tuple[bool, object]) -> None:
    had, val = saved
    if had:
        sys.frozen = val                     # type: ignore[attr-defined]
    else:
        sys.__dict__.pop("frozen", None)


class _FakePopen:
    """按用例定制的 `subprocess.Popen` 替身：记录参数、按需装死/装活。"""

    def __init__(self, *, rc=None, timeout=False, stderr_text=b"") -> None:
        self.rc = rc
        self.timeout = timeout
        self.stderr_text = stderr_text
        self.captured: dict = {}

    def __call__(self, argv, **kwargs):
        self.captured["argv"] = argv
        self.captured["kwargs"] = kwargs
        return self

    def wait(self, timeout=None):
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd="xdg-open", timeout=timeout)
        if self.stderr_text:
            kwargs = self.captured.get("kwargs", {})
            errfile = kwargs.get("stderr")
            if errfile is not None:
                errfile.write(self.stderr_text)
        return self.rc


def test_child_env_restores_original() -> None:
    """有 ORIG（AppImage 里用户原本也设了 LD_LIBRARY_PATH）→ 还原为原值。"""
    saved = _save_env()
    try:
        os.environ["LD_LIBRARY_PATH"] = "/bundle/_internal:/usr/lib"
        os.environ["LD_LIBRARY_PATH_ORIG"] = "/usr/lib"
        env = platform_mod.child_env()
        assert env["LD_LIBRARY_PATH"] == "/usr/lib", \
            f"应还原为 ORIG，实际 {env.get('LD_LIBRARY_PATH')!r}"
        assert "LD_LIBRARY_PATH_ORIG" not in env, "标记变量不该传给子进程"
    finally:
        _restore_env(saved)
    print("  ✓ child_env：有 ORIG → 还原原值")


def test_child_env_drops_empty_orig() -> None:
    """ORIG 为空串（用户原值为空）→ 删掉变量，而不是留一个空值。"""
    saved = _save_env()
    try:
        os.environ["LD_LIBRARY_PATH"] = "/bundle/_internal:"
        os.environ["LD_LIBRARY_PATH_ORIG"] = ""
        env = platform_mod.child_env()
        assert "LD_LIBRARY_PATH" not in env, \
            f"原值为空时应删除，实际 {env.get('LD_LIBRARY_PATH')!r}"
    finally:
        _restore_env(saved)
    print("  ✓ child_env：ORIG 为空 → 删除变量")


def test_child_env_drops_in_frozen_without_orig() -> None:
    """frozen 且没有 ORIG（用户本来就没设）→ 删掉引导器刚塞进去的包内路径。"""
    saved_env, saved_frozen = _save_env(), _save_frozen()
    try:
        os.environ["LD_LIBRARY_PATH"] = "/bundle/_internal"
        os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
        sys.frozen = True                    # type: ignore[attr-defined]
        env = platform_mod.child_env()
        assert "LD_LIBRARY_PATH" not in env, \
            f"frozen 无 ORIG 时应删除，实际 {env.get('LD_LIBRARY_PATH')!r}"
    finally:
        _restore_env(saved_env)
        _restore_frozen(saved_frozen)
    print("  ✓ child_env：frozen 无 ORIG → 删除包内路径")


def test_child_env_keeps_source_run_value() -> None:
    """源码运行没有 ORIG：用户自己设的路径必须原样保留，且返回的是副本。"""
    saved_env, saved_frozen = _save_env(), _save_frozen()
    try:
        os.environ["LD_LIBRARY_PATH"] = "/custom/lib"
        os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
        sys.__dict__.pop("frozen", None)
        env = platform_mod.child_env()
        assert env["LD_LIBRARY_PATH"] == "/custom/lib", \
            f"源码运行不该动用户的值，实际 {env.get('LD_LIBRARY_PATH')!r}"
        env["LD_LIBRARY_PATH"] = "/mutated"
        assert os.environ["LD_LIBRARY_PATH"] == "/custom/lib", "必须是副本"
    finally:
        _restore_env(saved_env)
        _restore_frozen(saved_frozen)
    print("  ✓ child_env：源码运行 → 用户值原样保留（副本）")


def test_open_path_passes_clean_env() -> None:
    """`open_path` 把清理过的 env 传给子进程；还活着 = 已交接 → 正常返回。"""
    saved = _save_env()
    fake = _FakePopen(timeout=True)
    orig_popen = platform_mod.subprocess.Popen
    try:
        os.environ["LD_LIBRARY_PATH"] = "/bundle/_internal:/usr/lib"
        os.environ["LD_LIBRARY_PATH_ORIG"] = "/usr/lib"
        platform_mod.subprocess.Popen = fake
        platform_mod.open_path("/tmp/vlt-open-path-test")
    finally:
        platform_mod.subprocess.Popen = orig_popen
        _restore_env(saved)

    assert fake.captured["argv"] == ["xdg-open", "/tmp/vlt-open-path-test"], \
        f"argv 不对：{fake.captured['argv']!r}"
    env = fake.captured["kwargs"].get("env")
    assert env is not None, "必须显式传 env（否则继承包内库路径）"
    assert env["LD_LIBRARY_PATH"] == "/usr/lib", \
        f"子进程 env 没清理：{env.get('LD_LIBRARY_PATH')!r}"
    assert fake.captured["kwargs"].get("stderr") is not None, "stderr 必须被捕获（失败可见）"
    print("  ✓ open_path：子进程拿到清理后的 env；还活着 → 正常返回")


def test_open_path_raises_on_quick_failure() -> None:
    """秒退非零 → 带 stderr 抛 RuntimeError（以前 DEVNULL + 不看退出码 = 全静默）。"""
    fake = _FakePopen(rc=4, stderr_text=b"no method available for opening '/tmp/vlt-x'\n")
    orig_popen = platform_mod.subprocess.Popen
    try:
        platform_mod.subprocess.Popen = fake
        try:
            platform_mod.open_path("/tmp/vlt-x")
        except RuntimeError as exc:
            assert "no method available" in str(exc), f"异常里应带 stderr：{exc}"
            assert "4" in str(exc), f"异常里应带退出码：{exc}"
        else:
            raise AssertionError("秒退非零必须抛异常，不许静默")
    finally:
        platform_mod.subprocess.Popen = orig_popen
    print("  ✓ open_path：秒退非零 → 带 stderr 抛异常（失败可见）")


def main() -> int:
    if platform_mod.IS_WINDOWS:
        print("test_platform_child_env:")
        print("  (Windows 上不适用 LD_LIBRARY_PATH 机制，跳过 = 未验证)")
        return 0
    tests = [
        test_child_env_restores_original,
        test_child_env_drops_empty_orig,
        test_child_env_drops_in_frozen_without_orig,
        test_child_env_keeps_source_run_value,
        test_open_path_passes_clean_env,
        test_open_path_raises_on_quick_failure,
    ]
    print("test_platform_child_env:")
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
