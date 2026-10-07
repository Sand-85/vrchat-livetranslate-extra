# -*- coding: utf-8 -*-
"""共享沙箱助手：把 config.yaml 重定向到临时目录（内容 = config.example.yaml 模板）。

## 为什么需要它

CONTRIBUTING.md 的纪律是「全量测试一次只跑一份，用例会共用仓库根的 config.yaml」。
CI / 新克隆上 config.yaml 不存在，测试自己照模板生成 → 天然干净；但**开发者本机**
的 config.yaml 是真实个人配置（比如 `ui.lang: en`、`capture.gate_enabled: false`、
`ui.update_ignored` 残留），直接拿来跑测试就会假红 —— 同一份代码在 CI 绿、本机红。

修法不是去改开发者的配置（那是用户资产，里面还有 base_url 等关键设置），
而是让**测试自己**用沙箱配置：临时目录 + 模板内容 + 与 CI 完全一致。

## 用法（测试文件顶部）

    from _cfgbox import sandbox_config
    CONFIG = sandbox_config()          # 返回沙箱 config.yaml 路径

`sandbox_config()` 会同时改写所有 `DEFAULT_CONFIG` 的绑定点，GUI 读写都落到沙箱。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXAMPLE = ROOT / "config.example.yaml"

_SANDBOX: Path | None = None

# DEFAULT_CONFIG 的绑定点（grep 全仓库核实过）：
#   - vlt.config        定义处，load_config / ensure_config 动态读它
#   - vlt.gui           `from .config import DEFAULT_CONFIG`（import 期绑定）
#   - vlt.gui_update    同上（update_ignored 读写走这里）
# 其余模块（gui_audio / gui_desktop / gui_engine）都是 `_cfg_mod.DEFAULT_CONFIG`
# 动态取属性，改 vlt.config 一处即生效。
_BINDERS = ("vlt.config", "vlt.gui", "vlt.gui_update")


def sandbox_config(reset: bool = False) -> Path:
    """返回沙箱 config.yaml 路径；首次调用创建，`reset=True` 重写为模板内容。

    幂等：已加载的 vlt 模块里的 DEFAULT_CONFIG 会被一并指过来；
    尚未 import 的模块（如 vlt.gui 还没加载）在之后 import 时会从
    vlt.config 拿到已改好的值，所以先改 vlt.config 就够了 ——
    但为稳妥仍逐个检查 sys.modules。
    """
    global _SANDBOX
    if _SANDBOX is None:
        _SANDBOX = Path(tempfile.mkdtemp(prefix="vlt-cfg-sandbox-")) / "config.yaml"
    if reset or not _SANDBOX.exists():
        _SANDBOX.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    import vlt.config as _cfg

    _cfg.DEFAULT_CONFIG = _SANDBOX
    for name in _BINDERS[1:]:
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "DEFAULT_CONFIG"):
            mod.DEFAULT_CONFIG = _SANDBOX
    return _SANDBOX
