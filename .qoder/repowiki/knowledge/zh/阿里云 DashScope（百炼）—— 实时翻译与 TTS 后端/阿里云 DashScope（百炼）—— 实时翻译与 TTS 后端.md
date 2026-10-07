---
kind: external_dependency
name: 阿里云 DashScope（百炼）—— 实时翻译与 TTS 后端
slug: aliyun-dashscope
category: external_dependency
category_hints:
    - vendor_identity
    - auth_protocol
    - client_constraint
scope:
    - '**'
---

本项目调用阿里云 DashScope（百炼）提供三类能力：
- OpenAI 兼容 chat：`https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions`，用于打字翻译（`textin.ENDPOINT`）和 Omni 音色试听（`tts.OMNI_ENDPOINT`）。
- DashScope 原生 multimodal-generation：`https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation`，用于 `qwen3-tts-flash` 的语音合成（`tts.ENDPOINT`）。
- WebSocket 实时会话：`wss://dashscope.aliyuncs.com/api-ws/v1/realtime`，由 `session.base_url` 默认值驱动（可被 `config.yaml` 覆盖），用于 `qwen3.8-livetranslate-flash-realtime` 说话译音。

鉴权通过环境变量 `DASHSCOPE_API_KEY` 注入；北京通用域仍可用，业务空间专属域为 `{WorkspaceId}.cn-beijing.maas.aliyuncs.com`，海外走 `dashscope-intl.aliyuncs.com` / `dashscope-us.aliyuncs.com`。Key 与地域强绑定，跨域会返回 `invalid_api_key`。项目当前全部端点直连官方 `dashscope.aliyuncs.com`，未使用任何第三方中转站。