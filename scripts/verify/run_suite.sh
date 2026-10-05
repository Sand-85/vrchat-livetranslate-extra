#!/usr/bin/env bash
# 跑全量**离线**测试并汇总（跳过需要真 API key 的 test_engine.py）。
#
# 口径（与仓库测试铁律一致）：
#   * 全部测试必须离线 → 用死代理跑一遍（HTTP_PROXY/HTTPS_PROXY 指向 127.0.0.1:1），
#     仍全绿才算真离线；死代理同时能抓出偷偷联网的用例。
#   * unset DASHSCOPE_API_KEY：会话里可能残留测试注入的假 key，会顶掉真凭据（见 skill）。
#   * 一次只跑一份：各用例共用 out/ 沙箱与仓库根的 config.yaml，并发跑会互相踩出假红。
#
# 用法：
#   bash scripts/verify/run_suite.sh                # 跑本仓库
#   bash scripts/verify/run_suite.sh <worktree 路径> [标签]   # 跑别的 worktree（用它自己的 .venv）
#
# 会不会写盘：结果写到 <worktree>/out/<标签>_results.txt，失败用例的完整输出写到
#   <worktree>/out/<标签>_fail_<用例>.log（都在 gitignore 的 out/ 里）。
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_WT="$(cd "$HERE/../.." && pwd)"
WT="${1:-$DEFAULT_WT}"
TAG="${2:-$(basename "$WT")}"

# venv 解释器：Windows 是 Scripts/python.exe，其余是 bin/python
PY="$WT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$WT/.venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "❌ 找不到 $WT 下的 venv 解释器（既非 .venv/Scripts/python.exe 也非 .venv/bin/python）"
    echo "   先在仓库根跑 setup；worktree 里用 cmd /c mklink /J <wt>\\.venv <仓库>\\.venv 接上 venv。"
    exit 2
fi

cd "$WT" || exit 2
mkdir -p out
unset DASHSCOPE_API_KEY
export PYTHONUTF8=1
export HTTP_PROXY=http://127.0.0.1:1 HTTPS_PROXY=http://127.0.0.1:1
RES="$WT/out/${TAG}_results.txt"
: > "$RES"

pass=0; fail=0; skip=0
for t in tests/test_*.py; do
    [ -e "$t" ] || continue
    name=$(basename "$t" .py)
    if [ "$name" = "test_engine" ]; then
        echo "SKIP  $name（需要真 API key）" | tee -a "$RES"
        skip=$((skip+1)); continue
    fi
    o=$( "$PY" "$t" 2>&1 ); rc=$?
    last=$(echo "$o" | grep -E "OK$|ALL PASSED|全部通过|PASSED|跳过" | tail -1)
    if [ $rc -eq 0 ]; then
        echo "PASS  $name  | $last" | tee -a "$RES"
        pass=$((pass+1))
    else
        echo "FAIL  $name  (rc=$rc)" | tee -a "$RES"
        echo "$o" > "$WT/out/${TAG}_fail_$name.log"
        fail=$((fail+1))
    fi
done
echo "---- [$TAG] 合计：PASS=$pass FAIL=$fail SKIP=$skip" | tee -a "$RES"
[ $fail -eq 0 ]
