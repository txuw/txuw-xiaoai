# txuw-xiaoai-server

`txuw-xiaoai-server` 是面向 `client-rust` 的 Python WebSocket 服务端，负责：

- 接收并解析音箱侧事件
- 记录结构化协议日志
- 在启用时拦截旧小爱播报文本，并切换到新的实时 TTS 播放链路
- 在启用时为 Agent 增加 mem0 长期记忆召回与异步写回

## 快速开始

```bash
cd server
uv sync
uv run txuw-xiaoai-server
```

默认监听：

- `0.0.0.0:8000`
- WebSocket 入口：`/ws`
- 健康检查：`/healthz`（兼容 `/health`）

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

## 地区工具（高德 IP 定位）

如需启用 Agent 的地区获取工具，请在 `.env` 中补充：

```env
AMAP_WEB_SERVICE_KEY=your-amap-web-service-key
```

可选配置：

```env
REGION_TOOL_ENABLED=true
REGION_TOOL_TIMEOUT_SECONDS=1.0
REGION_TOOL_CACHE_TTL_SECONDS=300
```

说明：

- 对模型暴露的工具名固定为 `region_get_current_region`
- 不传 `ip` 时，默认使用服务端请求来源 IP 做高德 IP 定位
- 显式传参时仅支持国内 IPv4，不支持 IPv6
- 工具返回高德原生风格结果：`province`、`city`、`adcode`、`rectangle`

## Memory 长期记忆

如需启用 mem0 + Milvus 长期记忆，请在 `.env` 中补充：

```env
MEMORY_ENABLED=true
MEMORY_USER_ID=txuw
MEMORY_LLM_MODEL=gpt-5-nano-2025-08-07
MEMORY_EMBEDDING_MODEL=text-embedding-3-small
MEMORY_MILVUS_URL=http://your-milvus-host:19530
MEMORY_MILVUS_TOKEN=
MEMORY_MILVUS_DB_NAME=default
MEMORY_MILVUS_COLLECTION_NAME=txuw_xiaoai_mem0
MEMORY_RECALL_MAX_RESULTS=5
MEMORY_RECALL_MIN_SCORE=0.3
MEMORY_COMMIT_QUEUE_MAXSIZE=256
MEMORY_COMMIT_WORKER_COUNT=2
```

说明：

- mem0 内部 LLM 默认使用 `gpt-5-nano-2025-08-07`
- 向量模型默认使用 `text-embedding-3-small`
- mem0 直接复用 `LLM_API_KEY` 和 `LLM_BASE_URL`
- 每次执行 Agent 前会先做一次长期记忆召回，并把结果注入本轮 prompt
- 只有 Agent 正常完成且拿到非空输出时，才会把 `user -> assistant` 这一对对话异步写回 mem0

## 手动自测

原来的 `demo.py` 已迁移到 `tests/manual`。

首次运行前，如果需要本地扬声器回放，请先安装额外音频依赖：

```bash
uv sync --group audio
```

如果在 Linux 上安装 `pyaudio` 失败，请先安装 PortAudio 开发包，例如：

```bash
sudo apt-get update
sudo apt-get install -y portaudio19-dev
```

在 `server` 目录下执行：

```bash
uv run python tests/manual/streaming_tts_selftest.py
```

这个脚本会：

- 真实调用 DashScope 流式 TTS
- 使用本地 `pyaudio` 播放合成结果
- 打印音频分片长度，便于观察实时链路
