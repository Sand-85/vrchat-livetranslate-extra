---
kind: configuration_system
name: YAML + 环境变量 + 凭据分槽的配置加载与就地持久化系统
category: configuration_system
scope:
    - '**'
source_files:
    - vlt/config.py
    - vlt/config_io.py
    - config.example.yaml
    - vlt/credentials.py
    - vlt/endpoints.py
    - vlt/paths.py
---

## 1. 总体方案

项目使用 **PyYAML** 作为配置格式，通过 `vlt/config.py` 负责加载、校验、合并默认值并构造 `AppConfig`/`Direction` dataclass；通过 `vlt/config_io.py` 提供对 `config.yaml` 的**就地文本改写**（保留注释、空行、键顺序），供 GUI 和更新模块共享。API key 不写入 YAML，而是从环境变量或界面保存的文件中读取，由 `vlt/credentials.py` 按线路（slot）分槽存储。

核心文件：
- `vlt/config.py` — 配置加载、默认值、类型转换、API key 解析
- `vlt/config_io.py` — config.yaml 就地读写工具
- `config.example.yaml` — 随程序分发的模板（首次运行复制为 `config.yaml`）
- `vlt/credentials.py` — API key 的分槽持久化（`api_key.txt` / `api_key_qwencloud.txt`）
- `vlt/endpoints.py` — provider/base_url 归一化、host 解析、已下线线路迁移
- `vlt/paths.py` — `APP_DIR` / `BUNDLE_DIR` 定位配置文件路径

## 2. 配置分层与优先级

### 2.1 配置文件来源
- `DEFAULT_CONFIG = APP_DIR / "config.yaml"` — 用户可写配置（gitignore，不出仓库）
- `EXAMPLE_CONFIG = BUNDLE_DIR / "config.example.yaml"` — 只读模板
- `ensure_config()`：若 `config.yaml` 不存在且模板存在，则自动复制一份，实现开箱即用

### 2.2 API key 解析优先级（`load_api_key`）
1. 显式传入参数（最高优先级）
2. 界面保存的 key（按 slot 分槽：`qianwen` / `qwencloud`）
3. 环境变量 `DASHSCOPE_API_KEY`
4. `~/.bailian/config.json`（bl CLI 配置，兼容 `api_key` / `apiKey` / `DASHSCOPE_API_KEY` 三个字段名）
5. 找不到 → `SystemExit("找不到 API key：…")`

注意：第 3、4 条是两条线路共用的兜底来源，不按 slot 区分；只有界面保存的 key 是按 slot 取对应那份。

### 2.3 AppConfig 结构
`AppConfig` dataclass 把原始 YAML 规范化为强类型对象：
- `session_base: dict` — 会话通用配置（model、base_url、provider、voice、glossary、api_key、reconnect_backoff、final_silence_s、fast_final_silence_s、fast_final_user_quiet_s、silence_gate_*、repeat_guard_*）
- `directions: dict[str, Direction]` — 每个方向一条会话（mine/theirs）
- `chatbox / merger / overlay / output / ui / room / desktop_overlay / text_input` — 各子系统配置段

`Direction.to_session_config(base)` 把 `Direction` + `session_base` 合并成 `SessionConfig`，其中全局 glossary 与方向级 hotwords 通过唯一的 `merge_hotwords()` 合并，方向级覆盖全局。

## 3. 就地持久化（config_io.py）

`config_io.py` 不使用 `yaml.safe_load` + `yaml.dump` 整文件重写，因为那会抹平注释、空行、键顺序。它直接操作 YAML 文本行，提供：
- `_yaml_set_in_text(text, path, value)` — 在已有路径上就地替换叶子值
- `_yaml_set_or_create(text, path, value)` — 父级缺失时补建整条链
- `_yaml_set_mapping(text, path, mapping)` — 整段块映射（如 `glossary:`）原地替换
- `_write_config_text(path, text)` — 写前用 `yaml.safe_load` 验证合法性，非法则抛 `RuntimeError` 放弃写入

关键约束（代码注释明确说明的设计取舍）：
- 多行块（列表/嵌套映射）被替换时必须一并删除子行，否则留下孤立 `- 0.0` 导致整个文件非法
- 纯注释行绝不删除，但属于该键的块行要删掉
- 长设备名等外部数据通过 `_yaml_scalar()` 走 PyYAML 安全序列化，必要时退回双引号标量
- 所有浮点数用 `_fmt_scalar` 输出紧凑形式（`0.24` 而非 `0.24000000000000002`）

## 4. 错误处理与容错约定

- 配置文件损坏：备份为 `.broken` 后缀，再从模板重建，打印清晰提示
- 非映射类型的 glossary/hotwords：打印 `[config] ⚠️ …` 后丢弃该段，不中断启动
- 非法浮点配置：通过 `_opt_float` / `_float_or_default` 留痕并回落默认值
- provider 与 base_url host 不一致：`_warn_provider_host_mismatch` 打 WARN 但不报错，地址仍以 base_url 为准
- 已下线线路（百炼·国际版）：自动迁移到千问云·海外版，并提示需要重新填 key
- `require_key=False` 时未配置 API key 不抛错，返回空串，由调用方引导用户在界面填写

## 5. 规则与约定（基于代码强制行为）

1. **配置文件路径**：用户配置位于 `APP_DIR/config.yaml`，模板位于 `BUNDLE_DIR/config.example.yaml`，两者路径由 `paths.APP_DIR` / `paths.BUNDLE_DIR` 决定。
2. **首次运行自动生成**：`ensure_config()` 保证 `config.yaml` 存在，不存在即从模板复制。
3. **API key 不落盘到 YAML**：key 仅存于 `vlt/credentials.py` 管理的独立文件中（按 slot 分槽），YAML 中的 `session.api_key` 仅在运行时注入。
4. **glossary 合并口径唯一**：全局 glossary 与方向级 hotwords 的合并只能通过 `merge_hotwords()`，避免实时会话与打字翻译两条腿出现差异。
5. **配置写入前必验**：`_write_config_text()` 在写回前调用 `yaml.safe_load(text)`，非法 YAML 直接抛 `RuntimeError`，不会写出损坏文件。
6. **老配置兼容**：缺失的 session.provider/base_url 走 `endpoints.normalize_provider` / `default_base_url` 默认值，不留痕；已下线 base_url 自动迁移。
7. **UI 状态持久化**：`ui` 段保存上次界面选择（direction/chatbox/overlay/desktop_overlay），启动时恢复。
8. **text_input.tts 默认模型**：`qwen-mt-flash`（文本翻译）与 `qwen3-tts-flash`（TTS）是硬编码默认，注释说明 `qwen3-livetranslate-flash` 的文本接口会原样回吐，不可依赖。
9. **音频采样率默认 48000**：与 VRChat/VoiceMeeter 统一要求一致。
10. **方向默认值**：若无 `directions` 段，自动创建 `mine`（source_lang=zh, target_lang=en）。