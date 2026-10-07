---
kind: dependency_management
name: Python 依赖管理：分层 requirements + CI 门禁 + uv/pip 双栈安装
category: dependency_management
scope:
    - '**'
source_files:
    - requirements.txt
    - requirements-windows.txt
    - requirements-linux.txt
    - requirements-dev.txt
    - .github/workflows/ci.yml
    - setup.sh
    - setup.bat
    - run_overlay.sh
---

## 1. 使用的系统与工具

- **包管理器**：`pip`（标准库 `venv`）与 `uv`（可选加速/自举解释器）。`setup.sh` 优先检测 `uv`，回退到系统 `python3.11 -m venv`。
- **CI**：GitHub Actions（`.github/workflows/ci.yml`、`release.yml`、`appimage.yml`），固定 Python 3.11，在 Windows 与 Ubuntu runner 上分别安装平台专属依赖。
- **打包**：PyInstaller（`scripts/build_exe.py`，CI 中要求 `pyinstaller>=6.6`），产物为 `dist/VRChatLiveTranslate.exe`。
- **静态检查/测试**：`ruff`（仅 `F,E9` 规则阻断）、`coverage`（非阻断摘要输出）、`pytest`（通过 `python -m coverage run` 驱动）。
- **虚拟环境**：仓库根 `.venv/`，由 `setup.bat` / `setup.sh` 创建，不纳入版本控制（见 `.gitignore`）。

## 2. 关键文件

| 文件 | 作用 |
|---|---|
| `requirements.txt` | 公共依赖（Windows/Linux 通用），使用 `>=` 宽松下限 |
| `requirements-windows.txt` | Windows 独占依赖，通过 `-r requirements.txt` 复用公共层 |
| `requirements-linux.txt` | Linux 独占依赖，同样 `-r requirements.txt` |
| `requirements-dev.txt` | 开发期依赖（ruff、coverage），不进产品运行环境 |
| `.github/workflows/ci.yml` | CI 中按平台安装对应 requirements 并断言导入 |
| `setup.sh` | Linux 端一键安装脚本，同时体检系统级依赖（PipeWire、OpenXR、libEGL 等） |
| `setup.bat` | Windows 端安装入口，调用 `pip install -r requirements-windows.txt` |
| `run_overlay.sh` | 手腕屏运行时入口，校验 pyopenxr 是否可用 |

## 3. 架构与约定

### 三层依赖拆分
项目采用「公共 + 平台独占」的三层结构：
- `requirements.txt` 声明跨平台依赖（`python-osc`、`Pillow`、`websockets`、`numpy`、`sounddevice`、`miniaudio`、`PyYAML`），注释明确说明「平台独占的依赖不要写在这里」。所有平台专属包都放在 `requirements-windows.txt` 或 `requirements-linux.txt`，并通过 `-r requirements.txt` 继承公共层。
- `requirements-windows.txt` 包含 `PyAudioWPatch`（WASAPI loopback）和 `openvr`（SteamVR overlay），注释强调二者在 Linux 无 wheel 或走自建 OpenXR 路径，故不能放入公共清单。
- `requirements-linux.txt` 包含 `pyopenxr`（自建 OpenXR overlay），其余 Linux 功能（系统声采集、虚拟声卡、麦克风）走系统命令或 C 扩展，无需额外 Python 包。

### 版本策略
- 生产依赖统一使用 `>=X.Y` 宽松下限（如 `python-osc>=1.9.0`、`websockets>=12.0`），注释写明「本机验证过的版本写在注释里；用 >= 是为了在别的机器上能解析到可用 wheel」——即不锁定精确版本，也不生成 lockfile。
- 开发依赖同样用 `>=`（`ruff>=0.6`、`coverage>=7.0`），但单独一份文件避免污染运行环境。

### CI 中的依赖门禁
- CI 在 Windows job 中执行 `import openvr, pyaudiowpatch` 作为「红灯门禁」，确保 PR 没有漏装 Windows 独占依赖（注释引用了 PR #4 审查发现的漏检类型）。
- CI 在打包 job 中再次断言同样的导入，保证发布产物构建时依赖齐全。
- 静态检查以 `ruff check --select F,E9 .` 阻断，覆盖率只出摘要不设阈值。
- Linux job 额外安装 xvfb、fonts-noto-cjk、tk、libportaudio2、libegl1、libwayland-client0 等系统依赖，并在 Tk 不可用时跳过 GUI 相关测试而非静默失败。

### 本地安装流程
- `setup.sh` 先尝试 `uv venv --python 3.11`，若系统有 `python3.11` 则回退到 `python3.11 -m venv`；随后优先用 `uv pip install`，否则用 `python -m pip install` 安装 `requirements-linux.txt`。
- 安装后执行「系统依赖体检」：检查 `pw-dump`/`pw-record`/`pw-cat`、`pyopenxr`、OpenXR 运行时（Monado/WiVRn）、`libEGL`/`libwayland-client`、`libportaudio`，缺失项打印 pacman 安装提示。
- Windows 侧通过 `setup.bat` 与 `run_gui.bat` 等批处理脚本调用 `pip install -r requirements-windows.txt`。

### 打包约束
- CI 的 package job 要求 `dist/VRChatLiveTranslate.exe` 体积 ≥ 20MB，否则视为「打包不完整」。
- 通过 `scripts/check_platform_purity.py dist/VRChatLiveTranslate.exe --platform windows` 断言 Windows 产物不含 Linux 独占实现（基于解包后的字节码扫描，而非原始 exe 字节）。

## 4. 约定与约束

- **公共/平台依赖分离**：`requirements.txt` 不得包含平台独占包；Windows 独占放 `requirements-windows.txt`，Linux 独占放 `requirements-linux.txt`。该约定由文件头注释与 CI 导入断言共同保障。
- **版本范围使用 `>=`**：不在仓库中维护 lockfile，依赖精确版本由 PyPI 在目标机器上解析到可用 wheel。来源：`requirements.txt` 顶部注释。
- **CI 必须能在无 API key、无麦克风/VRChat/SteamVR 的环境下跑**：需要真实硬件/API 的用例（如 `test_engine.py`）显式跳过，不得静默失败。来源：`ci.yml` 注释与 `SKIP`/`ALWAYS_SKIP` 列表。
- **Windows 独占依赖必须在 CI 中可 import**：`import openvr, pyaudiowpatch` 是阻断性门禁。来源：`.github/workflows/ci.yml` 两个 job 中的断言步骤。
- **打包产物必须不含 Linux 独占实现**：通过 `check_platform_purity.py` 对 PYZ 字节码做平台隔离校验。来源：`ci.yml` 的「断言平台隔离」步骤及 `docs/平台约束记录.md` 第三节。
- **Linux 安装需 Python 3.11**：`setup.sh` 硬编码 3.11，因为部分依赖在该版本才有 wheel。来源：`setup.sh` 第 6 行注释。
- **系统级依赖与 Python 依赖分开管理**：PipeWire、OpenXR 运行时、libEGL/libwayland-client、libportaudio 等不属于 pip 包，由 `setup.sh` 体检并给出 pacman 安装提示。来源：`requirements-linux.txt` 注释与 `setup.sh` 的 `check_cmd` 逻辑。
- **不使用私有 PyPI 源或 vendoring**：所有依赖直接从 PyPI 拉取，无 `--index-url`、`--extra-index-url`、`pip.conf` 或 vendor 目录配置。