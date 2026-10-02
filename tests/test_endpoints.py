"""服务线路（千问云 / 阿里云百炼·国际版）出网端点派生的验收测试。

本轮新增 `vlt/endpoints.py` 作为**地址的单一真相源**：`session.base_url` 说了算，
另外两条 HTTP 端点（打字翻译 chat/completions、打字译音 multimodal）一律从它的
host 派生。这个脚本把「派生对不对、脏值会不会静默、密钥分槽互不干扰、老配置零变化」
这几件事钉死 —— 全程离线（HTTP opener 与密钥目录都被替换成替身，不连服务端、不碰
用户真实 `config.yaml`、不碰真实 `~/.vrchat-livetranslate`）。

跑法：`.venv/Scripts/python.exe tests/test_endpoints.py`，全绿末行打印 `OK`，
有失败则非 0 退出（照本仓库 `tests/test_textin.py` 的单文件脚本惯例）。
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import wave
from pathlib import Path
from urllib.error import HTTPError

import yaml

ROOT = Path(__file__).resolve().parents[1]          # 不写死本机路径：CI / 别人克隆后也能跑
sys.path.insert(0, str(ROOT))

# 界面/日志文案会跟随系统语言（CI 与外国机器是英文系统）。本脚本里 `credentials.mask_key`
# 等会走 t()，钉死成 zh 让输出在不同机器上稳定（产品代码不依赖这个补丁）。
import vlt.i18n as _i18n  # noqa: E402
_i18n.detect_system_language = lambda: "zh"

import vlt.config as config_mod  # noqa: E402
import vlt.credentials as credentials  # noqa: E402
import vlt.endpoints as endpoints  # noqa: E402
import vlt.textin as textin  # noqa: E402
import vlt.tts as tts_mod  # noqa: E402
from vlt.session.base import SessionConfig  # noqa: E402

# 断言计数：每条 check 都打印一行，末尾汇总；有任何一条为假 → 非 0 退出。
_CHECKS = {"n": 0, "bad": 0}


def check(cond: bool, label: str) -> bool:
    _CHECKS["n"] += 1
    if cond:
        print(f"  ✓ {label}")
    else:
        _CHECKS["bad"] += 1
        print(f"  ✗ {label}")
    return cond


def eq(got: object, want: object, label: str) -> bool:
    """相等断言：把 got/want 都打出来，失败时一眼看出差在哪。"""
    return check(got == want, f"{label}：got={got!r} want={want!r}")


# ---------------------------------------------------------------- 测试替身


class FakeResp:
    """最小响应替身：可 read()、可当上下文管理器（with ... as r）。"""

    def __init__(self, body: str) -> None:
        self._buf = io.BytesIO(body.encode("utf-8"))

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """替掉模块级 `_opener`，把请求对象留下来断言（`req.full_url` 就是实际出网地址）。"""

    def __init__(self, body: str = "") -> None:
        self.body, self.req = body, None

    def open(self, req, timeout=None):  # noqa: ANN001
        self.req = req
        return FakeResp(self.body)


def _wav24k(seconds: float = 0.2) -> bytes:
    """造一段 24kHz 单声道 s16le WAV —— TTS 服务端返回的就是这个形态。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        n = int(24000 * seconds)
        w.writeframes(b"".join(
            struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / 24000)))
            for i in range(n)))
    return buf.getvalue()


# ---------------------------------------------------------------- 1) 千问云（写死期望值）


def test_qianwen_endpoints() -> None:
    """case 1：千问云四条值**整串写死**比对（不复算 —— 复算等于没测）。"""
    base = "wss://maas.qianwenaiapi.com/api-ws/v1/realtime"
    eq(endpoints.default_base_url("qianwen"), base, "default_base_url(qianwen)")
    eq(endpoints.host_of(base), "maas.qianwenaiapi.com", "host_of(qianwen)")
    eq(endpoints.chat_url(base),
       "https://maas.qianwenaiapi.com/compatible-mode/v1/chat/completions",
       "chat_url(qianwen)")
    eq(endpoints.multimodal_url(base),
       "https://maas.qianwenaiapi.com/api/v1/services/aigc/multimodal-generation/generation",
       "multimodal_url(qianwen)")


# ---------------------------------------------------------------- 2) 百炼派生


def test_bailian_derivation() -> None:
    """case 2：百炼 + 新加坡 + 空间 ID llm-abc123 —— 占位符被实参替换，两端点从 host 派生。"""
    base = endpoints.default_base_url("bailian_intl", region="ap-southeast-1")
    eq(base,
       "wss://{workspace_id}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime",
       "default_base_url(bailian_intl) 保留字面量占位符")
    eq(endpoints.resolve_base_url(base, "llm-abc123"),
       "wss://llm-abc123.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime",
       "resolve_base_url 把 {workspace_id} 换成实参")
    eq(endpoints.chat_url(base, "llm-abc123"),
       "https://llm-abc123.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
       "chat_url(bailian_intl)")
    eq(endpoints.multimodal_url(base, "llm-abc123"),
       "https://llm-abc123.ap-southeast-1.maas.aliyuncs.com"
       "/api/v1/services/aigc/multimodal-generation/generation",
       "multimodal_url(bailian_intl)")


# ---------------------------------------------------------------- 3) 地域（表驱动 + 已收口的老地域）


def test_all_regions() -> None:
    """case 3：地域 → host 的 region 段正确、后缀是百炼域名。

    ⚠️ 2026-10-02 收口：**可选地域只剩新加坡**（语音链路国际站只有新加坡有部署，
    见 `endpoints.REGIONS` 的说明）。但 host 派生逻辑对**已知的老地域**仍然成立 ——
    它们只是不可选/会被守卫拦下，不在这里被改写（地域进 host，改写等于打错域名）。
    """
    eq([rid for rid, _ in endpoints.REGIONS], ["ap-southeast-1"], "REGIONS 表与简报一致")
    host_regions = [rid for rid, _ in endpoints.REGIONS] + list(endpoints.UNSUPPORTED_REGIONS)
    for rid in host_regions:
        base = endpoints.default_base_url("bailian_intl", region=rid)
        host = endpoints.host_of(base, "llm-x")
        check(host == f"llm-x.{rid}.maas.aliyuncs.com",
              f"region={rid} → host={host!r}")


# ---------------------------------------------------------------- 4) 缺 workspace_id 报错


def test_missing_workspace_raises() -> None:
    """case 4：占位符在但 workspace_id 为空 → ValueError（绝不拼出残废 host 去连）。

    ⚠️ 简报内部口径冲突：§2.1 要求异常文案**照抄**为「…需要 workspace_id（百炼控制台
    「业务空间详情 → API Host」的前缀）」，而 §2.9-4 又要求消息含「业务空间 ID」。
    两者不可能同时字面成立 —— 这里以 §2.1 的**照抄文案**为准（产品代码不改），
    断言落在真正存在的子串上：既含英文键名 `workspace_id`、也含中文「业务空间」。
    """
    base = endpoints.default_base_url("bailian_intl", region="ap-southeast-1")
    try:
        endpoints.resolve_base_url(base, "")
        check(False, "缺 workspace_id 应抛 ValueError")
    except ValueError as exc:
        msg = str(exc)
        check("workspace_id" in msg and "业务空间" in msg,
              f"ValueError 消息含定位信息：{msg!r}")

    # 纪律 §4：异常里绝不出现**完整业务空间 ID**。手写一个缺 scheme 的 base_url
    # （占位符在、但没 wss://）→ host 解析不出 → host_of 抛错，消息**不得**回显空间 ID。
    leaky = "{workspace_id}.ap-southeast-1.maas.aliyuncs.com"     # 故意漏掉 wss://
    try:
        endpoints.host_of(leaky, "llm-secret999")
        check(False, "缺 scheme 的 base_url 应让 host_of 抛 ValueError")
    except ValueError as exc:
        msg = str(exc)
        check("llm-secret999" not in msg, f"host_of 异常不回显完整空间 ID：{msg!r}")


# ---------------------------------------------------------------- 5) 手填公有域名（无占位符）


def test_public_domain_no_placeholder() -> None:
    """case 5：手填公有域名（无 {workspace_id}）→ 不要求空间 ID，chat_url 照常派生。"""
    base = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
    eq(endpoints.resolve_base_url(base), base, "无占位符 → resolve_base_url 原样返回")
    eq(endpoints.chat_url(base),
       "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
       "chat_url(公有域名)")
    eq(endpoints.multimodal_url(base),
       "https://dashscope-intl.aliyuncs.com"
       "/api/v1/services/aigc/multimodal-generation/generation",
       "multimodal_url(公有域名)")


# ---------------------------------------------------------------- 6) 归一化


def test_normalize() -> None:
    """case 6：合法值原样、非法/空/None 回落默认；key_slot / signup_url 同样回落。"""
    eq(endpoints.normalize_provider("qianwen"), "qianwen", "normalize_provider 合法值原样")
    eq(endpoints.normalize_provider("BAILIAN_INTL"), "bailian_intl", "normalize_provider 大写归一")
    # 非法值会各打印一行留痕（下面用 redirect 收掉，避免污染汇总输出），断言回落默认
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        eq(endpoints.normalize_provider("nope"), "qianwen", "normalize_provider 非法值回落")
        eq(endpoints.normalize_provider(""), "qianwen", "normalize_provider 空串回落")
        eq(endpoints.normalize_provider(None), "qianwen", "normalize_provider None 回落")
        eq(endpoints.normalize_region("us-east-1"), "us-east-1", "normalize_region 合法值原样")
        eq(endpoints.normalize_region("mars-9"), "ap-southeast-1", "normalize_region 非法值回落")
        eq(endpoints.normalize_region(None), "ap-southeast-1", "normalize_region None 回落")
    check("未识别的服务线路" in buf.getvalue(), "非法 provider 留痕（不静默）")
    check("未识别的地域" in buf.getvalue(), "非法 region 留痕（不静默）")
    eq(endpoints.key_slot("bailian_intl"), "bailian_intl", "key_slot(bailian_intl)")
    eq(endpoints.key_slot("qianwen"), "qianwen", "key_slot(qianwen)")
    with contextlib.redirect_stdout(io.StringIO()):
        eq(endpoints.key_slot("garbage"), "qianwen", "key_slot 非法值回落 qianwen")
    eq(endpoints.signup_url("bailian_intl"),
       "https://modelstudio.console.alibabacloud.com/ap-southeast-1/model/market",
       "signup_url(bailian_intl) = 国际站模型市场页（用户实测确认的地址）")
    with contextlib.redirect_stdout(io.StringIO()):
        eq(endpoints.signup_url("unknown"), "https://www.qianwenai.com/",
           "signup_url 未知 provider 回落千问云")


# ---------------------------------------------------------------- 7) 密钥分槽


def test_key_slots() -> None:
    """case 7：两条线路各存一份 key，互不干扰；老文件名仍是 qianwen 槽；非法槽名抛错。"""
    d = Path(tempfile.mkdtemp())                 # 目录只建一次（别每次调用换目录）
    old = credentials._storage_dir_override
    credentials._storage_dir_override = lambda: d
    try:
        # 拼接出来的假 key（不触发仓库凭据扫描；只需满足 >=16 字符、无空白）
        k1 = "sk" + "-test-" + "qianwen0123456789abcdef"
        k2 = "sk" + "-test-" + "bailian0123456789abcdef"
        credentials.save_api_key(k1, slot="qianwen")
        credentials.save_api_key(k2, slot="bailian_intl")
        names = sorted(p.name for p in d.iterdir())
        eq(names, ["api_key.txt", "api_key_bailian_intl.txt"], "两槽各写一个文件")
        eq(credentials.load_saved_key("qianwen"), k1, "qianwen 槽读回 k1")
        eq(credentials.load_saved_key("bailian_intl"), k2, "bailian 槽读回 k2")
        eq(credentials._key_file("qianwen").name, "api_key.txt",
           "qianwen 槽 = 老文件名 api_key.txt（向后兼容，零迁移）")
        # 清一个槽不影响另一个（这就是「各存各的」）
        check(credentials.clear_saved_key("bailian_intl") is True, "clear_saved_key(bailian) 返回 True")
        check(credentials.load_saved_key("bailian_intl") is None, "清 bailian 后该槽为空")
        eq(credentials.load_saved_key("qianwen"), k1, "清 bailian 不动 qianwen 槽")
        # 非法槽名（含路径穿越风险）→ ValueError，绝不拼出目录穿越的文件名
        try:
            credentials.save_api_key(k1, slot="../evil")
            check(False, "非法槽名应抛 ValueError")
        except ValueError:
            check(True, "非法槽名 ../evil → ValueError")
    finally:
        credentials._storage_dir_override = old


# ---------------------------------------------------------------- 8) load_config 读 provider/region


def _fake_key(tag: str) -> str:
    """拼一个够长、无空白、不触发凭据扫描的假 key（load_config 显式传参用）。"""
    return "sk" + "-test-" + tag + "0123456789abcdef"


def test_load_config_provider_region() -> None:
    """case 8：临时 yaml 读 provider/region；老配置回落 qianwen；脏值回落默认。

    **绝不碰用户真实 config.yaml**：全部写到 tempfile.mkdtemp() 下的临时文件。
    显式传 api_key → `load_api_key` 第一步就返回，既不读环境变量也不 SystemExit。
    """
    key = _fake_key("cfgload")
    d = Path(tempfile.mkdtemp())

    f1 = d / "bailian.yaml"
    f1.write_text(
        "session:\n"
        "  model: qwen3.8-livetranslate-flash-realtime\n"
        "  provider: bailian_intl\n"
        "  region: us-east-1\n"
        "  workspace_id: llm-abc123\n"
        "  base_url: wss://{workspace_id}.us-east-1.maas.aliyuncs.com/api-ws/v1/realtime\n",
        encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        cfg = config_mod.load_config(f1, api_key=key)
    eq(cfg.session_base["provider"], "bailian_intl", "读到 provider=bailian_intl")
    eq(cfg.session_base["region"], "us-east-1", "读到 region=us-east-1")
    eq(cfg.session_base["workspace_id"], "llm-abc123", "读到 workspace_id")
    scfg = cfg.directions["mine"].to_session_config(cfg.session_base)
    check(scfg.provider == "bailian_intl" and scfg.region == "us-east-1",
          f"to_session_config 带出 provider/region：{scfg.provider}/{scfg.region}")

    # 老 yaml（没有 provider/region/base_url）→ 回落 qianwen + 默认地址（行为零变化），
    # 且**不该**打印「未识别的服务线路 None」这类噪声（键缺失 ≠ 填错值，见 config.load_config）。
    f2 = d / "old.yaml"
    f2.write_text("session:\n  model: qwen3.8-livetranslate-flash-realtime\n",
                  encoding="utf-8")
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        cfg2 = config_mod.load_config(f2, api_key=key)
    eq(cfg2.session_base["provider"], "qianwen", "老配置回落 provider=qianwen")
    eq(cfg2.session_base["region"], "ap-southeast-1", "老配置回落默认 region")
    eq(cfg2.session_base["base_url"], endpoints.default_base_url("qianwen"),
       "老配置 base_url = 千问云默认")
    check("未识别的服务线路" not in buf2.getvalue()
          and "未识别的地域" not in buf2.getvalue(),
          "老配置（键缺失）静默回落，不打「未识别」噪声（§1.5 零变化）")

    # 脏值（填了但填错）→ 留痕后回落默认（不静默、也不致命）
    f3 = d / "bad.yaml"
    f3.write_text("session:\n  provider: nope\n  region: mars-9\n", encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cfg3 = config_mod.load_config(f3, api_key=key)
    eq(cfg3.session_base["provider"], "qianwen", "非法 provider 回落 qianwen")
    eq(cfg3.session_base["region"], "ap-southeast-1", "非法 region 回落默认")
    check("未识别的服务线路" in buf.getvalue() and "未识别的地域" in buf.getvalue(),
          "非法值（present）留痕 + 回落（§1.4 不静默）")


def test_provider_host_mismatch_warn() -> None:
    """设计口径 #4：provider 与 base_url 的 host 对不上 → 打一行 WARN（不报错）。

    顺带验证「对得上时不误报」—— 否则 WARN 就成了噪声，真出问题时反而被淹没。
    """
    key = _fake_key("mismatch")
    d = Path(tempfile.mkdtemp())

    f1 = d / "mismatch.yaml"                 # provider=百炼，但 base_url 还是千问云域名
    f1.write_text(
        "session:\n"
        "  provider: bailian_intl\n"
        "  region: ap-southeast-1\n"
        "  base_url: wss://maas.qianwenaiapi.com/api-ws/v1/realtime\n",
        encoding="utf-8")
    buf1 = io.StringIO()
    with contextlib.redirect_stdout(buf1):
        config_mod.load_config(f1, api_key=key)
    out1 = buf1.getvalue()
    check("线路=阿里云百炼·国际版" in out1 and "千问云的域名" in out1,
          "provider=百炼 但 host=千问云 → WARN")

    f2 = d / "mismatch2.yaml"                # 反过来：provider=千问云，但 base_url 是百炼域名
    f2.write_text(
        "session:\n"
        "  provider: qianwen\n"
        "  base_url: wss://llm-x.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime\n",
        encoding="utf-8")
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        config_mod.load_config(f2, api_key=key)
    check("线路=千问云" in buf2.getvalue(), "provider=千问云 但 host=百炼 → 对称 WARN")

    f3 = d / "match.yaml"                    # 对得上：不该有任何 WARN
    f3.write_text(
        "session:\n"
        "  provider: bailian_intl\n"
        "  region: ap-southeast-1\n"
        "  workspace_id: llm-abc123\n"
        "  base_url: wss://{workspace_id}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime\n",
        encoding="utf-8")
    buf3 = io.StringIO()
    with contextlib.redirect_stdout(buf3):
        config_mod.load_config(f3, api_key=key)
    check("但 session.base_url" not in buf3.getvalue(), "provider 与 host 一致 → 不误报 WARN")


# ---------------------------------------------------------------- 9) endpoint 覆盖


def test_endpoint_override() -> None:
    """case 9：translate_text / synthesize 传 endpoint 时实际请求 URL 改变，不传回落模块常量。"""
    custom_chat = "https://custom.example/compatible-mode/v1/chat/completions"
    custom_mm = "https://custom.example/api/v1/services/aigc/multimodal-generation/generation"

    # 打字翻译：传 endpoint
    body = json.dumps({"choices": [{"message": {"content": "Hello"}}]})
    f = FakeOpener(body)
    textin._opener = f
    textin.translate_text("你好", target_lang="en", api_key="sk-x", endpoint=custom_chat)
    eq(f.req.full_url, custom_chat, "translate_text(endpoint=...) 实际请求地址")
    # 不传 → 回落模块常量 ENDPOINT
    f = FakeOpener(body)
    textin._opener = f
    textin.translate_text("你好", target_lang="en", api_key="sk-x")
    eq(f.req.full_url, textin.ENDPOINT, "translate_text() 不传 → 回落 textin.ENDPOINT")

    # 打字译音 TTS：传 endpoint
    wav_b64 = base64.b64encode(_wav24k(0.2)).decode()
    tts_body = json.dumps({"output": {"audio": {"data": wav_b64}}})
    g = FakeOpener(tts_body)
    tts_mod._opener = g
    tts_mod.synthesize("hi", api_key="sk-x", endpoint=custom_mm)
    eq(g.req.full_url, custom_mm, "synthesize(endpoint=...) 实际请求地址")
    # 不传 → 回落模块常量 ENDPOINT
    g = FakeOpener(tts_body)
    tts_mod._opener = g
    tts_mod.synthesize("hi", api_key="sk-x")
    eq(g.req.full_url, tts_mod.ENDPOINT, "synthesize() 不传 → 回落 tts.ENDPOINT")


# ---------------------------------------------------------------- 10) SessionConfig.url（逻辑搬了家）


def test_session_config_url() -> None:
    """case 10：url 属性回归 —— 占位符替换那段逻辑从 base.py 搬进了 endpoints。"""
    scfg = SessionConfig(
        model="qwen3.8-livetranslate-flash-realtime",
        base_url="wss://{workspace_id}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime",
        workspace_id="llm-abc123")
    eq(scfg.url,
       "wss://llm-abc123.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime"
       "?model=qwen3.8-livetranslate-flash-realtime",
       "占位符 + workspace_id → 拼出 ?model= 完整 URL")

    missing = SessionConfig(
        base_url="wss://{workspace_id}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime",
        workspace_id="")
    try:
        _ = missing.url
        check(False, "缺 workspace_id 时 .url 应抛 ValueError")
    except ValueError:
        check(True, "缺 workspace_id 时 .url → ValueError")

    dflt = SessionConfig(model="m")            # base_url 默认 = 千问云
    eq(dflt.url, "wss://maas.qianwenaiapi.com/api-ws/v1/realtime?model=m",
       "默认 SessionConfig.url = 千问云")


# ---------------------------------------------------------------- 额外：Engine 接线派生


def test_engine_derives_endpoints() -> None:
    """额外：Engine 启动时把两端点算一次并缓存；派生失败留痕后回落千问云默认（构造不崩）。"""
    try:
        from vlt.engine import Engine, EngineEvents
    except Exception as exc:  # noqa: BLE001
        print(f"  （跳过 Engine 派生用例：{type(exc).__name__}: {exc}）")
        return
    from vlt.config import AppConfig, Direction

    key = _fake_key("engine")
    bailian_base = endpoints.default_base_url("bailian_intl", region="ap-southeast-1")

    def _mk(base_url: str, ws: str) -> "AppConfig":
        return AppConfig(
            session_base={"model": "qwen3.8-livetranslate-flash-realtime",
                          "base_url": base_url, "workspace_id": ws,
                          "provider": "bailian_intl", "region": "ap-southeast-1",
                          "api_key": key},
            directions={"mine": Direction(source_lang="zh", target_lang="en")},
            chatbox={}, merger={}, text_input={})

    eng = Engine(cfg=_mk(bailian_base, "llm-abc123"), direction="mine", source="mic",
                 sinks=set(), events=EngineEvents(), dry_run=True)
    eq(eng._chat_endpoint,
       "https://llm-abc123.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
       "Engine._chat_endpoint 从 base_url 派生")
    eq(eng._tts_endpoint,
       "https://llm-abc123.ap-southeast-1.maas.aliyuncs.com"
       "/api/v1/services/aigc/multimodal-generation/generation",
       "Engine._tts_endpoint 从 base_url 派生")

    # 占位符在但没 workspace_id → 派生抛 ValueError：留痕 + 回落千问云默认端点，构造不崩
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        eng2 = Engine(cfg=_mk(bailian_base, ""), direction="mine", source="mic",
                      sinks=set(), events=EngineEvents(), dry_run=True)
    dflt = endpoints.default_base_url("qianwen")
    eq(eng2._chat_endpoint, endpoints.chat_url(dflt), "派生失败 → chat 回落千问云默认")
    eq(eng2._tts_endpoint, endpoints.multimodal_url(dflt), "派生失败 → tts 回落千问云默认")
    check("出网端点派生失败" in buf.getvalue(), "派生失败留痕（不静默）")


# ---------------------------------------------------------------- 11) 流式兜底那次请求也必须走当前线路


class _SeqResp:
    """按需返回：既可当 SSE 逐行迭代，也可 read()。`headers` 是判定 SSE 的唯一依据。"""

    def __init__(self, lines, ctype: str = "text/event-stream") -> None:
        self.headers = {"Content-Type": ctype}
        self._lines = [ln.encode("utf-8") if isinstance(ln, str) else ln for ln in lines]

    def __iter__(self):
        return iter(self._lines)

    def read(self) -> bytes:
        return b"".join(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False


class _SeqOpener:
    """按调用顺序发不同的响应，并把每次请求留下来（`reqs[-1].full_url` = 最后一次真实地址）。"""

    def __init__(self, resps, fail_first: int | None = None) -> None:
        self.resps, self.reqs, self.fail_first = list(resps), [], fail_first

    def open(self, req, timeout=None):  # noqa: ANN001
        self.reqs.append(req)
        if self.fail_first is not None and len(self.reqs) == 1:
            raise HTTPError(req.full_url, self.fail_first, "synthetic", {}, io.BytesIO(b"{}"))
        return self.resps[min(len(self.reqs) - 1, len(self.resps) - 1)]


def test_stream_fallback_keeps_endpoint() -> None:
    """case 11：`synthesize_stream` 退回整段合成时，那次请求**也必须**走当前线路。

    为什么单独钉这条：两条兜底路径（① 服务端不认流式：HTTP 400/406/415；② 一个分片都没拿到）
    都会掉回 `synthesize()`。若兜底那次忘了透传 endpoint，切到海外线路后**只有流式兜底**
    会偷偷连回千问云 —— 平时一切正常，只在服务端不认流式时才现形，是最难查的那种漂移。
    两条路径各验一遍。
    """
    custom_mm = "https://llm-abc.ap-southeast-1.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    tts_body = json.dumps({"output": {"audio": {"data": base64.b64encode(_wav24k(0.2)).decode()}}})

    # ① 服务端不认流式（HTTP 400）→ 退回整段
    op = _SeqOpener([_SeqResp([], ctype="application/json"), FakeResp(tts_body)], fail_first=400)
    tts_mod._opener = op
    parts = list(tts_mod.synthesize_stream("你好", api_key="sk-x", endpoint=custom_mm))
    eq(len(op.reqs), 2, "① 服务端不认流式 → 触发整段兜底（共 2 次请求）")
    eq(op.reqs[-1].full_url, custom_mm,
       "① 兜底那次请求也必须走当前线路（否则切海外会偷偷连回千问云）")
    check(len(parts) == 1, "① 兜底确实吐出了 1 块音频")

    # ② 流式一个分片都没拿到（只回 [DONE]）→ 退回整段
    op2 = _SeqOpener([_SeqResp(["data: [DONE]\n"]), FakeResp(tts_body)])
    tts_mod._opener = op2
    parts2 = list(tts_mod.synthesize_stream("你好", api_key="sk-x", endpoint=custom_mm))
    eq(len(op2.reqs), 2, "② 流式空手而归 → 触发整段兜底（共 2 次请求）")
    eq(op2.reqs[-1].full_url, custom_mm,
       "② 兜底那次请求也必须走当前线路（否则切海外会偷偷连回千问云）")
    check(len(parts2) == 1, "② 兜底确实吐出了 1 块音频")

    # ③ 不传 endpoint 时仍回落模块常量（老调用零变化）
    op3 = _SeqOpener([_SeqResp(["data: [DONE]\n"]), FakeResp(tts_body)])
    tts_mod._opener = op3
    list(tts_mod.synthesize_stream("你好", api_key="sk-x"))
    eq(op3.reqs[-1].full_url, tts_mod.ENDPOINT, "③ 不传 endpoint → 兜底仍回落 tts.ENDPOINT")


# ---------------------------------------------------------------- 12) describe 日志摘要（打码 + 不抛）


def test_describe_masks_workspace() -> None:
    """case 12：`[net]` 那行日志 —— 线路/地域/host 要看得清，业务空间 ID 不能整串进日志。

    为什么单钉：日志会落到用户硬盘上（还可能被贴进 issue / 群里求助），
    空间 ID 是账号标识，整串打出去属于白白泄露；但线路名与地域必须完整，
    否则「我切了线路怎么没生效」根本查不了。
    """
    ws = "llm-abc123def"
    base = endpoints.default_base_url("bailian_intl", ws, "ap-southeast-1")
    line = endpoints.describe("bailian_intl", "ap-southeast-1", base, ws)
    check(ws not in line, f"describe 不整串打出业务空间 ID：{line}")
    check("llm-ab…" in line, "describe 保留空间 ID 前 6 字符（够对控制台）")
    check("ap-southeast-1.maas.aliyuncs.com" in line, "describe 保留地域 + 域名（排查要看）")
    check("阿里云百炼·国际版" in line, "describe 带线路名")

    q = endpoints.describe("qianwen", "ap-southeast-1",
                           endpoints.default_base_url("qianwen"), "")
    eq(q, "线路=千问云 host=maas.qianwenaiapi.com", "千问云线路的 describe")

    # host 解析不出来时也绝不能把启动搞挂（日志本身不能成为故障源）
    bad = endpoints.describe("bailian_intl", "ap-southeast-1", "没有 scheme 的串", "llm-x")
    check("<host 解析失败>" in bad, "base_url 解析失败 → 降级占位串，不抛")


# ---------------------------------------------------------------- 13) 界面保存线路：落盘 + 缺段响亮失败


def test_persist_provider_guard() -> None:
    """case 13：`gui._persist_provider` —— 正常落盘；config.yaml 缺 `session:` 段时**响亮失败**。

    为什么单钉第二条：`_yaml_set_in_text` 找不到路径时是「原样返回」，写成文件就是
    同一份文本 —— 界面会说保存成功、配置其实一字未改（静默降级）。房间段有补建逻辑，
    session 段没有，所以老配置上必须**抛错**而不是假装成功。
    """
    d = Path(tempfile.mkdtemp())
    try:
        import vlt.gui as gui_mod          # 延迟导入：本文件其余用例不需要 tkinter/音频依赖
    except Exception as exc:               # noqa: BLE001 — 缺依赖的环境跳过，别把整份用例打成红
        print(f"  ⚠ 跳过（导入 vlt.gui 失败：{type(exc).__name__}: {exc}）")
        return

    bailian_base = "wss://{workspace_id}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime"

    # ① 正常：从模板拷一份（含注释）→ 四项都写进去
    ok_cfg = d / "config.yaml"
    shutil.copyfile(ROOT / "config.example.yaml", ok_cfg)
    gui_mod._persist_provider(ok_cfg, "bailian_intl", "ap-southeast-1", "llm-test123",
                              bailian_base)
    after = ok_cfg.read_text(encoding="utf-8")
    raw = (yaml.safe_load(after) or {}).get("session") or {}
    eq(raw.get("provider"), "bailian_intl", "① 落盘 session.provider（裸写线路 id）")
    eq(raw.get("region"), "ap-southeast-1", "① 落盘 session.region")
    eq(raw.get("workspace_id"), "llm-test123", "① 落盘 session.workspace_id")
    eq(raw.get("base_url"), bailian_base, "① 落盘 session.base_url（保留 {workspace_id} 字面量）")
    check("# " in after, "① 就地写入保住了注释")

    # ② 老配置没有 session: 段 → 必须抛错，且磁盘文件**一个字没动**
    bad_cfg = d / "legacy.yaml"
    bad_cfg.write_text("chatbox:\n  host: 127.0.0.1\n  port: 9000\n", encoding="utf-8")
    before = bad_cfg.read_text(encoding="utf-8")
    raised = ""
    try:
        gui_mod._persist_provider(bad_cfg, "bailian_intl", "ap-southeast-1", "llm-x",
                                  bailian_base)
    except RuntimeError as exc:
        raised = str(exc)
    check(bool(raised), "② 缺 session: 段 → 抛 RuntimeError（不是静默不保存）")
    check("session" in raised, "② 报错说清缺的是哪一段（用户能自己补）")
    eq(bad_cfg.read_text(encoding="utf-8"), before, "② 失败时磁盘文件一字未改")


# ---------------------------------------------------------------- 业务空间 ID 形态校验


def test_validate_workspace_id() -> None:
    """case 14：业务空间 ID 的**形态**校验 —— 线上事故的直接回归。

    事故（2026-10-02，海外用户）：把 114 字符的 API key 粘进了「业务空间 ID」框。
    改动前只校验「非空」，于是 key 被当成 host 的**第一段**拼进去 → DNS 单段超 63 字符 →
    建链那一刻抛 `UnicodeError: encoding with 'idna' codec failed (label empty or too long)`
    —— 报错与「粘错了框」毫无关系，用户和我们都要翻半天日志。
    """
    key = "sk-ws-" + "A" * 108                      # 与事故同形：114 字符、sk- 开头
    cases = [
        (key, "key_like", "114 字符 API key（事故现场原样）"),
        ("sk-abcdef", "key_like", "短的 sk- 串同样算 key"),
        ("SK-abcdef", "key_like", "大写 SK- 也要认"),
        ("", "empty", "空串"),
        ("   ", "empty", "全空白"),
        (None, "empty", "None"),
        ("A" * 64, "too_long", "64 字符（超 DNS 单段上限 1 个字符）"),
        ("llm.abcd1234", "bad_chars", "带点（业务空间 ID 是单个 DNS 段）"),
        ("llm abcd", "bad_chars", "带空格（从控制台复制常带）"),
        ("业务空间-abcd", "bad_chars", "非 ASCII"),
        ("-llm-abcd", "edge_dash", "以短横线开头"),
        ("llm-abcd-", "edge_dash", "以短横线结尾"),
        ("llm-abcd1234", None, "正常值"),
        ("A" * 63, None, "63 字符（正好在上限）"),
        ("llm-0a1b2c3d", None, "另一条正常值"),
    ]
    for value, want, label in cases:
        eq(endpoints.validate_workspace_id(value), want, f"validate_workspace_id({label})")

    # 回归核心：**正是这个值**会让建链炸掉 —— 证明守卫拦的是真会出事的输入，不是摆设
    base = endpoints.default_base_url(endpoints.PROVIDER_BAILIAN_INTL, key, "ap-northeast-1")
    blew = ""
    try:
        endpoints.host_of(base, key).encode("idna")
    except UnicodeError as exc:
        blew = str(exc)
    check(bool(blew), f"① 不校验时该值确实会让建链炸（{blew[:52]}…）")
    check(endpoints.validate_workspace_id(key) == "key_like",
          "① 校验会在保存 / 开始翻译之前把它拦下（用户看不到 idna 报错）")
    ok_host = endpoints.host_of(
        endpoints.default_base_url(endpoints.PROVIDER_BAILIAN_INTL, "llm-abcd1234",
                                   "ap-northeast-1"), "llm-abcd1234")
    try:
        ok_host.encode("idna")
        check(True, f"① 合法业务空间 ID 派生出的 host 能过 idna（{ok_host}）")
    except UnicodeError as exc:                     # pragma: no cover — 正常不会走到
        check(False, f"① 合法值反而过不了 idna？{exc}")


def test_gui_workspace_id_guard() -> None:
    """case 15：真窗口 —— 「保存线路设置」与「开始翻译」两条路都要拦下形态不对的值。

    钉子：① 保存被拒时**配置文件一个字不改**（不能把连不上的 host 留在配置里）；
    ② 错误提示里得说清「这是 API key、该填哪儿」；③ 合法值照常落盘；
    ④ 「开始翻译」前置守卫拦住并给状态栏错误，不真的去建链。
    """
    d = Path(tempfile.mkdtemp(prefix="vlt-wsid-"))
    cfg = d / "config.yaml"
    shutil.copyfile(ROOT / "config.example.yaml", cfg)
    key = "sk-ws-" + "A" * 108
    saved_env = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME", "DASHSCOPE_API_KEY")}
    os.environ["USERPROFILE"] = str(d)
    os.environ["HOME"] = str(d)
    os.environ.pop("DASHSCOPE_API_KEY", None)
    gui = None
    try:
        import vlt.config as cfg_mod
        import vlt.gui as gui_mod

        cfg_mod.DEFAULT_CONFIG = cfg
        gui_mod.DEFAULT_CONFIG = cfg
        gui = gui_mod.TranslationGUI()
        if gui._update_check_job is not None:
            gui._root.after_cancel(gui._update_check_job)
            gui._update_check_job = None
        gui._root.update()

        # ---- ① 保存：把 API key 粘进「业务空间 ID」→ 拒绝 + 磁盘一字不动 ----
        before = cfg.read_text(encoding="utf-8")
        gui._provider_var.set(gui._provider_id_to_name[endpoints.PROVIDER_BAILIAN_INTL])
        gui._on_provider_change()
        gui._region_var.set(gui_mod._region_display(endpoints.DEFAULT_REGION))
        gui._workspace_var.set(key)
        gui._on_save_provider()
        gui._root.update()
        check(cfg.read_text(encoding="utf-8") == before, "① 保存被拒：配置文件一个字没改")
        err = str(gui._provider_err.cget("text") or "")
        check("API key" in err, f"① 提示说清了「这是 API key」（{err[:44]}…）")
        check(gui._last_status_level == "error", "① 状态栏标成 error（不静默）")

        # ---- ② 合法值：照常落盘（改完要能正常工作）----
        gui._workspace_var.set("llm-abcd1234")
        gui._on_save_provider()
        gui._root.update()
        raw = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("session") or {}
        eq(raw.get("provider"), "bailian_intl", "② 合法值：落盘 session.provider")
        eq(raw.get("workspace_id"), "llm-abcd1234", "② 合法值：落盘 session.workspace_id")
        check("{workspace_id}" in str(raw.get("base_url") or ""),
              "② 合法值：base_url 仍保留 {workspace_id} 字面量")
        check(not str(gui._provider_err.cget("text") or ""), "② 保存成功后错误提示已清空")

        # ---- ③ 「开始翻译」前置守卫：配置里是 key → 拦下且不建链 ----
        gui._cfg.session_base["workspace_id"] = key
        gui._cfg.session_base["api_key"] = "sk-" + "t" * 40      # 绕过「没 key」那条更早的分支
        gui._refresh_api_key_in_cfg = lambda: None               # 别让它把 key 重新解成空
        gui._start()
        gui._root.update()
        check(not any(e.running for e in gui._engines), "③ 未启动：一个引擎都没起来")
        check(gui._last_status_level == "error", "③ 状态栏标成 error")
        check("业务空间 ID" in str(gui._status_label.cget("text") or ""),
              f"③ 状态栏说清了是业务空间 ID 的问题（{gui._status_label.cget('text')!r}）")
    finally:
        if gui is not None:
            try:
                gui._root.destroy()
            except Exception:                       # noqa: BLE001
                pass
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------- 地域收口


def test_region_closure() -> None:
    """case 16：地域收口到新加坡 —— 候选、判定、**绝不静默改写**。

    背景（2026-10-02，官方国际站逐页核对）：本程序要用的语音链路
    （实时同传 `qwen3.8-livetranslate-flash-realtime` / TTS `qwen3-tts-flash` /
    Omni）在国际站**只有新加坡**有部署；东京、弗吉尼亚、法兰克福、香港都缺语音类模型
    （东京连打字翻译的 `qwen-mt-flash` 都没有）。所以地域候选只留新加坡。

    但**绝不静默改写**已有配置里的地域：地域进 host
    （`{workspace_id}.{region}.maas.aliyuncs.com`），把它改成新加坡等于把请求打到一个
    和用户业务空间对不上的域名上 —— 比「这个地域不支持」难查得多。
    """
    eq([rid for rid, _ in endpoints.REGIONS], ["ap-southeast-1"], "可选地域只剩新加坡")
    eq(endpoints.DEFAULT_REGION, "ap-southeast-1", "默认地域 = 新加坡")
    for rid, _name in endpoints.REGIONS:
        eq(endpoints.region_supported(rid), None, f"{rid} 应判为可用")
    for rid in endpoints.UNSUPPORTED_REGIONS:
        eq(endpoints.region_supported(rid), "unsupported", f"{rid} 应判为不支持")
    eq(endpoints.region_supported("zz-nowhere"), "unknown", "完全未知的地域 → unknown")

    # 关键不变式一：已知但不支持的地域**原样保留**（不改写成新加坡）
    eq(endpoints.normalize_region("ap-northeast-1"), "ap-northeast-1",
       "已知但不支持的地域原样保留（改写会把请求打到别的地域域名上）")
    # 关键不变式二：它仍然如实进 host —— 正因如此才必须在前面拦住，而不是改掉它
    host = endpoints.host_of(
        endpoints.default_base_url(endpoints.PROVIDER_BAILIAN_INTL,
                                   "ws-j8wqpy9f2w86a8k3", "ap-northeast-1"),
        "ws-j8wqpy9f2w86a8k3")
    check(host.endswith(".ap-northeast-1.maas.aliyuncs.com"),
          f"地域仍如实进 host（{host}）")
    # 只有**完全不认识**的值才回落默认
    eq(endpoints.normalize_region("zz-nowhere"), endpoints.DEFAULT_REGION, "未知地域回落新加坡")
    eq(endpoints.region_name("ap-northeast-1"), "Japan (Tokyo)", "region_name 认得老地域")
    eq(endpoints.region_name("zz-nowhere"), "zz-nowhere", "region_name 未知值原样回显（不 KeyError）")


def test_gui_region_guard() -> None:
    """case 17：真窗口 —— 老配置里存着东京时，设置页要开得开、红字要讲清、
    保存与开始翻译两条路都要拦下；切成新加坡后照常保存。
    """
    d = Path(tempfile.mkdtemp(prefix="vlt-region-"))
    cfg = d / "config.yaml"
    # 造一份「老版本存下来的东京配置」：provider/region/workspace_id 都按东京写
    txt = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    txt = txt.replace("  provider: qianwen", "  provider: bailian_intl")
    txt = txt.replace("  region: ap-southeast-1", "  region: ap-northeast-1")
    txt = txt.replace('  workspace_id: ""', '  workspace_id: "ws-j8wqpy9f2w86a8k3"')
    check("  region: ap-northeast-1" in txt, "造出了东京老配置")
    cfg.write_text(txt, encoding="utf-8")

    saved_env = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME", "DASHSCOPE_API_KEY")}
    os.environ["USERPROFILE"] = str(d)
    os.environ["HOME"] = str(d)
    os.environ.pop("DASHSCOPE_API_KEY", None)
    gui = None
    try:
        import vlt.config as cfg_mod
        import vlt.gui as gui_mod

        cfg_mod.DEFAULT_CONFIG = cfg
        gui_mod.DEFAULT_CONFIG = cfg
        gui = gui_mod.TranslationGUI()          # ← 老地域不能让这一步抛 KeyError
        if gui._update_check_job is not None:
            gui._root.after_cancel(gui._update_check_job)
            gui._update_check_job = None
        gui._root.update()

        # ---- ① 设置页打得开、地域显示东京、红字当场讲清原因 ----
        check("Japan (Tokyo)" in gui._region_var.get(),
              f"① 地域照实显示（{gui._region_var.get()!r}）")
        err = str(gui._provider_err.cget("text") or "")
        check("新加坡" in err, f"① 红字说清了该改用新加坡（{err[:40]}…）")
        check("ap-southeast-1" in err, "① 红字给了具体地域 id")

        # ---- ② 保存被拒 + 配置文件一字不动 ----
        before = cfg.read_text(encoding="utf-8")
        gui._on_save_provider()
        gui._root.update()
        check(cfg.read_text(encoding="utf-8") == before, "② 保存被拒：配置文件一个字没改")
        check(gui._last_status_level == "error", "② 状态栏标成 error（不静默）")
        raw = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("session") or {}
        eq(raw.get("region"), "ap-northeast-1", "② 地域没被静默改写")

        # ---- ③ 「开始翻译」被拦下，不建链 ----
        gui._cfg.session_base["api_key"] = "sk-" + "t" * 40   # 绕过更早的「没 key」分支
        gui._refresh_api_key_in_cfg = lambda: None            # 别把 key 重新解成空
        gui._start()
        gui._root.update()
        check(not any(e.running for e in gui._engines), "③ 未启动：一个引擎都没起来")
        check(gui._last_status_level == "error", "③ 状态栏标成 error")
        check("地域" in str(gui._status_label.cget("text") or ""),
              f"③ 状态栏说清了是地域的问题（{gui._status_label.cget('text')!r}）")

        # ---- ④ 切成新加坡 → 照常落盘，红字清空 ----
        gui._region_var.set(gui_mod._region_display(endpoints.DEFAULT_REGION))
        gui._on_save_provider()
        gui._root.update()
        raw = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("session") or {}
        eq(raw.get("region"), "ap-southeast-1", "④ 切成新加坡后落盘")
        check(not str(gui._provider_err.cget("text") or ""), "④ 保存成功后错误提示已清空")
    finally:
        if gui is not None:
            try:
                gui._root.destroy()
            except Exception:                       # noqa: BLE001
                pass
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------- main


_FUNCS = [
    test_qianwen_endpoints,
    test_bailian_derivation,
    test_all_regions,
    test_missing_workspace_raises,
    test_public_domain_no_placeholder,
    test_normalize,
    test_key_slots,
    test_load_config_provider_region,
    test_provider_host_mismatch_warn,
    test_endpoint_override,
    test_session_config_url,
    test_engine_derives_endpoints,
    test_stream_fallback_keeps_endpoint,
    test_describe_masks_workspace,
    test_persist_provider_guard,
    test_validate_workspace_id,
    test_gui_workspace_id_guard,
    test_region_closure,
    test_gui_region_guard,
]


def main() -> int:
    print("test_endpoints:")
    for fn in _FUNCS:
        print(f"\n[{fn.__name__}]")
        try:
            fn()
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            _CHECKS["n"] += 1
            _CHECKS["bad"] += 1
    print(f"\n共 {_CHECKS['n']} 项断言，失败 {_CHECKS['bad']} 项")
    if _CHECKS["bad"]:
        print("FAILED")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
