---
kind: error_handling
name: Python 应用级异常与崩溃日志体系（TtsError + crashlog）
category: error_handling
scope:
    - '**'
source_files:
    - vlt/tts.py
    - vlt/crashlog.py
    - vlt/credentials.py
    - vlt/config_io.py
    - vlt/app.py
    - vlt/gui.py
    - vlt/ui_text.py
---

## 1. 采用的方案

仓库没有引入第三方错误框架，而是基于 Python 内置异常体系自建了两层：

- **领域异常类**：在 `vlt/tts.py` 中定义 `TtsError(RuntimeError)` 及其子类 `TtsStreamTruncated(TtsError)`，作为 TTS 子系统的统一对外异常；其他模块则直接抛 `ValueError` / `RuntimeError` / `SystemExit`。
- **崩溃日志系统**：`vlt/crashlog.py` 通过 `faulthandler`、`sys.excepthook`、`threading.excepthook`、`root.report_callback_exception` 四道钩子捕获原生崩溃、未捕获异常、线程异常和 Tk 回调异常，并把 stdout/stderr 用自定义 `_Tee` 流重定向到带时间戳的日志文件，同时实现按大小切段与总量滚动清理。

## 2. 关键文件与包

| 文件 | 职责 |
|---|---|
| `vlt/tts.py` | 定义 `TtsError`、`TtsStreamTruncated`，所有 TTS 调用路径统一包装为 `TtsError` |
| `vlt/crashlog.py` | 安装全局异常钩子、stdout/stderr Tee、日志滚动、zip 导出与脱敏 |
| `vlt/credentials.py` | 校验密钥槽名/长度等输入，抛 `ValueError` |
| `vlt/config_io.py` | YAML 写入失败时抛 `RuntimeError`（`from exc` 链式） |
| `vlt/app.py` | 顶层入口，`KeyboardInterrupt` 与 `SystemExit` 处理 |
| `vlt/gui.py` | GUI 内部使用局部异常 `_DownloadCancelled`（继承自 `Exception`），并显式 `raise FileNotFoundError(path)` |
| `vlt/ui_text.py` | 消费 `TtsError`，判断「音色不被该模型支持」做差异化 UI 提示 |

## 3. 架构与约定

### 3.1 领域异常分层

```python
class TtsError(RuntimeError): ...          # 基类：合成失败（缺 key / 网络 / 参数 / 空音频）
class TtsStreamTruncated(TtsError): ...   # 子类：流式中途断，但已 yield 的分片有效
```

- 所有 TTS 外部 I/O（HTTP、base64、miniaudio 解码、JSON 解析）都被 try/except 包裹后重新抛出 `TtsError(...)`，并使用 `from exc` 保留原始堆栈链。
- `TtsStreamTruncated` 携带一条调用方约定：**不要把已经推给声卡的部分撤掉，也不要在状态栏报「合成失败」**，改为 warn 级提示——这是文档化的语义约束，而非类型强制。
- 非 TTS 模块不定义自定义异常类，直接使用 `ValueError`（如 `credentials.py` 对非法 slot/key 的校验）、`RuntimeError`（如 `config_io.py` 对非法 YAML 输出）、`SystemExit`（如 `app.py`、`config.py` 对缺失 API key 的启动期退出）。

### 3.2 崩溃日志（crashlog）

`install(log_dir, tag="gui")` 是幂等的单点安装函数，依次完成：

1. 创建 `logs/{tag}_{YYYYMMDD_HHMMSS}.log`。
2. 用 `_Tee` 替换 `sys.stdout` / `sys.stderr`，每条行首自动加 `HH:MM:SS.mmm` 本地时间戳。
3. `faulthandler.enable(file=_LOG_FILE, all_threads=True)` 捕获段错误 / 访问违规。
4. 覆盖 `sys.excepthook` 与 `threading.excepthook`，把主线程与子线程未捕获异常打印到 stderr（即日志文件）。
5. `install_tk(root)` 设置 `root.report_callback_exception`，接管 Tk 回调异常（Tk 默认只打印不退出，容易静默带病运行）。

日志滚动策略：
- 单文件上限 `MAX_LOG_FILE_BYTES = 2MB`，超过后 `_rotate()` 关闭旧句柄、打开新段（`{stem}.{seg}.log`），并重新启用 `faulthandler`。
- 总容量上限 `MAX_LOG_TOTAL_BYTES = 5MB`，`_prune_logs` 删除最旧的 `.log*` 文件（当前正在写的永不删）。
- 打包导出 `export_logs()` 会扫描 `sk-[A-Za-z0-9_-]{10,}` 模式并替换为 `sk-****（已脱敏）`，返回被脱敏的文件清单。

### 3.3 降级与“禁静默降级”约定

`tts.py` 中的 `_note()` 注释明确声明仓库约定：**禁静默降级，每一处降级都要能查**。`_note()` 本身用 try/except Exception 保护，确保日志子系统不会反噬主流程。SSE 流中断时 `_cut_note()` 统一记录分片数并生成用户可见消息。

### 3.4 顶层入口的错误传播

- `app.py` 中 `main()` 正常返回时由 `raise SystemExit(main())` 转为退出码；`KeyboardInterrupt` 被显式捕获以优雅退出。
- `config.py` 在找不到 API key 时直接 `raise SystemExit(...)`，让上层统一处理。
- `config_io.py` 在生成的 YAML 不合法时 `raise RuntimeError(...)` 并用 `from exc` 保留底层原因。

## 4. 观察到的约定与约束

- **TTS 子系统对外异常必须走 `TtsError`**：`synthesize`、`synthesize_stream`、`synthesize_omni` 等所有公开路径最终都 `raise TtsError(...)`，调用方（如 `ui_text.py`）按 `isinstance(exc, TtsError)` 或具体子类分支处理。
- **异常链保留**：TTS 中几乎所有 `raise TtsError(...)` 都带 `from exc`，以便 traceback 显示原始 HTTPError / URLError / JSONDecodeError。
- **GUI 内部取消信号用局部异常**：`gui.py` 定义 `_DownloadCancelled` 仅用于下载取消分支，不在模块外暴露。
- **日志子系统自身不可失败**：`_Tee.write`、`_rotate`、`_prune_logs`、`export_logs` 中对 `OSError` / `Exception` 的捕获都是 `pass` 或只写一行告警，保证日志功能退化不影响主程序。
- **日志文件命名与位置固定**：`logs/{tag}_{YYYYMMDD_HHMMSS}.log`，一次运行一个文件，由 `crashlog.install` 唯一创建。
- **敏感信息脱敏**：`export_logs` 对 `sk-*` 模式进行正则替换，并在说明中列出被脱敏的文件名，避免“默默改了内容”。
- **无全局错误码枚举**：仓库未定义统一的错误码常量表，错误语义通过异常类型 + 人类可读消息传递。
- **无中间件层**：项目是桌面 GUI 应用，不存在 Web 中间件式的错误拦截器；错误处理集中在各模块的 try/except 与顶层 crashlog 钩子。