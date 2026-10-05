<!-- 提交前请读 CONTRIBUTING.md，尤其「四、提 PR 之前」。标了 * 的栏目必须填，不能留空。 -->

## 这个 PR 做了什么

<!-- 一句话说清动机与改动。关联的 issue：#___ -->

## 改动类型

- [ ] `feat` 新功能
- [ ] `fix` 修 bug
- [ ] `docs` 文档 / README / GUIDE
- [ ] `chore` / `ci` / `test` / `perf` / 其它

## 影响平台

- [ ] Windows
- [ ] Linux
- [ ] 两端都影响（请在下方分别说明差异）

> 只涉及 Linux 独占实现（PipeWire 采集、自建 OpenXR 手腕屏、AppImage、GUIDE.linux.md）时请注明。

## 验证证据 *

> 必填。贴出你**实际执行**的命令与**原始输出**，不要只写「测试通过」。没验到的部分如实写「未验证」。

```
$ <你跑的命令>
<原始输出>
```

## 真机结论 *

> 必填。涉及硬件 / 平台行为时（音频设备、虚拟声卡、手腕屏、UI 布局……）必须写清：
> **哪台机器、什么硬件、实测到什么现象**。本机测不了的，写明「未在本机复现 + 依据」。

## 文档与词表是否同步

- [ ] 不涉及
- [ ] 已更新相关文档（README / GUIDE / GUIDE.linux / config.example.yaml）
- [ ] 改到了 `t()` 文案 → 已同步全部 `vlt/locales/*.py`，且 `tests/test_i18n.py` 全绿

## 是否触及 `docs/平台约束记录.md` 的硬约束

- [ ] 否
- [ ] 是 → 已附上**我自己的实测依据**（复现步骤 + 结果），见上方「验证证据」

## 本地测试

- [ ] 已 `rebase` 到最新 `main`
- [ ] 全量离线测试全绿（`tests/test_engine.py` 除外）
- [ ] `scripts/check_doc_links.py` 通过（改了文档时）
- [ ] `scripts/check_no_secrets.py --once` 通过（未提交任何 key）

## 备注

<!-- reviewer 需要知道的其他事；UI 改动建议附图。 -->
