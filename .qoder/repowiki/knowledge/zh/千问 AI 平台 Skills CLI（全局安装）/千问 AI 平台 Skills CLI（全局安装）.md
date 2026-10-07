---
kind: external_dependency
name: 千问 AI 平台 Skills CLI（全局安装）
slug: qwen-ai-skills-cli
category: external_dependency
category_hints:
    - framework_behavior
    - client_constraint
scope:
    - '**'
---

通过 `npx skills add QianWen-AI/qianwen-ai --all -g` 与 `npx skills add QianWen-AI/qianwenai-deploy --all -g` 以用户级（global）方式安装，落地于 `~\.agents\skills\`，并复制进 Qoder。共 14 个 skill（qianwen-ai 包 11 个 + qianwenai-deploy 包 3 个），来源 GitHub 组织 `QianWen-AI`（已核实为阿里官方）。Windows 未开开发者模式时符号链接失败，改用文件复制，后续 `skills update` 不会自动同步。部分 skill（如 `qianwen-payment`、`qianwenai-deploy`、`qianwenai-operate`、`qianwen-find-skills`）具备真实花钱或操作云基础设施的能力，需配合 `QIANWEN_API_KEY` 使用。