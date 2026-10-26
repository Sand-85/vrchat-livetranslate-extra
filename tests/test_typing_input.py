"""打字输入接线验收（上游 PR #113 的用例，按本仓库拆分后的 API 适配）。

## 上游为什么加它

他们 v0.11.0–v0.12.0 的「打字:」行 100% 不可用：回车 `TypeError: 'NoneType' object is not callable`、
发送按钮 `TypeError: send_typed() missing 3 required positional arguments` —— 根因是 **GUI 拆分后
两个调用点各写一遍参数、都漏了 3 个实参**，而当时的用例没有覆盖「界面回调 → send_typed」这一层，
所以 45/45 全绿而功能是死的。

## 本仓库的差异

我们的 `send_typed(ctx, engines, engine_dirs, set_status_fn)` 签名与调用点都是完整的（没踩上游那个坑），
但**我们踩过同类的另一个坑**（同语种闸把腿停了 → `send_text()` 返回 False → 界面提示「引擎还没就绪」）。
所以这里钉两件事：

1. **行为**：有 mine 腿 → 送出并清空输入框 + info；没有 mine 腿 → warn 不崩；`set_status_fn=None` 也不崩；
2. **接线**：静态扫 `vlt/gui.py` / `vlt/gui_chat.py` 里每一处 `send_typed(` 调用，**必须传 4 个实参** ——
   这正是上游那次故障的形状（漏参数），静态钉住比等真机踩更便宜。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlt.gui_chat import ChatCtx, send_typed  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    results.append((name, ok))
    print(f"  {name}  {'OK' if ok else '✗'}")


class _Var:
    def __init__(self, s: str = "") -> None:
        self.s = s

    def get(self) -> str:
        return self.s

    def set(self, v: str) -> None:
        self.s = v


class _Eng:
    def __init__(self, ok: bool = True) -> None:
        self.ok, self.sent = ok, []

    def send_text(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


def _ctx(text: str = "你好") -> ChatCtx:
    ctx = ChatCtx()
    ctx.text_entry = SimpleNamespace()          # 非 None 即可（send_typed 只判空）
    ctx.text_var = _Var(text)
    return ctx


def _statuses() -> SimpleNamespace:
    box: list[tuple[str, str]] = []
    return SimpleNamespace(box=box, fn=lambda lv, msg, **kw: box.append((lv, msg)))


def test_sends_to_mine_leg() -> bool:
    ctx, eng, st = _ctx(), _Eng(), _statuses()
    send_typed(ctx, [eng], ["mine"], st.fn)          # 返回值由控件层用（这里只验副作用）
    return (eng.sent == ["你好"] and ctx.text_var.get() == ""
            and bool(st.box) and st.box[0][0] == "info")


def test_no_mine_leg_warns() -> bool:
    ctx, eng, st = _ctx(), _Eng(), _statuses()
    send_typed(ctx, [eng], ["theirs"], st.fn)
    return eng.sent == [] and bool(st.box) and st.box[0][0] == "warn"


def test_no_engine_message_differs_from_no_mine_leg() -> bool:
    """没引擎 = 还没开始翻译；有引擎但方向不含「我说」= 方向选错了 —— 两句话不能说成一句（上游 #115）。"""
    ctx1, st1 = _ctx(), _statuses()
    send_typed(ctx1, [], [], st1.fn)                     # 一条腿都没有
    ctx2, st2 = _ctx(), _statuses()
    send_typed(ctx2, [_Eng()], ["theirs"], st2.fn)      # 有腿，但只有 theis 方向
    m1 = st1.box[0][1] if st1.box else ""
    m2 = st2.box[0][1] if st2.box else ""
    return bool(m1) and bool(m2) and m1 != m2 and "我说" in m2 and "我说" not in m1


def test_rejected_send_warns() -> bool:
    """引擎在跑但不收（例如同语种闸把腿停了那种）→ warn，不许静默。"""
    ctx, eng, st = _ctx(), _Eng(ok=False), _statuses()
    send_typed(ctx, [eng], ["mine"], st.fn)
    return eng.sent == ["你好"] and bool(st.box) and st.box[0][0] == "warn"


def test_status_fn_none_is_safe() -> bool:
    """回车那条路历史上的原始故障面：set_status_fn 为 None 时不许抛。"""
    ctx, eng = _ctx(), _Eng()
    try:
        send_typed(ctx, [eng], ["mine"], None)          # type: ignore[arg-type]
    except Exception:                                   # noqa: BLE001
        return False
    return eng.sent == ["你好"]


def test_blank_text_ignored() -> bool:
    ctx, eng, st = _ctx("   "), _Eng(), _statuses()
    send_typed(ctx, [eng], ["mine"], st.fn)
    return eng.sent == [] and st.box == []


def _call_sites(path: Path) -> list[ast.Call]:
    """`send_typed(...)` 的调用点，**排除** build_input_row 里那两个「文档里写明的兜底 lambda」
    （`send_fn or (lambda: send_typed(ctx))` —— 那是故意缺参的兜底，真正建界面时必须传回调）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    in_lambda = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Lambda):
            for sub in ast.walk(node):
                in_lambda.add(id(sub))
    def _is_call(n: ast.AST) -> bool:
        if not isinstance(n, ast.Call):
            return False
        f = n.func
        return ((isinstance(f, ast.Name) and f.id == "send_typed")
                or (isinstance(f, ast.Attribute) and f.attr == "send_typed"))
    return [n for n in ast.walk(tree) if _is_call(n) and id(n) not in in_lambda]


def test_gui_passes_both_callbacks() -> bool:
    """真正建界面时必须把 send_fn / enter_fn 传进去（否则点发送/回车必 TypeError）。"""
    tree = ast.parse((ROOT / "vlt" / "gui.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "build_input_row":
                kw = {k.arg for k in node.keywords}
                return {"send_fn", "enter_fn"} <= kw
    return False


def test_every_call_site_passes_four_args() -> bool:
    """上游那次故障的形状：调用点漏实参。静态钉死（本仓库两个调用点 + 定义本身不算）。"""
    bad: list[str] = []
    total = 0
    for rel in ("vlt/gui.py", "vlt/gui_chat.py"):
        p = ROOT / rel
        if not p.exists():
            continue
        for call in _call_sites(p):
            total += 1
            n = len(call.args) + len(call.keywords)
            if n != 4:
                bad.append(f"{rel}:{call.lineno} 传了 {n} 个实参")
    if total < 2:
        print(f"    （只找到 {total} 处调用点，可能已重构，请复核）")
    for b in bad:
        print(f"    ✗ {b}")
    return total >= 2 and not bad


def main() -> int:
    check("有 mine 腿 → 送出 + 清空 + info", test_sends_to_mine_leg())
    check("没有 mine 腿 → warn 且不送", test_no_mine_leg_warns())
    check("「没引擎」与「方向不含我说」文案不同", test_no_engine_message_differs_from_no_mine_leg())
    check("引擎不收 → warn（不静默）", test_rejected_send_warns())
    check("set_status_fn=None 也不崩", test_status_fn_none_is_safe())
    check("空白文本不送、不报状态", test_blank_text_ignored())
    check("build_input_row 传了 send_fn/enter_fn", test_gui_passes_both_callbacks())
    check("每处 send_typed 调用都传 4 个实参（兜底 lambda 除外）",
          test_every_call_site_passes_four_args())
    bad = [n for n, ok in results if not ok]
    print(f"\n{'ALL PASSED' if not bad else '失败：' + ', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
