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
