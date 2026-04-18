# txuw-xiaoai-server

`txuw-xiaoai-server` 是面向 `client-rust` 的 Python WebSocket 服务端，负责：

- 接收并解析音箱侧事件
- 结构化记录协议消息
- 在启用时拦截旧小爱播报文本，并切换到新的实时 TTS 播放链路

## 快速开始

```bash
cd server
uv sync
uv run txuw-xiaoai-server
```

默认监听：

- `0.0.0.0:8000`
- WebSocket 入口：`/ws`
- 健康检查：`/healthz`

## 开发命令

```bash
uv run pytest -q
uv run uvicorn txuw_xiaoai_server.app:create_app --factory --host 0.0.0.0 --port 8000
```

## 实时 TTS 替换开关

默认不会启用旧小爱播报拦截。

如需启用实时 TTS 替换，请在 `.env` 中至少配置：

```env
DASHSCOPE_API_KEY=sk-xxx
TTS_INTERCEPT_ENABLED=true
```

可选配置：

```env
DASHSCOPE_TTS_MODEL=cosyvoice-v3-flash
DASHSCOPE_TTS_VOICE=longyan_v3
TTS_SAMPLE_RATE=22050
TTS_CHANNELS=1
TTS_BITS_PER_SAMPLE=16
TTS_PERIOD_SIZE=330
TTS_BUFFER_SIZE=1320
LEGACY_INTERRUPT_ENABLED=true
LEGACY_INTERRUPT_COMMAND=/etc/init.d/mico_aivs_lab restart >/dev/null 2>&1
```

## LLM 问答流开关

如需启用 Server 侧 LiteLLM 问答流，请在 `.env` 中补充：

```env
LLM_PROXY_ENABLED=true
LLM_API_KEY=sk-xxx
LLM_BASE_URL=http://your-litellm-host:4000/v1
LLM_MODEL=gpt-4o-mini
LLM_TIMEOUT_SECONDS=60
LLM_SYSTEM_PROMPT=你是一个适合直接口播的中文语音助手。请用自然、简短、口语化的中文回答。只输出纯文本，不要使用 Markdown、列表编号、代码块或表情。
```

启用后，服务端会优先在收到 `Template.Query` 或最终 ASR 文本时直接调用 LiteLLM 的 OpenAI 兼容接口，并把流式文本直接送入 DashScope `SpeechSynthesizer`。

## 手动自测

原来的 `demo.py` 已迁移到 `tests/manual`。

在 `server` 目录下执行：

```bash
uv run python tests/manual/streaming_tts_selftest.py
```

这个脚本会：

- 真实调用 DashScope 流式 TTS
- 使用本地 `pyaudio` 播放合成结果
- 打印音频分片长度，便于观察实时链路
