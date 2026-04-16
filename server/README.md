# txuw-xiaoai-server

`txuw-xiaoai-server` 是一个基于 `uv` 管理的 Python WebSocket 服务端，用于接收 `client-rust` 抛出的事件并将协议结构化为 Pydantic 模型。

## 快速开始

```bash
cd server
uv sync
uv run txuw-xiaoai-server
```

默认监听 `0.0.0.0:8000`，WebSocket 入口为 `/ws`，健康检查为 `/health`。

## 开发命令

```bash
uv run pytest -q
uv run uvicorn txuw_xiaoai_server.app:create_app --factory --host 0.0.0.0 --port 8000
```

## 环境变量

- `TXUW_XIAOAI_HOST`：默认 `0.0.0.0`
- `TXUW_XIAOAI_PORT`：默认 `8000`
- `TXUW_XIAOAI_LOG_LEVEL`：默认 `INFO`
