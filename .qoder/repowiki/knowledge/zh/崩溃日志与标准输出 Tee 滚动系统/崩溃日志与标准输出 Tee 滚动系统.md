---
kind: logging_system
name: 崩溃日志与标准输出 Tee 滚动系统
category: logging_system
scope:
    - '**'
source_files:
    - vlt/crashlog.py
    - tests/test_log_rotation.py
    - vlt/gui.py
    - vlt/platform/audio.py
    - vlt/platform/__init__.py
---

## 1. 使用的系统与框架

本项目采用**双轨日志体系**：

- **运行时结构化日志**：使用 Python 标准库 `logging`，各模块通过 `logging.getLogger(__name__)` 获取 logger（如 `vlt/platform/audio.py`），以 `log.debug/warning` 等级别输出带模块前缀的结构化消息。
- **崩溃/启动日志**：自研 `vlt/crashlog.py`，不依赖第三方日志框架。它通过替换 `sys.stdout` / `sys.stderr`、安装 `faulthandler`、hook `sys.excepthook` / `threading.excepthook` / Tk 的 `report_callback_exception`，把**所有**标准输出、未捕获异常、原生段错误都落到磁盘文件。

日志落盘目录为 `logs/`（已在 `.gitignore`），文件名形如 `gui_YYYYMMDD_HHMMSS.log`，一次运行一个 tag（默认 `gui`）对应一个文件。

## 2. 关键文件

| 文件 | 作用 |
|---|---|
| `vlt/crashlog.py` | 崩溃日志核心：Tee、滚动切段、总量清理、zip 导出、启动信息写入 |
| `tests/test_log_rotation.py` | 滚动 + 脱敏导出的验收测试 |
| `vlt/gui.py` | GUI 入口，调用 `crashlog.install()` / `install_tk()` / `log_startup_info()` |
| `vlt/platform/audio.py` | 示例：用标准 `logging` 输出业务日志 |
| `vlt/platform/__init__.py` | 示例：`logging.getLogger(__name__).warning(...)` |

## 3. 架构与约定

### 3.1 启动流程（由 `vlt/gui.py` 驱动）

```python
crashlog.install(ROOT / "logs", "gui")
crashlog.log_startup_info(f"gui ...")
# ... 创建 Tk root ...
crashlog.install_tk(gui._root)
```

`install()` 幂等：重复调用直接返回已创建的 `_LOG_PATH`。

### 3.2 标准输出 Tee（`_Tee` 类）

- 同时写原流和日志文件；日志失败不影响主流程。
- 每条日志行首自动加 `HH:MM:SS.mmm` 时间戳（本地 UTC+8），只在换行处插入，避免打断半行写入。
- 线程安全：内部持有 `threading.Lock`。
- 只给 stdout 的 Tee 负责体积控制（stderr 量极小，共享同一文件，避免互相关闭句柄）。

### 3.3 日志滚动策略

两个阈值在 `crashlog.py` 顶部定义：

- `MAX_LOG_FILE_BYTES = 2 * 1024 * 1024`（单文件 2MB）：超过即 `_rotate()` —— 关旧文件、开新段 `xxx.N.log`，并重新启用 `faulthandler` 指向新段。
- `MAX_LOG_TOTAL_BYTES = 5 * 1024 * 1024`（总量 5MB）：每 500 次 write 触发 `_prune_logs()`，按 mtime 从最旧开始删，**当前正在写的文件永不删除**。

切段而非停止写入的设计理由（代码注释明确说明）：日志末尾才是出问题的地方，停写等于丢弃关键证据。

### 3.4 崩溃钩子三层覆盖

1. `faulthandler.enable(file=_LOG_FILE, all_threads=True)`：捕获原生崩溃（段错误/访问违规）并 dump 所有线程栈。
2. `sys.excepthook` + `threading.excepthook`：捕获主线程与子线程未处理异常。
3. `root.report_callback_exception`：接管 Tk 回调异常（Tk 默认只打印不退出，容易静默带病运行）。

每个异常块都以 `!!! <标题> <ISO 时间>` 分隔线格式写入 stderr（再被 Tee 转存到日志）。

### 3.5 启动信息（`log_startup_info`）

写入版本、Python 版本、平台、是否打包 exe、cwd、程序路径（含非 ASCII 字符告警）、git HEAD + dirty 状态、API 密钥（仅首尾 6+4 字符 + 长度）。用于拿到日志后能判断是哪个构建崩的。

### 3.6 日志导出与脱敏（`export_logs`）

- 打包 `logs/*.log*` + `config.yaml` + `config.yaml.broken` + 额外文件。
- 打包前逐文件扫描 `sk-[A-Za-z0-9_-]{10,}` 正则，匹配则替换为 `sk-****（已脱敏）`。
- 生成 `说明.txt` 记录导出时间、目录、文件数、原始字节、哪些文件被脱敏。
- 单元测试 `test_export_redacts_keys_and_includes_config` 断言 zip 中绝不含完整密钥。

## 4. 约定与约束

- **日志位置固定**：`logs/<tag>_<timestamp>.log`，tag 默认 `gui`，由 `install(log_dir, tag)` 决定。
- **时间戳格式**：行首 `HH:MM:SS.mmm`（本地时间），异常头用 ISO 秒级时间。
- **滚动阈值硬编码**：单文件 2MB、总量 5MB，位于 `crashlog.py` 模块级常量，测试通过临时修改 `crashlog.MAX_LOG_FILE_BYTES` 验证行为。
- **当前日志永不删除**：`_prune_logs` 接受 `keep={self._log_path}` 参数，确保正在写的文件不被误删（测试断言此不变式）。
- **切段后继续写**：`_rotate` 打开新段并替换 `_streams[-1]`，保证后续写入不落空（测试断言切段后内容未丢且包含“已切段”标记）。
- **崩溃钩子必须全部装上**：`install()` 同时装 faulthandler + excepthook + threading.excepthook；Tk 应用还需单独调 `install_tk(root)`。
- **业务日志统一走 `logging`**：模块内通过 `log = logging.getLogger(__name__)` 获取 logger，不使用 `print` 作为业务日志（`audio.py` 是典型模式）。
- **敏感信息脱敏**：`log_startup_info` 对 API key 做截断显示；`export_logs` 在打包前再次正则替换 `sk-*` 密钥，这是安全红线（有专门测试覆盖）。
- **日志文件编码**：统一 UTF-8，open 时 `encoding="utf-8", buffering=1`（行缓冲，保证崩溃时最后几行已在磁盘）。
- **日志目录权限容错**：`mkdir(parents=True, exist_ok=True)` 和所有 I/O 操作均 try/except，崩溃日志本身不能成为新的崩溃点。