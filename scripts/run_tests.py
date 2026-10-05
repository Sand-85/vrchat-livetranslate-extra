#!/usr/bin/env python3
"""仓库内的测试运行器：逐文件跑 `tests/test_*.py`，可选累积覆盖率。

为什么有它：本仓库的用例是**单文件脚本**（`python tests/xxx.py` 直接跑，不是 pytest），
CI 里靠一段 `for` 循环凑合。这段循环以前只存在于 one-off 的外壳脚本里（`out/run_all.sh`），
没入库 —— 谁都能改、谁都不用负责。把它固化成这个脚本，本机与 CI 用**同一套**口径。

用法（务必用仓库自己的 venv 解释器，本机没有 python3）：

    .venv/Scripts/python.exe scripts/run_tests.py                # 全部（默认跳过 test_engine）
    .venv/Scripts/python.exe scripts/run_tests.py --only device  # 只跑文件名含 device 的
    .venv/Scripts/python.exe scripts/run_tests.py --only 'test_room_*'   # 也支持通配
    .venv/Scripts/python.exe scripts/run_tests.py --coverage     # 顺带累积覆盖率摘要
    .venv/Scripts/python.exe scripts/run_tests.py --with-engine  # 连需真 key 的也跑

约定（与 CI 保持一致）：
  * 默认**跳过** `tests/test_engine.py`（要真实 API key 打真会话），并打印原因，**不静默**；
  * 任一用例退出码非 0 → 整体退出码非 0；
  * 覆盖率**不设阈值、不阻断**：只多打印一张 `--show-missing` 摘要；测试结果才是门禁。

⚠️ 已知陷阱：若环境里带着 `DASHSCOPE_API_KEY`（尤其是被别的用例注入过的假 key），
会影响部分用例。跑之前先 `unset DASHSCOPE_API_KEY`（见技能 `vrchat-livetranslate-dev`）。
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"

#: 需要真实 API key / 真会话的用例：默认跳过（CI 同样跳过，理由一致）
ENGINE_TEST = "test_engine.py"
ENGINE_SKIP_REASON = "需要真实 API key（打真会话，属本机实测项）"


def discover() -> list[Path]:
    """全部待跑用例，按文件名排序（顺序固定，报告可复现）。"""
    return sorted(TESTS_DIR.glob("test_*.py"))


def select(tests: list[Path], only: str | None) -> list[Path]:
    if not only:
        return tests
    return [t for t in tests if only in t.name or fnmatch.fnmatch(t.name, only)]


def build_cmd(test: Path, coverage: bool) -> list[str]:
    """允许覆盖解释器的测试命令。

    覆盖率走 `coverage run -a`（append）而不是 `--parallel-mode` + combine：
    本运行器**严格逐个**起子进程（从不开并行 —— 见技能里「全量测试一次只能跑一份」
    的踩坑），单个 append 目标就够，省掉一批 .coverage.* 与 combine 的簿记。
    """
    if coverage:
        return [sys.executable, "-m", "coverage", "run", "-a",
                "--source=vlt", str(test)]
    return [sys.executable, str(test)]


def erase_coverage_data() -> None:
    """清掉上一次的覆盖率数据，避免把不同代码版本的结果混进来。"""
    (ROOT / ".coverage").unlink(missing_ok=True)
    for old in ROOT.glob(".coverage.*"):
        old.unlink(missing_ok=True)


def coverage_summary() -> int:
    """打印覆盖率摘要（`--show-missing`）。**只打印**，结论不影响退出码。"""
    print("\n" + "=" * 62)
    print("覆盖率摘要（非阻断；未设阈值）")
    print("=" * 62)
    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "report", "--show-missing"],
        cwd=ROOT,
    )
    if proc.returncode != 0:
        print(f"（coverage report 退出码 {proc.returncode}：没有任何数据或 coverage 未安装？）")
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    # 逐行刷新：本运行器把子进程输出直接透传到同一个 stdout，而 Python 的 print 在
    # 重定向到文件/CI 日志时是**块缓冲**的 —— 不刷新的话「PASS/SKIP 行 + 覆盖率表」
    # 会晚于（甚至排到）子进程输出之后，日志顺序看着是乱的。
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(
        description="逐文件跑 tests/test_*.py（单文件脚本用例），可选覆盖率摘要。")
    ap.add_argument("--only", metavar="PATTERN",
                    help="只跑文件名匹配 PATTERN 的用例（子串或通配，如 device / 'test_room_*'）")
    ap.add_argument("--with-engine", action="store_true",
                    help=f"连 {ENGINE_TEST} 一起跑（需要真实 API key）")
    ap.add_argument("--coverage", action="store_true",
                    help="累积覆盖率并打印摘要（不设阈值、不阻断）")
    args = ap.parse_args(argv)

    tests = select(discover(), args.only)
    if not tests:
        print(f"没有匹配的用例（tests 目录：{TESTS_DIR}，--only={args.only!r}）")
        return 1

    if args.coverage:
        if subprocess.run([sys.executable, "-m", "coverage", "--version"],
                          stdout=subprocess.DEVNULL).returncode != 0:
            print("--coverage 需要 coverage：先 `pip install -r requirements-dev.txt`")
            return 2
        erase_coverage_data()

    if os.environ.get("DASHSCOPE_API_KEY"):
        print("⚠️ 环境里有 DASHSCOPE_API_KEY —— 仓库已知陷阱：它可能顶掉真凭据、"
              "或让用例拿假 key 假红。建议先 `unset DASHSCOPE_API_KEY` 再跑。\n")

    print(f"解释器：{sys.executable}")
    print(f"用例目录：{TESTS_DIR}")
    print(f"共 {len(tests)} 个文件" + (f"（--only={args.only!r}）" if args.only else ""))
    print("-" * 62)

    n_pass = n_fail = n_skip = 0
    failed: list[str] = []
    for test in tests:
        if test.name == ENGINE_TEST and not args.with_engine:
            n_skip += 1
            print(f"SKIP {test.name} —— {ENGINE_SKIP_REASON}")
            continue
        proc = subprocess.run(build_cmd(test, args.coverage), cwd=ROOT)
        if proc.returncode == 0:
            n_pass += 1
            print(f"PASS {test.name}")
        else:
            n_fail += 1
            failed.append(test.name)
            print(f"FAIL {test.name}（退出码 {proc.returncode}）")

    if args.coverage:
        coverage_summary()

    print("-" * 62)
    print(f"合计：PASS={n_pass} FAIL={n_fail} SKIP={n_skip}")
    if failed:
        print("失败用例：" + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
