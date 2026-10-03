"""服务线路（千问云 / 千问云·海外版）出网端点派生的验收测试。

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


# ---------------------------------------------------------------- 2) 千问云·海外版派生


def test_qwencloud_derivation() -> None:
    """case 2：千问云·海外版四条值**整串写死**比对（地址里没有账号成分，直接可连）。"""
    base = "wss://maas.qwencloudapi.com/api-ws/v1/realtime"
    eq(endpoints.default_base_url("qwencloud"), base, "default_base_url(qwencloud)")
    eq(endpoints.host_of(base), "maas.qwencloudapi.com", "host_of(qwencloud)")
    eq(endpoints.chat_url(base),
       "https://maas.qwencloudapi.com/compatible-mode/v1/chat/completions",
       "chat_url(qwencloud)")
    eq(endpoints.multimodal_url(base),
       "https://maas.qwencloudapi.com/api/v1/services/aigc/multimodal-generation/generation",
       "multimodal_url(qwencloud)")
    # 用户给的入口就是兼容模式那一串，钉住 host + 路径的拼接结果
    eq(endpoints.chat_url(base).rsplit("/chat/completions", 1)[0],
       "https://maas.qwencloudapi.com/compatible-mode/v1",
       "海外版 OpenAI 兼容 base_url（与用户给的 https://maas.qwencloudapi.com/compatible-mode/v1 一致）")


# ---------------------------------------------------------------- 3) 已下线线路的识别（迁移用）


def test_retired_line_detection() -> None:
    """case 3：`is_retired_base_url` —— 只认当年百炼国际版生成的域名，别的一律 False。

    为什么单钉：`config.load_config` 靠它做**一次性迁移**（老配置从百炼国际版挪到
    千问云·海外版，并提示 key 要重填）。误判 = 把用户手写的地址也改掉；
    漏判 = 老配置继续连一条已经不在界面里的线路。
    """
    retired = [
        "wss://{workspace_id}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime",
        "wss://llm-abc123.us-east-1.maas.aliyuncs.com/api-ws/v1/realtime",
        "wss://llm-abc123.cn-hongkong.maas.aliyuncs.com/api-ws/v1/realtime",
        "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime",
    ]
    for base in retired:
        check(endpoints.is_retired_base_url(base), f"认出已下线线路：{base[:60]}…")

    keep = [
        "wss://maas.qianwenaiapi.com/api-ws/v1/realtime",                    # 千问云（国内）
        "wss://maas.qwencloudapi.com/api-ws/v1/realtime",                    # 千问云·海外版
        "wss://llm-abc.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime",     # 国内百炼：别动人家
        "wss://gateway.example.com/api-ws/v1/realtime",                      # 用户自建
        "", "没有 scheme 的串",
    ]
    for base in keep:
        check(not endpoints.is_retired_base_url(base), f"不误判：{base[:60]!r}")


# ---------------------------------------------------------------- 5) 自定义 host 派生


def test_custom_host_derivation() -> None:
    """case 5：手填第三方/自建地址（scheme + host 齐全）→ 两条 HTTP 端点照常从 host 派生。"""
    base = "wss://gateway.example.com/api-ws/v1/realtime"
    eq(endpoints.chat_url(base),
       "https://gateway.example.com/compatible-mode/v1/chat/completions", "chat_url(自定义 host)")
    eq(endpoints.multimodal_url(base),
       "https://gateway.example.com"
       "/api/v1/services/aigc/multimodal-generation/generation", "multimodal_url(自定义 host)")
    # 缺 scheme / 空串 → 抛 ValueError（绝不拼出 https:///compatible-mode/... 这种残废地址）
    for bad in ("", "没有 scheme 的串", "maas.qwencloudapi.com/api-ws/v1/realtime"):
        try:
            endpoints.host_of(bad)
            check(False, f"host_of({bad!r}) 应抛 ValueError")
        except ValueError:
            check(True, f"host_of({bad!r}) → ValueError")
        check(not endpoints.is_retired_base_url(bad), f"残废地址不算已下线线路：{bad!r}")


# ---------------------------------------------------------------- 6) 归一化


def test_normalize() -> None:
    """case 6：合法值原样、非法/空/None 回落默认；已下线线路按海外版处理并留痕。"""
    eq(endpoints.normalize_provider("qianwen"), "qianwen", "normalize_provider 合法值原样")
    eq(endpoints.normalize_provider("QWENCLOUD"), "qwencloud", "normalize_provider 大写归一")
    # 非法 / 已下线值都会打印留痕（下面用 redirect 收掉，避免污染汇总输出）
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        eq(endpoints.normalize_provider("bailian_intl"), "qwencloud", "已下线的 bailian_intl 落到海外版")
        eq(endpoints.normalize_provider("nope"), "qianwen", "normalize_provider 非法值回落")
        eq(endpoints.normalize_provider(""), "qianwen", "normalize_provider 空串回落")
        eq(endpoints.normalize_provider(None), "qianwen", "normalize_provider None 回落")
    out = buf.getvalue()
    check("已下线" in out and "不互通" in out,
          "已下线线路留痕，且提醒 key 不互通（不静默）")
    check("未识别的服务线路" in out, "非法 provider 留痕（不静默）")
    eq(endpoints.key_slot("qwencloud"), "qwencloud", "key_slot(qwencloud)")
    eq(endpoints.key_slot("qianwen"), "qianwen", "key_slot(qianwen)")
    with contextlib.redirect_stdout(io.StringIO()):
        eq(endpoints.key_slot("garbage"), "qianwen", "key_slot 非法值回落 qianwen")
    eq(endpoints.signup_url("qwencloud"), "https://www.qwencloud.com/",
       "signup_url(qwencloud) = Qwen Cloud 首页")
    eq(endpoints.signup_url("qianwen"), "https://www.qianwenai.com/", "signup_url(qianwen)")
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
        k2 = "sk" + "-test-" + "qwencloud0123456789abcdef"
        credentials.save_api_key(k1, slot="qianwen")
        credentials.save_api_key(k2, slot="qwencloud")
        names = sorted(p.name for p in d.iterdir())
        eq(names, ["api_key.txt", "api_key_qwencloud.txt"], "两槽各写一个文件")
        eq(credentials.load_saved_key("qianwen"), k1, "qianwen 槽读回 k1")
        eq(credentials.load_saved_key("qwencloud"), k2, "海外版槽读回 k2")
        eq(credentials._key_file("qianwen").name, "api_key.txt",
           "qianwen 槽 = 老文件名 api_key.txt（向后兼容，零迁移）")
        # 清一个槽不影响另一个（这就是「各存各的」）
        check(credentials.clear_saved_key("qwencloud") is True, "clear_saved_key(qwencloud) 返回 True")
        check(credentials.load_saved_key("qwencloud") is None, "清海外版槽后该槽为空")
        eq(credentials.load_saved_key("qianwen"), k1, "清海外版槽不动 qianwen 槽")
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


def test_load_config_provider() -> None:
    """case 8：临时 yaml 读 provider；老配置回落 qianwen；脏值回落默认；老线路自动迁移。

    **绝不碰用户真实 config.yaml**：全部写到 tempfile.mkdtemp() 下的临时文件。
    显式传 api_key → `load_api_key` 第一步就返回，既不读环境变量也不 SystemExit。
    """
    key = _fake_key("cfgload")
    d = Path(tempfile.mkdtemp())

    f1 = d / "qwencloud.yaml"
    f1.write_text(
        "session:\n"
        "  model: qwen3.8-livetranslate-flash-realtime\n"
        "  provider: qwencloud\n"
        "  base_url: wss://maas.qwencloudapi.com/api-ws/v1/realtime\n",
        encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        cfg = config_mod.load_config(f1, api_key=key)
    eq(cfg.session_base["provider"], "qwencloud", "读到 provider=qwencloud")
    eq(cfg.session_base["base_url"], endpoints.default_base_url("qwencloud"), "读到海外版 base_url")
    scfg = cfg.directions["mine"].to_session_config(cfg.session_base)
    eq(scfg.provider, "qwencloud", "to_session_config 带出 provider")
    eq(scfg.url, "wss://maas.qwencloudapi.com/api-ws/v1/realtime"
                 "?model=qwen3.8-livetranslate-flash-realtime",
       "会话 URL 用海外版地址（真连的就是它）")

    # 老 yaml（没有 provider/base_url）→ 回落 qianwen + 默认地址（行为零变化），
    # 且**不该**打印「未识别的服务线路 None」这类噪声（键缺失 ≠ 填错值，见 config.load_config）。
    f2 = d / "old.yaml"
    f2.write_text("session:\n  model: qwen3.8-livetranslate-flash-realtime\n",
                  encoding="utf-8")
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        cfg2 = config_mod.load_config(f2, api_key=key)
    eq(cfg2.session_base["provider"], "qianwen", "老配置回落 provider=qianwen")
    eq(cfg2.session_base["base_url"], endpoints.default_base_url("qianwen"),
       "老配置 base_url = 千问云默认")
    check("未识别的服务线路" not in buf2.getvalue(),
          "老配置（键缺失）静默回落，不打「未识别」噪声（§1.5 零变化）")

    # 脏值（填了但填错）→ 留痕后回落默认（不静默、也不致命）
    f3 = d / "bad.yaml"
    f3.write_text("session:\n  provider: nope\n", encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cfg3 = config_mod.load_config(f3, api_key=key)
    eq(cfg3.session_base["provider"], "qianwen", "非法 provider 回落 qianwen")
    check("未识别的服务线路" in buf.getvalue(), "非法值（present）留痕 + 回落（§1.4 不静默）")

    # 老线路（百炼国际版）→ 一次性迁移到海外版，且**响亮说明 key 要重填**
    f4 = d / "retired.yaml"
    f4.write_text(
        "session:\n"
        "  provider: bailian_intl\n"
        "  region: ap-southeast-1\n"
        "  workspace_id: llm-abc123\n"
        "  base_url: wss://{workspace_id}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime\n",
        encoding="utf-8")
    buf4 = io.StringIO()
    with contextlib.redirect_stdout(buf4):
        cfg4 = config_mod.load_config(f4, api_key=key)
    out4 = buf4.getvalue()
    eq(cfg4.session_base["provider"], "qwencloud", "老线路迁移后 provider=qwencloud")
    eq(cfg4.session_base["base_url"], endpoints.default_base_url("qwencloud"),
       "老线路迁移后 base_url = 海外版默认")
    check("已下线" in out4 and "重新填一次 key" in out4, "迁移留痕并提醒重填 key（不静默）")
    check("llm-abc123" not in out4, "迁移日志不回显业务空间 ID（账号标识不进日志）")


def test_provider_host_mismatch_warn() -> None:
    """设计口径 #4：provider 与 base_url 的 host 对不上 → 打一行 WARN（不报错）。

    顺带验证「对得上时不误报」—— 否则 WARN 就成了噪声，真出问题时反而被淹没。
    """
    key = _fake_key("mismatch")
    d = Path(tempfile.mkdtemp())

    f1 = d / "mismatch.yaml"                 # provider=海外版，但 base_url 还是国内域名
    f1.write_text(
        "session:\n"
        "  provider: qwencloud\n"
        "  base_url: wss://maas.qianwenaiapi.com/api-ws/v1/realtime\n",
        encoding="utf-8")
    buf1 = io.StringIO()
    with contextlib.redirect_stdout(buf1):
        config_mod.load_config(f1, api_key=key)
    out1 = buf1.getvalue()
    check("线路=千问云·海外版" in out1 and "国内千问云的域名" in out1,
          "provider=海外版 但 host=国内 → WARN")

    f2 = d / "mismatch2.yaml"                # 反过来：provider=千问云，但 base_url 是海外版
    f2.write_text(
        "session:\n"
        "  provider: qianwen\n"
        "  base_url: wss://maas.qwencloudapi.com/api-ws/v1/realtime\n",
        encoding="utf-8")
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        config_mod.load_config(f2, api_key=key)
    check("线路=千问云，" in buf2.getvalue() and "海外版" in buf2.getvalue(),
          "provider=千问云 但 host=海外版 → 对称 WARN")

    f3 = d / "match.yaml"                    # 对得上：不该有任何 WARN
    f3.write_text(
        "session:\n"
        "  provider: qwencloud\n"
        "  base_url: wss://maas.qwencloudapi.com/api-ws/v1/realtime\n",
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
    """case 10：`.url` 只负责拼 `?model=`（两条线路都是可直接连接的公共地址，无占位符）。"""
    scfg = SessionConfig(model="qwen3.8-livetranslate-flash-realtime",
                         base_url="wss://maas.qwencloudapi.com/api-ws/v1/realtime")
    eq(scfg.url,
       "wss://maas.qwencloudapi.com/api-ws/v1/realtime"
       "?model=qwen3.8-livetranslate-flash-realtime",
       "海外版 base_url → 拼出 ?model= 完整 URL")
    dflt = SessionConfig(model="m")            # base_url 默认 = 千问云
    eq(dflt.url, "wss://maas.qianwenaiapi.com/api-ws/v1/realtime?model=m",
       "默认 SessionConfig.url = 千问云")
    eq(dflt.provider, endpoints.DEFAULT_PROVIDER, "默认 provider = 当前默认线路")


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

    def _mk(base_url: str) -> "AppConfig":
        return AppConfig(
            session_base={"model": "qwen3.8-livetranslate-flash-realtime",
                          "base_url": base_url, "provider": "qwencloud",
                          "api_key": key},
            directions={"mine": Direction(source_lang="zh", target_lang="en")},
            chatbox={}, merger={}, text_input={})

    eng = Engine(cfg=_mk(endpoints.default_base_url("qwencloud")), direction="mine",
                 source="mic", sinks=set(), events=EngineEvents(), dry_run=True)
    eq(eng._chat_endpoint,
       "https://maas.qwencloudapi.com/compatible-mode/v1/chat/completions",
       "Engine._chat_endpoint 从 base_url 派生")
    eq(eng._tts_endpoint,
       "https://maas.qwencloudapi.com/api/v1/services/aigc/multimodal-generation/generation",
       "Engine._tts_endpoint 从 base_url 派生")

    # 手写的 base_url 缺 scheme → 派生抛 ValueError：留痕 + 回落千问云默认端点，构造不崩
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        eng2 = Engine(cfg=_mk("没有 scheme 的串"), direction="mine", source="mic",
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
    custom_mm = "https://maas.qwencloudapi.com/api/v1/services/aigc/multimodal-generation/generation"
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


def test_describe_line_summary() -> None:
    """case 12：`[net]` 那行日志 —— 线路名与 host 必须完整（换线路排查第一眼），且绝不抛。

    为什么单钉：日志会落到用户硬盘上、还可能被贴进 issue；两条线路**只能靠这一行**区分
    （同一个模型、不同域名），写不全就根本查不了「我切了线路怎么没生效」。
    """
    base = endpoints.default_base_url("qwencloud")
    eq(endpoints.describe("qwencloud", base),
       "线路=千问云·海外版 host=maas.qwencloudapi.com", "海外版的 describe")
    eq(endpoints.describe("qianwen", endpoints.default_base_url("qianwen")),
       "线路=千问云 host=maas.qianwenaiapi.com", "千问云线路的 describe")

    # host 解析不出来时也绝不能把启动搞挂（日志本身不能成为故障源）
    bad = endpoints.describe("qwencloud", "没有 scheme 的串")
    check("<host 解析失败>" in bad, "base_url 解析失败 → 降级占位串，不抛")

    # 老线路名（bailian_intl）在日志里也要认出来：归一化成海外版并留痕
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        line = endpoints.describe("bailian_intl", base)
    check("千问云·海外版" in line and "已下线" in buf.getvalue(),
          f"老线路名 → 归一化成海外版：{line}")


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

    base_url = endpoints.default_base_url("qwencloud")

    # ① 正常：从模板拷一份（含注释）→ 两项都写进去，老的两键不再出现
    ok_cfg = d / "config.yaml"
    shutil.copyfile(ROOT / "config.example.yaml", ok_cfg)
    gui_mod._persist_provider(ok_cfg, "qwencloud", base_url)
    after = ok_cfg.read_text(encoding="utf-8")
    raw = (yaml.safe_load(after) or {}).get("session") or {}
    eq(raw.get("provider"), "qwencloud", "① 落盘 session.provider（裸写线路 id）")
    eq(raw.get("base_url"), base_url, "① 落盘 session.base_url")
    eq(raw.get("region"), None, "① 老键 region 不再写入")
    eq(raw.get("workspace_id"), None, "① 老键 workspace_id 不再写入")
    check("# " in after, "① 就地写入保住了注释")

    # ② 老配置没有 session: 段 → 必须抛错，且磁盘文件**一个字没动**
    bad_cfg = d / "legacy.yaml"
    bad_cfg.write_text("chatbox:\n  host: 127.0.0.1\n  port: 9000\n", encoding="utf-8")
    before = bad_cfg.read_text(encoding="utf-8")
    raised = ""
    try:
        gui_mod._persist_provider(bad_cfg, "qwencloud", base_url)
    except RuntimeError as exc:
        raised = str(exc)
    check(bool(raised), "② 缺 session: 段 → 抛 RuntimeError（不是静默不保存）")
    check("session" in raised, "② 报错说清缺的是哪一段（用户能自己补）")
    eq(bad_cfg.read_text(encoding="utf-8"), before, "② 失败时磁盘文件一字未改")


# ---------------------------------------------------------------- 14) 界面切线路：落盘 + key 分槽


def test_gui_provider_switch() -> None:
    """case 14：真窗口 —— 切到千问云·海外版后，config 与内存都换成海外版地址，key 槽也跟着换。

    为什么单钉：线路决定「连哪个域名 + 用哪把 key」。只改一半（忘了改 base_url、
    或忘了换 key 槽）会表现成「切了线路还是连国内」「明明填过 key 却说没配」——
    都是要翻日志才能查的漂移。
    """
    d = Path(tempfile.mkdtemp(prefix="vlt-line-"))
    cfg = d / "config.yaml"
    shutil.copyfile(ROOT / "config.example.yaml", cfg)
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

        # ---- ① 默认线路 = 千问云（老用户升级上来不该被改动）----
        eq(gui._provider(), "qianwen", "① 默认线路 = qianwen")
        eq(gui._current_key_slot(), "qianwen", "① 默认 key 槽 = qianwen")

        # ---- ② 切成海外版并保存：config、内存、key 槽三处一起换 ----
        gui._provider_var.set(gui._provider_id_to_name[endpoints.PROVIDER_QWENCLOUD])
        gui._on_save_provider()
        gui._root.update()
        raw = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("session") or {}
        eq(raw.get("provider"), "qwencloud", "② 落盘 session.provider=qwencloud")
        eq(raw.get("base_url"), "wss://maas.qwencloudapi.com/api-ws/v1/realtime",
           "② 落盘海外版 base_url")
        eq(gui._cfg.session_base["provider"], "qwencloud", "② 内存 provider 同步")
        eq(gui._cfg.session_base["base_url"], "wss://maas.qwencloudapi.com/api-ws/v1/realtime",
           "② 内存 base_url 同步（否则还连国内）")
        eq(gui._current_key_slot(), "qwencloud", "② key 槽切到海外版（两版 key 不互通）")
        check(not str(gui._provider_err.cget("text") or ""), "② 保存成功后没有红字")

        # ---- ③ 切回千问云：地址与 key 槽都要回来 ----
        gui._provider_var.set(gui._provider_id_to_name[endpoints.PROVIDER_QIANWEN])
        gui._on_save_provider()
        gui._root.update()
        raw = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("session") or {}
        eq(raw.get("provider"), "qianwen", "③ 切回后落盘 provider=qianwen")
        eq(gui._cfg.session_base["base_url"], "wss://maas.qianwenaiapi.com/api-ws/v1/realtime",
           "③ 切回后 base_url 回到国内")
        eq(gui._current_key_slot(), "qianwen", "③ key 槽切回 qianwen")
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
    test_qwencloud_derivation,
    test_retired_line_detection,
    test_custom_host_derivation,
    test_normalize,
    test_key_slots,
    test_load_config_provider,
    test_provider_host_mismatch_warn,
    test_endpoint_override,
    test_session_config_url,
    test_engine_derives_endpoints,
    test_stream_fallback_keeps_endpoint,
    test_describe_line_summary,
    test_persist_provider_guard,
    test_gui_provider_switch,
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
