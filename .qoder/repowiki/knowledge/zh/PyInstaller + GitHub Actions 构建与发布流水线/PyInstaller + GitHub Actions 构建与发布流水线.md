---
kind: build_system
name: PyInstaller + GitHub Actions 构建与发布流水线
category: build_system
scope:
    - '**'
source_files:
    - scripts/build_exe.py
    - .github/workflows/ci.yml
    - .github/workflows/release.yml
    - .github/workflows/appimage.yml
    - build_exe.bat
    - setup.bat
    - requirements.txt
    - vlt/__init__.py
---

## 1. 总体方案

本项目使用 **PyInstaller** 把 Python 桌面应用打包为 Windows 单文件 exe（`dist/VRChatLiveTranslate.exe`，无控制台窗口）和 Linux AppImage（`dist/VRChatLiveTranslate-x86_64.AppImage`），并通过 **GitHub Actions** 在 push main / PR 时执行 CI，在打 `v*` 标签时触发 Release。

- 构建入口：`scripts/build_exe.py`（Windows）、`scripts/build_appimage.sh`（Linux，由 `.github/workflows/appimage.yml` 调用）
- 本地快捷脚本：`build_exe.bat`、`setup.bat`、`run_gui.bat`、`run_chatbox.bat`、`run_overlay.bat`、`run_selfcheck.bat`
- CI：`.github/workflows/ci.yml`（tests / package / linux-tests / appimage）
- Release：`.github/workflows/release.yml`（tag v* → 上传 exe + AppImage + SHA256SUMS.txt）
- 可复用 workflow：`.github/workflows/appimage.yml`（被 ci.yml 与 release.yml 共同 `uses:`）

依赖分三层：公共依赖 `requirements.txt`，平台独占依赖分别放在 `requirements-windows.txt` 与 `requirements-linux.txt`，开发依赖 `requirements-dev.txt`。Python 版本固定为 **3.11**（CI 与 README 一致；注释明确「3.12 未测」）。

## 2. 关键文件与职责

| 文件 | 职责 |
|---|---|
| `scripts/build_exe.py` | PyInstaller 参数组装、隐藏导入声明、平台模块排除、产物自检（`--self-test`） |
| `.github/workflows/ci.yml` | 多 job CI：Windows 语法/静态检查/凭据扫描/离线测试/打包；Linux 离线测试 + AppImage 构建 |
| `.github/workflows/release.yml` | tag 校验 `__version__`、双端产物组装、SHA256SUMS 生成、softprops/action-gh-release 发版 |
| `.github/workflows/appimage.yml` | 可复用的 Linux AppImage 构建 + 独立验收（xvfb 下字体检查、平台隔离断言） |
| `scripts/check_no_secrets.py` | 仓库凭据扫描门禁 |
| `scripts/check_platform_purity.py` | 断言 Windows 产物不含 Linux 独占实现（PipeWire/OpenXR） |
| `scripts/verify_release.py` | Release 产物复核（配合 release.yml） |
| `setup.bat` | 创建 `.venv` + 安装 `requirements-windows.txt` |
| `build_exe.bat` | 调用 `scripts/build_exe.py` 的 ASCII-only 包装器 |
| `requirements.txt` | 公共依赖（注释明确平台独占依赖不得写在这里） |
| `vlt/__init__.py` | 单一版本源 `__version__ = "0.9.0"` |

## 3. 架构与约定

### 3.1 平台隔离策略

`scripts/build_exe.py` 通过三组列表显式控制打包内容：
- `HIDDEN`：按需导入的库（静态分析看不到），必须 `--hidden-import` 声明
- `COLLECT_ALL`：带二进制/数据文件的库（pyaudiowpatch、sounddevice、openvr、pythonosc），用 `--collect-all` 连数据一起收
- `EXCLUDE_WIN`：Windows 产物中**必须排除**的 Linux 独占模块（`vlt.platform.linux`、`vlt.platform.wayland`、`vlt.platform.x11`、`vlt.output.openxr_overlay`、`xr`）

CI 通过 `scripts/check_platform_purity.py dist/VRChatLiveTranslate.exe --platform windows` 对解包后的字节码做断言，而非搜索原始 exe 字节（PYZ 是 zlib 压缩的）。Linux AppImage 侧反向排除 openvr 后端，由 `scripts/verify_appimage.py` 验证。

### 3.2 构建产物与运行期路径

- 输出目录：`dist/`，临时目录：`build/`，每次构建前清空
- 图标：`assets/app.ico`（多尺寸 16/24/32/48/64/128/256）
- 资源打包：`config.example.yaml`、`testdata/`、`assets/` 通过 `--add-data` 打入 PYZ
- 运行期配置目录：`%APPDATA%\vrchat-livetranslate`（exe 旁有 `portable.txt` 时回退到 exe 所在目录，即绿色版）
- 日志：`logs/gui_YYYYMMDD_HHMMSS.log`，自检通过判定依据是退出码 0 且日志含 `GUI_SELFTEST_OK`

### 3.3 版本管理

版本只维护在 `vlt/__init__.py` 的 `__version__`。Release 流程在打 tag 时强制校验 `refs/tags/vX.Y.Z` 与代码中的 `__version__` 一致，不一致直接失败（PowerShell 正则匹配后比较）。

### 3.4 依赖门禁

CI 中三次显式断言 Windows 独占依赖可导入：
```bash
python -c "import openvr, pyaudiowpatch; print('Windows 独占依赖 OK')"
```
原因注释说明：缺了不会报错，只会静默降级（SteamVR 手腕屏 unavailable、VRChat 音频下拉为空），所以做成红灯门禁。

### 3.5 测试与覆盖率

- 使用 pytest 风格的 `tests/test_*.py`，由 CI 逐个 `coverage run -a --source=vlt` 累积覆盖率
- `tests/test_engine.py` 因需要真实 API key 被跳过（仅本机实测）
- Linux 上 Tk 相关测试走 `xvfb-run -a`，并需安装 `fonts-noto-cjk`、`fonts-thai-tlwg`、`tk`、`libportaudio2`、`libegl1`、`libwayland-client0` 等系统依赖
- 覆盖率报告非阻断（`continue-on-error: true`），只出摘要

### 3.6 安全门禁

- `scripts/check_no_secrets.py --once` 在 CI 每个 job 运行，防止 key 进历史
- `PYTHONUTF8=1` 全局开启 UTF-8 模式，避免 Windows runner 默认 cp1252 导致 emoji/中文 print 崩溃

### 3.7 AppImage 构建缓存

AppImage 构建使用 `actions/cache@v4`，key 包含 `uv` 自带 Tcl/Tk 版本号与 `scripts/build_appimage.sh` 的哈希，避免 uv 升级 Tcl/Tk 后命中旧缓存导致 ABI 不匹配。

## 4. 约定与约束

- **Python 版本**：固定 3.11（CI setup-python 指定，setup.bat 优先 `py -3.11`，README 注明 3.12 未测）
- **PyInstaller 版本**：`>=6.6`（CI 与 build_exe.py 均要求）
- **公共依赖不得出现平台独占项**：`requirements.txt` 顶部注释明确「平台独占的依赖不要写在这里（否则另一个平台会解析失败）」
- **Windows 产物不得包含 Linux 独占实现**：由 `scripts/build_exe.py` 的 `EXCLUDE_WIN` 与 CI 的 `check_platform_purity.py` 双重保证
- **Release 必须同时产出 exe 与 AppImage**：release.yml 中 `needs: appimage` 故意设置，宁可整体红也不静默发半份
- **Tag 与代码版本必须一致**：release.yml 在打 tag 分支比对 `refs/tags/vX.Y.Z` 与 `vlt/__init__.py` 的 `__version__`，不一致则 exit 1
- **SHA256SUMS.txt 必须随 exe 附件发布**：老客户端（≤ v0.2.0）更新检查逻辑写死读取该文件，缺一个就永远查不到新版本
- **本地 bat 脚本必须 ASCII + CRLF**：`build_exe.bat` 头部注释明确要求，避免 GBK 控制台误解码
- **所有测试必须在无 API key / 无麦克风 / 无 VRChat / 无 SteamVR 的机器上跑通**：CI 就是这台机器的契约，需要真硬件的 `--self-test` 不在 CI 跑（仅在 release.yml 中显式跳过）