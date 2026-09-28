"""vlt/update_check.py 的离线测试（覆盖计划 Task 1~7 的全部纯逻辑）。

跑法：.venv/Scripts/python.exe tests/test_update_check.py

全程离线：HTTP 层一律用假 opener 注入（整体替换模块级 `uc._opener`），
绝不真连 GitHub —— CI 机器不一定能连外网。
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]          # 不写死本机路径：CI / 别人克隆后也能跑
sys.path.insert(0, str(ROOT))

from vlt import update_check as uc                  # noqa: E402

OUT = ROOT / "out" / "update_check"
EXE_NAME = "VRChatLiveTranslate.exe"
SUMS_NAME = "SHA256SUMS.txt"
DL_BASE = "https://github.com/nixi-agent/vrchat-livetranslate/releases/download"


# ---------------------------------------------------------------- 测试替身


class FakeResp:
    """冒充 urllib 的响应：read 可分块、有 url / headers、能当上下文管理器。"""

    def __init__(self, body: bytes, url: str = uc.RELEASES_LATEST_API,
                 headers: dict | None = None) -> None:
        self._buf = io.BytesIO(body)
        self.url = url
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """按预设脚本顺序回应：条目是 FakeResp（原样返回）或 Exception（原样抛出）。"""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls: list[str] = []
        self.reqs: list = []

    def open(self, req, timeout=None):  # noqa: ANN001, ANN201
        self.calls.append(req.full_url if hasattr(req, "full_url") else str(req))
        self.reqs.append(req)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class MapOpener:
    """按 URL 分发响应（下载测试用：exe 与 sums 两个地址各回各的）。"""

    def __init__(self, mapping: dict) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def open(self, req, timeout=None):  # noqa: ANN001, ANN201
        url = req.full_url if hasattr(req, "full_url") else str(req)
        self.calls.append(url)
        item = self.mapping[url]
        if isinstance(item, Exception):
            raise item
        return item


def payload(tag: str = "v9.9.9", *, exe_size: int | None = 12345678, drop: str = "",
            digest: str | None = "sha256:" + "a" * 64) -> bytes:
    """造一份 GitHub /releases/latest 的 JSON。

    drop='exe'/'sums' 让附件缺一个；digest=None 模拟「GitHub 没给 digest」的老形态
    （2026-09 起官方 Release 不再上传 SHA256SUMS.txt，校验值改为取 assets[].digest）。
    """
    assets = []
    if drop != "exe":
        a = {"name": EXE_NAME, "browser_download_url": f"{DL_BASE}/{tag}/{EXE_NAME}"}
        if exe_size is not None:
            a["size"] = exe_size
        if digest:
            a["digest"] = digest
        assets.append(a)
    if drop != "sums":
        assets.append({"name": SUMS_NAME,
                       "browser_download_url": f"{DL_BASE}/{tag}/{SUMS_NAME}"})
    return json.dumps({
        "tag_name": tag,
        "html_url": f"https://github.com/nixi-agent/vrchat-livetranslate/releases/tag/{tag}",
        "assets": assets,
    }).encode()


# ---------------------------------------------------------------- Task 1：版本解析与比较


def test_parse_version() -> None:
    assert uc.parse_version("0.1.1") == (0, 1, 1)
    assert uc.parse_version("v0.1.2") == (0, 1, 2)
    assert uc.parse_version(" V1.2.3 ") == (1, 2, 3)
    assert uc.parse_version("10.20.30") == (10, 20, 30)
    for bad in ("", "0.1", "0.1.2.3", "v", "abc", "0.1.2-beta", "0.1.x", "1.02.3"):
        # 1.02.3 这种带前导零的也拒掉：与 release.yml 对账正则的产物不一致就是异常
        assert uc.parse_version(bad) is None, f"应当拒绝：{bad!r}"
    print("  parse_version OK")


def test_is_newer() -> None:
    assert uc.is_newer("v0.1.2", "0.1.1")
    assert not uc.is_newer("v0.1.1", "0.1.1")
    assert not uc.is_newer("v0.1.0", "0.1.1")
    assert uc.is_newer("v1.0.0", "0.9.9")
    # 非法 tag → 不认为有更新（防御，绝不崩）
    assert not uc.is_newer("not-a-version", "0.1.1")
    print("  is_newer OK")


# ---------------------------------------------------------------- Task 2：拉取最新 Release


def test_fetch_latest_release() -> None:
    old = uc._opener
    try:
        op = FakeOpener([FakeResp(payload("v9.9.9"))])
        uc._opener = op
        info = uc.fetch_latest_release()
        assert info.version == "9.9.9" and info.tag == "v9.9.9"
        assert info.exe_url.endswith("/" + EXE_NAME)
        assert info.sums_url.endswith("/" + SUMS_NAME)
        assert info.exe_size == 12345678, f"exe_size 没带上：{info.exe_size}"
        assert info.exe_digest == "a" * 64, f"没取到 GitHub 的 digest：{info.exe_digest!r}"
        # GitHub 对无 User-Agent 的请求直接 403 —— UA 必须带
        ua = op.reqs[0].headers.get("User-agent", "")
        assert ua.startswith("vrchat-livetranslate/"), f"缺 User-Agent：{ua!r}"

        # assets[].size 缺省 → exe_size 为 None（进度条改走 Content-Length / indeterminate）
        uc._opener = FakeOpener([FakeResp(payload("v9.9.9", exe_size=None))])
        assert uc.fetch_latest_release().exe_size is None

        # ★ 没有 SHA256SUMS.txt（2026-09 起官方 Release 就不带这个文件）→ 靠 digest 照样能更新。
        #   这就是实测踩到的那个 bug：旧逻辑硬要求两个附件，删掉 sums 后更新检查直接静默失败。
        uc._opener = FakeOpener([FakeResp(payload("v9.9.9", drop="sums"))])
        _nosum = uc.fetch_latest_release()
        assert _nosum.exe_digest and not _nosum.sums_url, \
            f"缺 sums 时应当用 digest 顶上：digest={_nosum.exe_digest!r} sums={_nosum.sums_url!r}"

        # exe 缺了 → UpdateCheckError
        uc._opener = FakeOpener([FakeResp(payload("v9.9.9", drop="exe"))])
        try:
            uc.fetch_latest_release()
            raise AssertionError("缺 exe 居然没报错")
        except uc.UpdateCheckError as e:
            assert "附件" in str(e)

        # 既没 digest 也没 sums（没有任何可校验的凭据）→ 也必须报错，绝不装一个没法验的包
        uc._opener = FakeOpener([FakeResp(payload("v9.9.9", digest=None, drop="sums"))])
        try:
            uc.fetch_latest_release()
            raise AssertionError("没有任何校验凭据居然没报错")
        except uc.UpdateCheckError as e:
            assert "附件" in str(e)

        # 限流 403 / 429 → 明确提示（未认证 60 次/小时，手动按钮被连点时最容易撞）
        for code in (403, 429):
            uc._opener = FakeOpener([HTTPError(uc.RELEASES_LATEST_API, code, "x",
                                               None, io.BytesIO(b""))])
            try:
                uc.fetch_latest_release()
                raise AssertionError(f"HTTP {code} 居然没报错")
            except uc.UpdateCheckError as e:
                assert "限流" in str(e), f"HTTP {code} 提示不对：{e}"

        # 断网 → 网络不可达
        uc._opener = FakeOpener([URLError("timed out")])
        try:
            uc.fetch_latest_release()
            raise AssertionError("断网居然没报错")
        except uc.UpdateCheckError as e:
            assert "网络不可达" in str(e)

        # 非 semver tag → 报错不崩
        uc._opener = FakeOpener([FakeResp(payload("release-2024"))])
        try:
            uc.fetch_latest_release()
            raise AssertionError("坏 tag 居然没报错")
        except uc.UpdateCheckError:
            pass
        print("  fetch_latest_release OK")
    finally:
        uc._opener = old


# ---------------------------------------------------------------- Task 3：忽略列表读写


def test_fmt_scalar() -> None:
    # 既有标量行为必须与 gui.py 里的同源函数一致（配置写入的回归线）
    assert uc._fmt_scalar(None) == "null"
    assert uc._fmt_scalar(True) == "true"
    assert uc._fmt_scalar(0.24) == "0.24"
    assert uc._fmt_scalar(3) == "3"
    # 本轮新增：list → 行内流式（忽略列表写 config.yaml 要用）
    assert uc._fmt_scalar(["0.1.2", "0.1.3"]) == "[0.1.2, 0.1.3]"
    assert uc._fmt_scalar([]) == "[]"
    print("  _fmt_scalar OK")


def test_ignore_list_roundtrip() -> None:
    tmp = OUT / "ignore"
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = tmp / "config.yaml"
    cfg.write_text("# 顶部注释\nsession:\n  model: m\n", encoding="utf-8")

    assert uc.load_ignored_versions(cfg) == []
    uc.add_ignored_version(cfg, "0.1.2")
    uc.add_ignored_version(cfg, "v0.1.3")           # 带 v 也规范化存放
    uc.add_ignored_version(cfg, "0.1.2")            # 重复加 → 不重复
    assert uc.load_ignored_versions(cfg) == ["0.1.2", "0.1.3"]

    text = cfg.read_text(encoding="utf-8")
    assert "session:" in text and "model: m" in text, "其它段被破坏"
    assert "# 顶部注释" in text, "注释被吃掉"
    assert "update_ignored: [0.1.2, 0.1.3]" in text, "不是行内流式列表写法"

    # 版本号不合法 → 明确报错，文件不动
    before = cfg.read_text(encoding="utf-8")
    try:
        uc.add_ignored_version(cfg, "not-a-version")
        raise AssertionError("坏版本号居然没报错")
    except uc.UpdateCheckError:
        pass
    assert cfg.read_text(encoding="utf-8") == before

    # 坏文件 / 缺失文件 → 空列表不崩
    assert uc.load_ignored_versions(tmp / "nope.yaml") == []
    cfg.write_text("ui: [\n", encoding="utf-8")
    assert uc.load_ignored_versions(cfg) == []
    # 坏 YAML 上 add → UpdateCheckError（绝不能把坏文件写得更坏）
    try:
        uc.add_ignored_version(cfg, "0.1.2")
        raise AssertionError("坏 YAML 居然能往里写")
    except uc.UpdateCheckError:
        pass
    print("  ignore list OK")


# ---------------------------------------------------------------- Task 4：决策 + 一次性检查入口


def test_should_prompt_and_check() -> None:
    info = uc.ReleaseInfo(tag="v0.2.0", version="0.2.0", html_url="h", exe_url="e", sums_url="s")
    assert uc.should_prompt(info, "0.1.1", [])
    assert not uc.should_prompt(info, "0.1.1", ["0.2.0"])
    assert not uc.should_prompt(info, "0.2.0", [])

    old = uc._opener
    tmp = OUT / "check"
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = tmp / "config.yaml"
    cfg.unlink(missing_ok=True)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            # 1) 有更新
            uc._opener = FakeOpener([FakeResp(payload("v9.9.9"))])
            status, got = uc.check_for_updates("0.1.1", cfg)
            assert status == "update" and got is not None and got.version == "9.9.9"
            # 2) 已是最新
            uc._opener = FakeOpener([FakeResp(payload("v0.1.1"))])
            status, got = uc.check_for_updates("0.1.1", cfg)
            assert status == "latest" and got is not None
            # 3) 在忽略列表
            uc.add_ignored_version(cfg, "9.9.9")
            uc._opener = FakeOpener([FakeResp(payload("v9.9.9"))])
            status, got = uc.check_for_updates("0.1.1", cfg)
            assert status == "ignored" and got is not None
            # 4) 失败：只留日志不抛、info=None
            uc._opener = FakeOpener([URLError("timed out")])
            status, got = uc.check_for_updates("0.1.1", cfg)
            assert status == "error" and got is None
        logs = buf.getvalue()
        # 留痕是硬要求：成功/跳过/失败每个分支都要有一行 [update] 日志
        for needle in ("检查中", "发现新版本 v9.9.9（当前 v0.1.1）", "已是最新",
                       "忽略列表，跳过", "检查失败"):
            assert needle in logs, f"[update] 日志缺：{needle}"
        print("  should_prompt / check_for_updates OK")
    finally:
        uc._opener = old


# ---------------------------------------------------------------- Task 5：下载 + SHA256 + 白名单


def test_download_and_verify() -> None:
    exe_bytes = b"fake-exe-" * 1000
    good = hashlib.sha256(exe_bytes).hexdigest()
    sums = f"{good}  {EXE_NAME}\n".encode()
    exe_url = f"{DL_BASE}/v9.9.9/{EXE_NAME}"
    sums_url = f"{DL_BASE}/v9.9.9/{SUMS_NAME}"
    info = uc.ReleaseInfo(tag="v9.9.9", version="9.9.9", html_url="h",
                          exe_url=exe_url, sums_url=sums_url, exe_size=len(exe_bytes))
    tmp = OUT / "download"
    tmp.mkdir(parents=True, exist_ok=True)
    dest = tmp / (EXE_NAME + ".new")
    dest.unlink(missing_ok=True)

    old = uc._opener
    try:
        # 通过路径：内容一致、progress 按块回报、总量取 Content-Length
        uc._opener = MapOpener({
            sums_url: FakeResp(sums, url=sums_url),
            exe_url: FakeResp(exe_bytes,
                              url="https://objects.githubusercontent.com/real-exe",
                              headers={"Content-Length": str(len(exe_bytes))}),
        })
        calls: list[tuple[int, int | None]] = []
        out = uc.download_and_verify(info, tmp, progress=lambda d, t: calls.append((d, t)))
        assert out == dest and out.read_bytes() == exe_bytes
        assert calls and calls[-1] == (len(exe_bytes), len(exe_bytes)), f"progress 不对：{calls[-1:]}"
        assert all(t == len(exe_bytes) for _, t in calls)
        # 校验通过 → 写下 update_pending.json（【稍后】退出时替换 / 残留恢复的复验凭据）
        meta = json.loads((tmp / uc.PENDING_JSON).read_text(encoding="utf-8"))
        assert meta == {"version": "9.9.9", "sha256": good}, f"pending json 不对：{meta}"
        out.unlink()

        # 没有 Content-Length → total=None（进度条降级显示，绝不除零）
        uc._opener = MapOpener({sums_url: FakeResp(sums, url=sums_url),
                                exe_url: FakeResp(exe_bytes, url=exe_url)})
        calls.clear()
        out = uc.download_and_verify(info, tmp, progress=lambda d, t: calls.append((d, t)))
        assert calls and all(t is None for _, t in calls) and calls[-1][0] == len(exe_bytes)
        out.unlink()

        # 校验值对不上 → UpdateCheckError 且下载物被删
        bad_good = good[:-1] + ("0" if good[-1] != "0" else "1")
        uc._opener = MapOpener({sums_url: FakeResp(f"{bad_good}  {EXE_NAME}\n".encode(),
                                                   url=sums_url),
                                exe_url: FakeResp(exe_bytes, url=exe_url)})
        try:
            uc.download_and_verify(info, tmp)
            raise AssertionError("校验不一致居然没报错")
        except uc.UpdateCheckError:
            pass
        assert not dest.exists(), "校验失败后下载物没被删除"

        # sums 里没有 exe 那一行 → 报错
        uc._opener = MapOpener({sums_url: FakeResp(b"deadbeef  other.bin\n", url=sums_url)})
        try:
            uc.download_and_verify(info, tmp)
            raise AssertionError("sums 缺行居然没报错")
        except uc.UpdateCheckError:
            pass

        # 最终落地地址跑出白名单 → 拦（响应里的 url 模拟被改写后的落地地址）
        uc._opener = MapOpener({sums_url: FakeResp(sums, url=sums_url),
                                exe_url: FakeResp(exe_bytes, url="https://evil.example.com/x")})
        try:
            uc.download_and_verify(info, tmp)
            raise AssertionError("白名单外的落地地址居然放行")
        except uc.UpdateCheckError:
            pass
        assert not dest.exists()

        # 下载中途断网 → UpdateCheckError + 无残留
        class _Boom:
            url = exe_url
            headers: dict = {}

            def read(self, n: int = -1) -> bytes:
                raise URLError("connection reset")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        uc._opener = MapOpener({sums_url: FakeResp(sums, url=sums_url), exe_url: _Boom()})
        try:
            uc.download_and_verify(info, tmp)
            raise AssertionError("中途断网居然没报错")
        except uc.UpdateCheckError:
            pass
        assert not dest.exists(), "中途失败后残留没被清理"
        print("  download_and_verify OK")
    finally:
        uc._opener = old


def test_redirect_guard() -> None:
    h = uc._GuardedRedirectHandler()
    req = Request(f"{DL_BASE}/v9.9.9/{EXE_NAME}")

    # 白名单内的 https 跳转 → 放行（exe 下载正常就会 302 到 CDN）
    new = h.redirect_request(req, None, 302, "Found", {},
                             "https://objects.githubusercontent.com/the-real-asset")
    assert new is not None and new.full_url.startswith("https://objects.githubusercontent.com/")

    # 白名单外 host → 拦
    try:
        h.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/x")
        raise AssertionError("跳到白名单外居然放行")
    except uc.UpdateCheckError:
        pass

    # 降级成 http → 拦（哪怕 host 在白名单里）
    try:
        h.redirect_request(req, None, 302, "Found", {}, "http://github.com/x")
        raise AssertionError("http 降级居然放行")
    except uc.UpdateCheckError:
        pass
    print("  redirect guard OK")


# ---------------------------------------------------------------- Task 6：运行形态判定


def test_update_mode() -> None:
    mode = uc.update_mode()
    assert mode in ("frozen", "source")
    # 本测试在源码 checkout 里跑 → 必然是 source
    assert mode == "source", f"源码运行却判定成 {mode}"
    print("  update_mode OK")


# ---------------------------------------------------------------- Task 7：更新器 bat 生成


def test_build_updater_bat() -> None:
    bat = uc.build_updater_bat(
        pid=4321,
        current_exe=Path(r"C:\Users\A B\Desktop\VRChatLiveTranslate.exe"),
        new_exe=Path(r"C:\Users\A B\Desktop\VRChatLiveTranslate.exe.new"))
    assert "\r\n" in bat, "bat 必须 CRLF"
    assert "\n" not in bat.replace("\r\n", ""), "混进了裸 LF（cmd 对 LF-only 的 label/goto 会抽风）"
    for needle in ("4321", r'"C:\Users\A B\Desktop\VRChatLiveTranslate.exe"',
                   r'"C:\Users\A B\Desktop\VRChatLiveTranslate.exe.new"',
                   "tasklist", "move /y", 'start ""', 'del "%~f0"', ".bak",
                   "update_failed.log", ":fail", "pause"):
        assert needle in bat, f"bat 缺 {needle}"
    # ★ 替换必须带重试（真机实测）：PyInstaller 单文件的**父进程**比子进程晚一步释放 exe 句柄，
    #   子进程一消失就 move 会撞共享冲突 → 替换失败回滚、用户升了个寂寞。没有这个循环就是回归。
    assert ":trymove" in bat and "goto trymove" in bat, "替换没有重试循环"
    assert ":restore" in bat, "重试用尽后应先从 .bak 还原再报失败"
    assert ">nul 2>&1" in bat, "重试时要把 move 的报错也吞掉（否则黑窗刷错误）"
    print("  build_updater_bat OK")


# ---------------------------------------------------------------- Task 11：阶段二纯逻辑


def test_build_updater_bat_relaunch() -> None:
    cur = Path(r"C:\Users\A B\Desktop\VRChatLiveTranslate.exe")
    new = Path(r"C:\Users\A B\Desktop\VRChatLiveTranslate.exe.new")
    bat_on = uc.build_updater_bat(pid=4321, current_exe=cur, new_exe=new, relaunch=True)
    bat_off = uc.build_updater_bat(pid=4321, current_exe=cur, new_exe=new, relaunch=False)

    # 【重载】形态：替换后 start "" 拉起新版；【稍后】退出时替换：只换不拉
    assert f'start "" "{cur}"' in bat_on, "relaunch=True 缺拉起行"
    assert 'start ""' not in bat_off, "relaunch=False 不该拉起新进程"
    # ★ 拉起新实例前必须清掉 PyInstaller 的内部变量（真机实测的启动失败根因）：
    #   新版本会带着 _PYI_PARENT_PROCESS_LEVEL=1 启动 → 引导器以为自己是已解包的子进程
    #   → 跳过自解包 → 起不来（不写日志、无窗口）。用户看到的就是「更新完没再打开」。
    for var in ("_MEIPASS", "_MEIPASS2", "_PYI_ARCHIVE_FILE",
                "_PYI_PARENT_PROCESS_LEVEL", "_PYI_APPLICATION_HOME_DIR"):
        assert f'set "{var}="' in bat_on, f"拉起前没有清 {var} —— 新实例会起不来"
        assert f'set "{var}="' not in bat_off, f"relaunch=False 不该出现清理 {var} 的行"
    # 两形态除「拉起块（清理 + start 行）」外其余逐字节相同
    extra = [ln for ln in bat_on.splitlines(keepends=True) if ln not in bat_off]
    assert extra, "两形态居然完全一样"
    assert all(ln.startswith(('start ""', 'set "_', "rem drop"))
               for ln in extra), f"多出来不只是拉起块：{extra}"
    stripped = bat_on
    for ln in extra:
        stripped = stripped.replace(ln, "", 1)
    assert stripped == bat_off, "两形态差异不止拉起块"
    for needle in ("tasklist", "move /y", 'del "%~f0"', ".bak", ":fail"):
        assert needle in bat_off, f"relaunch=False 缺 {needle}"
    # 默认参数必须保持 True（既有行为不变）
    assert uc.build_updater_bat(pid=1, current_exe=cur, new_exe=new) == bat_on.replace(
        "4321", "1"), "默认值不是 relaunch=True"
    print("  build_updater_bat relaunch 两形态 OK")


def test_pending_download() -> None:
    tmp = OUT / "pending"
    tmp.mkdir(parents=True, exist_ok=True)
    exe = tmp / EXE_NAME
    exe.write_bytes(b"old")                          # 当前 exe（内容无所谓）
    new_exe = tmp / (EXE_NAME + ".new")
    pending = tmp / uc.PENDING_JSON
    new_exe.unlink(missing_ok=True)
    pending.unlink(missing_ok=True)

    # 什么都没有 → None，且一行日志都不打（每次启动都走的正常路）
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert uc.check_pending_download(exe) is None
    assert buf.getvalue() == "", f"无残留不该刷日志：{buf.getvalue()!r}"

    # 完好：写真 bytes + write_pending → 复核通过，返回 (version, sha256)
    body = b"new-exe-" * 1000
    new_exe.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    p = uc.write_pending(tmp, "9.9.9", sha)
    assert p == pending and json.loads(p.read_text(encoding="utf-8")) == {
        "version": "9.9.9", "sha256": sha}
    buf = io.StringIO()
    with redirect_stdout(buf):
        hit = uc.check_pending_download(exe, current_version="0.1.1")
    assert hit == ("9.9.9", sha), f"完好残留没命中：{hit}"
    assert "复核通过" in buf.getvalue() and "9.9.9" in buf.getvalue(), "命中没留痕"
    assert new_exe.exists() and pending.exists(), "命中后文件不该被动"

    # 损坏：改一个字节 → None，两个文件都被清理，留痕
    new_exe.write_bytes(body + b"x")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert uc.check_pending_download(exe, current_version="0.1.1") is None
    assert not new_exe.exists() and not pending.exists(), "损坏残留没被清理"
    assert "复核不通过" in buf.getvalue(), "损坏分支没留痕"

    # json 损坏 / 缺字段 → None + 清理 + 留痕
    for bad_json in ("{not json", json.dumps({"version": "9.9.9"}),  # 缺 sha256
                     json.dumps({"version": "not-a-version", "sha256": sha}),
                     json.dumps({"version": "9.9.9", "sha256": "abc"})):
        new_exe.write_bytes(body)
        pending.write_text(bad_json, encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert uc.check_pending_download(exe) is None, f"坏 json 居然放行：{bad_json!r}"
        assert not new_exe.exists() and not pending.exists()
        assert "记录文件损坏" in buf.getvalue(), f"坏 json 分支没留痕：{bad_json!r}"

    # 只剩 json / 只剩 .new → None + 清理 + 留痕（两种残缺都不能放行）
    pending.write_text(json.dumps({"version": "9.9.9", "sha256": sha}), encoding="utf-8")
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert uc.check_pending_download(exe) is None
    assert not pending.exists() and "下载物缺失" in buf.getvalue()

    new_exe.write_bytes(body)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert uc.check_pending_download(exe) is None
    assert not new_exe.exists() and "缺少记录文件" in buf.getvalue()

    # 待替换版本不比当前新（上次其实换成功了，只是残留没清）→ 清理 + 留痕
    new_exe.write_bytes(body)
    uc.write_pending(tmp, "9.9.9", sha)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert uc.check_pending_download(exe, current_version="9.9.9") is None
    assert not new_exe.exists() and not pending.exists()
    assert "不比当前" in buf.getvalue(), "换完残留分支没留痕"
    # 当前版本号不是严格 X.Y.Z（测试版/预发布版，如 0.4.0-beta.1）→ 新旧无法比较，
    # ⚠️ 绝不能当成「换完剩下的残留」删掉（is_newer 对不可解析版本一律返回 False，
    #    那是防御性默认值，不是「不比当前新」的结论）。
    new_exe.write_bytes(body)
    uc.write_pending(tmp, "9.9.9", sha)
    buf = io.StringIO()
    with redirect_stdout(buf):
        hit = uc.check_pending_download(exe, current_version="0.4.0-beta.1")
    assert hit is not None, "当前版本号不可解析时，校验完好的待更新文件被误判成残留丢弃"
    assert new_exe.exists() and pending.exists(), "文件不该被清掉"
    assert "无法比较" in buf.getvalue(), "无法比较分支没留痕"
    new_exe.unlink()
    pending.unlink()

    print("  write_pending / check_pending_download OK")


def test_last_seen_version() -> None:
    tmp = OUT / "state"
    tmp.mkdir(parents=True, exist_ok=True)
    state = tmp / uc.STATE_JSON
    state.unlink(missing_ok=True)

    assert uc.load_last_seen_version(tmp) is None, "缺失文件应视为首次"
    uc.save_last_seen_version(tmp, "0.1.1")
    assert uc.load_last_seen_version(tmp) == "0.1.1"
    uc.save_last_seen_version(tmp, "0.1.2")            # 覆盖写（提示后写回当前版本）
    assert uc.load_last_seen_version(tmp) == "0.1.2"
    state.write_text("{broken", encoding="utf-8")
    assert uc.load_last_seen_version(tmp) is None, "损坏文件应视为首次"
    state.write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert uc.load_last_seen_version(tmp) is None, "缺字段应视为首次"
    print("  load/save_last_seen_version OK")


def test_expected_sha256_prefers_github_digest() -> None:
    """校验值优先取 GitHub 的 `assets[].digest`；只有老 Release 没 digest 才回退拉 SHA256SUMS.txt。

    这条守的是「删掉 SHA256SUMS.txt 之后更新链路还能用」——真机实测就是这么坏的：
    旧逻辑硬要求两个附件，官方 Release 不再上传 sums 后，更新检查在真 exe 上直接静默失败
    （用户什么提示都看不到，等于永远收不到新版本）。
    """
    old = uc._opener
    try:
        info = uc.ReleaseInfo(tag="v1.0.0", version="1.0.0", html_url="h",
                              exe_url=f"{DL_BASE}/v1.0.0/{EXE_NAME}", exe_digest="b" * 64)
        uc._opener = MapOpener({})          # 空映射：有 digest 时**一次网络都不该走**
        assert uc._expected_sha256(info, 1.0) == "b" * 64, "有 digest 时不该再去拉文件"

        legacy = uc.ReleaseInfo(tag="v1.0.0", version="1.0.0", html_url="h",
                                exe_url=f"{DL_BASE}/v1.0.0/{EXE_NAME}",
                                sums_url=f"{DL_BASE}/v1.0.0/{SUMS_NAME}")
        uc._opener = MapOpener({f"{DL_BASE}/v1.0.0/{SUMS_NAME}":
                                FakeResp(f"{'c' * 64}  {EXE_NAME}\n".encode(),
                                         url=f"{DL_BASE}/v1.0.0/{SUMS_NAME}")})
        assert uc._expected_sha256(legacy, 1.0) == "c" * 64, "没 digest 时应回退到 sums 文件"
        print("  _expected_sha256 OK（GitHub digest 优先，sums 仅老版本兜底）")
    finally:
        uc._opener = old


def main() -> int:
    print("test_update_check:")
    tests = [
        test_parse_version,
        test_is_newer,
        test_fetch_latest_release,
        test_fmt_scalar,
        test_ignore_list_roundtrip,
        test_should_prompt_and_check,
        test_download_and_verify,
        test_expected_sha256_prefers_github_digest,
        test_redirect_guard,
        test_update_mode,
        test_build_updater_bat,
        test_build_updater_bat_relaunch,
        test_pending_download,
        test_last_seen_version,
    ]
    bad = []
    for fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001 — 任何一个断言挂都要退出码非 0，但其余用例照跑
            bad.append(fn.__name__)
            print(f"  ✗ {fn.__name__} 失败：")
            traceback.print_exc()
    print("ALL PASSED" if not bad else f"FAILED: {', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
