"""设置窗「输入门限」的**独立电平探针**：没在翻译时也能看到实时电平。

## 治什么

电平条以前只有一个数据源 —— **运行中引擎**的 `input_gate.level_db`。于是想调门限
就得先点「开始翻译」：可门限调的正是「多小的声音该被滤掉」，这时候音频已经在往
模型上送了（白花钱，且调的过程本身就在污染会话）。用户口径（原话）：

    「只要上面的 select 勾选了启用就会显示当前电平，
      但是也需要注意，只有设置这个窗口被打开的时候才会有」

所以这里补一路**只为看电平**的采集。开关判定不在本模块，在
`vlt/gui.py:_sync_gate_level_probe()`：设置窗可见 + 勾了「启用」+ 没在翻译 → 开；
窗口一关 / 一取消勾选 / 一开始翻译 → 立刻停并释放设备。

## 为什么必须与引擎「同一路配方」

用户是照着这条电平条调门限的 —— 探针抓的端点、重采样、电平口径只要与引擎差一点，
调出来的门限就是错的。所以这里**不另写一套**，逐环复用 `engine.run_loopback` 用的
那些函数：端点由 `pick_loopback_target` / `pick_vrchat_targets` 挑（含界面上手选的
那台设备），打开走 `platform.capture_backend().open_loopback(blocksize=CHUNK_BYTES)`，
重采样与电平用 `to_16k_mono` / `chunk_level_db`。

同一时刻**只允许一路**电平来源：有引擎就用引擎的（见 gui 侧的优先级），
再开一路 loopback 会与引擎抢同一个采集端点。

## 必须复查采集目标（Linux 的坑，实测）

`pw-record --target=<serial>` 在**目标节点消失后不会退出**：session manager 会按
`node.autoconnect` 把它**回落到默认源（麦克风）**。于是 VRChat 中途退出时，不复查的
探针既不停、也不冻住，而是继续产数据 —— 读数活得好好的，但那其实是麦克风。
用户照着它调门限，调出来的门限是照着麦克风调的。

所以探针照抄引擎那条腿的做法（`engine._pump_vrchat_capture`）：每 `VRCHAT_RECHECK_S`
复查一次 `pick_vrchat_targets()` 的 `serial` 集合，**变了立刻关掉当前一路、按新目标
重开**；目标没了就进「等待」并低频重试（VRChat 再起自动接上）。

## 线程模型

与 `Engine` 一致：daemon 线程里跑一个自己的 asyncio 事件循环。这不是可选项 ——
平台侧的 `open_loopback()` 要 `asyncio.get_running_loop()`，`AudioSource.read()`
也是协程。

⚠️ 读取一律**带超时**：端点没在出声时 WASAPI loopback 压根不产数据（见
`vlt/platform/win.py:PyaudioLoopbackSource` 的实测记录），死等会让「停止」按不下去。
超时 = 「还活着但暂时没数据」，按静音处理，**不是失败**。

⚠️ 收尾顺序照抄 `vlt/platform/audio.py:QueueAudioSource.close()` 的硬约束：
**置停止位 → join 线程 → 才碰底层资源**。反过来在 Windows 上会撞访问违规
（那边有实测记录：点「停止翻译」闪退、退出码 139）。
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable

from . import platform
from .engine import (
    CHUNK_BYTES,
    LEVEL_FLOOR_DB,
    VRCHAT_RECHECK_S,
    chunk_level_db,
    pick_loopback_target,
    pick_vrchat_targets,
    to_16k_mono,
)
from .platform.audio import MixedAudioSource

LOG = "[level]"

# Linux：枚举不到 VRChat 播放流时的统一文案。「开不出设备」与「开出来之后复查发现目标
# 已消失」在用户看来是同一件事（VRChat 没在出声），故意用**同一句话** —— 低频重试路径靠
# 「同一理由只留一行」节流，两种入口文案一致才不会交替刷屏（#26-1）。
_NO_TARGET_MSG = "没找到 VRChat 的音频输出流（VRChat 在跑并且出声了吗？）"

# 单次 read 的超时。它同时决定两件事：①「静音」多久把电平归到地板值；
# ② `stop()` 的最大延迟（线程要等这一次 read 返回才看得到停止位）。
# 取 0.2s：远小于引擎的 1.0s，关设置窗时不会有可感的卡顿。
READ_TIMEOUT_S = 0.2
JOIN_TIMEOUT_S = 3.0
# 复查采集目标的周期：与引擎那条腿同一常数，避免两条路对「流还在不在」判断不一致。
RECHECK_S = VRCHAT_RECHECK_S
# 开不出设备时的低频重试周期。取 5s：既能自愈（先开窗、后启动 VRChat），
# 又不至于把日志/CPU 刷成噪声。
RETRY_S = 5.0

# 「没传这个参数」与「显式传 None（= 不复查）」要分得开，所以用哨兵而不是 None。
_UNSET = object()


def vrchat_target_signature() -> str:
    """Linux：当前 VRChat 输出流集合的签名（`serial` 排序拼接）；空字符串 = 没目标。

    与引擎腿同一口径（`engine._pump_vrchat_capture` 里的 `sorted(t.id ...)`）：
    探针靠它判定「目标流没了 / 增减了」，进而关掉已经回落到麦克风的 `pw-record`。
    """
    return "|".join(sorted(t.id for t in pick_vrchat_targets()))


def _describe_sig(sig: str | None) -> str:
    """签名 → 日志里好读的「N 路 / 无目标」。"""
    if not sig:
        return "无目标"
    return f"{sig.count('|') + 1} 路"


def open_level_source(device_name: str | None = None) -> Any:
    """按引擎 loopback 腿的同一配方，开一路「只听电平」的采集源。

    `device_name`：界面上手选的系统声设备（`capture.loopback_device`），只在 Windows
    有意义 —— Linux 的 loopback 腿不看这个配置，目标固定是「VRChat 的播放流」
    （见 `engine._run_loopback_linux`），探针必须抓同一个东西，否则用户按这条电平
    调出来的门限对不上真正被上送的音频。

    打不开一律**抛异常**（由 `LevelProbe` 统一留痕/重试）：绝不返回 None 让调用方猜原因。
    """
    backend = platform.capture_backend()
    if platform.IS_LINUX:
        targets = pick_vrchat_targets()
        if not targets:
            raise RuntimeError(_NO_TARGET_MSG)
        opened = [backend.open_loopback(t, blocksize=CHUNK_BYTES) for t in targets]
        print(f"{LOG} 探针采集 {len(opened)} 路 VRChat 输出："
              f"{'、'.join(t.name for t in targets)}", flush=True)
        # 多路要混成一路再判电平 —— 与 engine._pump_vrchat_capture 同一口径
        return opened[0] if len(opened) == 1 else MixedAudioSource(opened)

    target = pick_loopback_target(None, device_name)
    if target is None:
        raise RuntimeError("没找到任何可采集的系统输出（音频服务正常吗？）")
    print(f"{LOG} 探针采集端点「{target.name}」{target.sample_rate}Hz "
          f"×{target.channels}ch → 16kHz 单声道", flush=True)
    return backend.open_loopback(target, blocksize=CHUNK_BYTES)


class LevelProbe:
    """一路「只为显示电平」的采集：后台线程读块 → 算 dBFS → 写 `level_db`。

    `level_db` 的语义与 `engine._LevelGate.level_db` **完全一致**：最近一块的
    瞬时电平，不做峰保。峰保只在界面那一处做（`gui._refresh_gate_level` 里
    每 100ms 掉 1.5dB），这样「有引擎」与「用探针」两条路的条子观感一模一样；
    两边各做一层峰保会双重衰减、且与引擎那条路对不上。

    `has_data`：**读到第一块真数据**之前为 False —— 界面据此显示「—」。
    起线程时 `level_db` 还是地板值，直接当读数画出来会变成假的 `-70 dB`
    （那不是读数，是把 -120 的地板 clamp 到界面下限）。

    `recheck` 依赖注入：返回「当前采集目标」的签名（Linux 默认
    `vrchat_target_signature`）。非 None 时线程每 `recheck_s` 复查一次，
    **签名变了就关掉当前一路、按新目标重开**（VRChat 退出 / 播放流增减都算）。
    传 None = 不复查（Windows 的端点由用户选、不随 PipeWire 图变；测试也用）。

    `retry_s`：**低频自愈**。开不出设备时（VRChat 还没跑 / PipeWire 还没就绪）
    不退出，每 `retry_s` 重试一次；同一失败理由只留一行日志、不刷屏。

    `opener` 依赖注入：默认打开真设备（`open_level_source`），测试塞假源 ——
    离线测试**绝不许**碰真声卡（CI 机器上根本没有）。

    跨线程只共享标量（float / int / bool / str），CPython 里读写原子、不会读到半截值 ——
    与 `gui._apply_gate_live` 同一取舍，不加锁。
    """

    def __init__(self, opener: Callable[[], Any] | None = None, *,
                 device_name: str | None = None,
                 recheck: Callable[[], str] | None | object = _UNSET,
                 read_timeout: float = READ_TIMEOUT_S,
                 join_timeout: float = JOIN_TIMEOUT_S,
                 recheck_s: float = RECHECK_S,
                 retry_s: float = RETRY_S) -> None:
        self._opener = opener or (lambda: open_level_source(device_name))
        if recheck is _UNSET:
            recheck = vrchat_target_signature if platform.IS_LINUX else None
        self._recheck: Callable[[], str] | None = recheck  # type: ignore[assignment]
        self._read_timeout = float(read_timeout)
        self._join_timeout = float(join_timeout)
        self._recheck_s = float(recheck_s)
        self._retry_s = float(retry_s)
        self.level_db = LEVEL_FLOOR_DB     # 最近一块的电平（dBFS）
        self.has_data = False              # 读到过第一块真数据吗（界面据此显示「—」）
        self.last_error: str | None = None  # 失败原因（已留痕的那一行），没失败为 None
        self.chunks = 0                    # 本次采到的块数（停止时写进日志，好核对）
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._source: Any = None
        self._running = False

    # ---------------------------------------------------------------- 对外

    @property
    def running(self) -> bool:
        """采集线程是否还活着。

        `stop()` 与读取异常之后是 False；**等待目标（VRChat 没跑）时仍为 True** ——
        那是「暂时没得采，还在等」，不是「死了」。界面靠 `has_data` 决定显示「—」。
        """
        return self._running

    def start(self) -> None:
        """起 daemon 线程开始采集。重复调用无效。

        线程内部会自己处理「目标不在 / 目标变了」：低频重试 + 复查重开
        （见类文档）。只有 stop / 读取异常才真正结束。
        """
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._thread_run, daemon=True,
                                        name="vlt-level-probe")
        self._thread.start()

    def stop(self) -> None:
        """停止采集并释放设备。幂等，可以在任何线程调。"""
        self._stop_evt.set()
        self._running = False
        th = self._thread
        if th is not None and th is not threading.current_thread() and th.is_alive():
            th.join(timeout=self._join_timeout)
            if th.is_alive():
                print(f"{LOG} ⚠️ 采集线程未在 {self._join_timeout:g}s 内退出"
                      f"（仍继续尝试关闭设备，可能有竞争）", flush=True)
        # 正常路径下线程自己的 finally 已经关过源（这里 _source 已是 None）；
        # 只有线程卡住没退出时才轮到这一手 —— 与 QueueAudioSource.close() 同一取舍。
        self._close_source()

    # ---------------------------------------------------------------- 线程内

    def _thread_run(self) -> None:
        try:
            asyncio.run(self._pump())
        except Exception as exc:  # noqa: BLE001 — 采集线程绝不能把异常抛出去
            self._fail(f"电平采集线程异常退出：{type(exc).__name__}: {exc}")
        finally:
            self._running = False

    async def _pump(self) -> None:
        """外层循环：目标不在就等（低频重试）；目标变了就按新目标重开。

        只有三种情况让线程真正退出：① 用户 stop；② 读取异常；③ 线程自身异常。
        """
        while not self._stop_evt.is_set():
            try:
                source = self._opener()
            except Exception as exc:  # noqa: BLE001
                self._enter_waiting(exc)
                if not await self._sleep(self._retry_s):
                    break
                continue
            self._source = source
            self.has_data = False          # 刚开的一路：读到第一块之前不许假装有电平
            self._note_open()
            sig = self._recheck() if self._recheck is not None else None
            if self._recheck is not None and not sig:
                # ★ 开成功、但当次复查已经看不到任何目标：VRChat 恰在这几毫秒里退出，
                #   或者这次枚举抛错（`engine.pick_vrchat_targets` 把异常吞成 `[]`）。
                #   这时 `pw-record` 已按 `node.autoconnect` 回落到麦克风 —— 若把空签名
                #   就势记成基线，之后每次 recheck 也返回空、与基线相等，就会**永不重开**，
                #   读数一直是麦克风的电平（#26-1）。所以直接关源、按「目标不在」处理，
                #   与引擎腿「want = 真正被 open 的那一批」的口径对齐。
                self._close_source()
                self._enter_waiting(RuntimeError(_NO_TARGET_MSG))
                if not await self._sleep(self._retry_s):
                    break
                continue
            try:
                again = await self._read_until_change(source, sig)
            finally:
                self._close_source()
            if not again:
                break                      # 读取异常：停下留痕，不自动重试
            # 目标变了：立刻回外层重开（不小睡，尽量少丢一小段）

    async def _read_until_change(self, source: Any, sig: str | None) -> bool:
        """读块 → 写 `level_db`，直到 stop / 读取异常 / 目标签名变化。

        返回 True = 目标变了、外层该重开；False = 该停下（stop 或读取异常）。
        """
        last_check = time.monotonic()
        while not self._stop_evt.is_set():
            try:
                chunk = await source.read(timeout=self._read_timeout)
            except Exception as exc:  # noqa: BLE001
                self._fail(f"读取系统声失败，电平条停止更新："
                           f"{type(exc).__name__}: {exc}")
                return False
            if chunk is None:
                # 超时 = 「还活着但暂时没数据」：端点静音时就是这么表现的。
                # 有数据之后必须显式写地板值 —— 否则读数会**冻在**最后一块上；
                # 还没读到第一块就保持「—」（has_data 仍 False，不假装有电平）。
                if self.has_data:
                    self.level_db = LEVEL_FLOOR_DB
            else:
                pcm16 = to_16k_mono(chunk, source.rate, source.channels)
                self.level_db = chunk_level_db(pcm16)
                self.has_data = True
                self.chunks += 1
            if sig is not None and self._recheck is not None:
                now = time.monotonic()
                if now - last_check >= self._recheck_s:
                    last_check = now
                    now_sig = self._recheck()
                    if now_sig != sig:
                        print(f"{LOG} 采集目标变化（{_describe_sig(sig)} → "
                              f"{_describe_sig(now_sig)}）→ 重开采集", flush=True)
                        return True
        return False

    # ---------------------------------------------------------------- 内部

    async def _sleep(self, seconds: float) -> bool:
        """可被 `stop()` 及时打断的等待。返回 False = 收到停止位、调用方该退出。

        分片睡（≤0.1s 一片）是必须的：整段 `asyncio.sleep(retry_s)` 会让
        `stop()` 在 Tk 主线程上干等一个 join 超时，关窗会有可感的卡顿。
        """
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            if self._stop_evt.is_set():
                return False
            await asyncio.sleep(min(0.1, end - time.monotonic()))
        return not self._stop_evt.is_set()

    def _enter_waiting(self, exc: Exception) -> None:
        """打不开设备：进「等待目标」状态（保留低频自愈），同一理由只留一行日志。"""
        self.has_data = False
        self.level_db = LEVEL_FLOOR_DB
        msg = f"打不开系统声采集，电平条不可用：{type(exc).__name__}: {exc}"
        if self.last_error != msg:         # 理由没变就不重复打，避免刷屏
            self.last_error = msg
            print(f"{LOG} ❌ {msg}（{self._retry_s:g}s 后重试；关窗或取消勾选即停止）",
                  flush=True)

    def _note_open(self) -> None:
        """一路真的开出来了：清掉失败留痕；若是从失败/等待里恢复，补一行日志。"""
        if self.last_error is not None:
            print(f"{LOG} ✅ 已接上采集目标，继续显示实时电平", flush=True)
        self.last_error = None

    def _close_source(self) -> None:
        src = self._source
        if src is None:
            return
        self._source = None
        # ★ 「关源」与「读数打回 —」要同时发生：重开一路在 Linux 上是拉起 `pw-record`
        #   的数百毫秒，这段窗口里若 `has_data` 仍为 True，界面（判据是
        #   `probe.running and probe.has_data`）会画**冻结的旧 dB**、而不是「—」（#26-2）。
        #   以前要等下一轮 `opener()` 返回才置 False，正好漏掉这段窗口。
        self.has_data = False
        try:
            src.close()
        except Exception as exc:  # noqa: BLE001 — 关不上也不该把停止流程带崩
            print(f"{LOG} ⚠️ 关闭采集源时出错（忽略）：{type(exc).__name__}: {exc}",
                  flush=True)

    def _fail(self, msg: str) -> None:
        """失败留痕（仓库硬约定：降级路径不许静默）。

        只打这一行、并**结束本次采集**：读取异常按 PR#21 的取舍不自动重试
        （流/设备坏了不是重试能救的），由用户关窗或重开触发。界面看到
        `running=False` 就把读数显示成「—」。
        """
        self.last_error = msg
        self.has_data = False
        self.level_db = LEVEL_FLOOR_DB
        print(f"{LOG} ❌ {msg}", flush=True)
