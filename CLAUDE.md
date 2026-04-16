# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Open-XiaoAI 是一个解锁小爱音箱潜力的开源项目，通过刷机补丁和自定义 Client/Server 架构，将小爱音箱接入各类 AI 服务。项目**已停止维护**。

支持机型：小爱音箱 Pro (LX06) 和 Xiaomi 智能音箱 Pro (OH2P)。

## 架构

Client-Server 架构，通过 WebSocket 双向通信：

- **Client 端** (`packages/client-rust/`)：Rust 编写，运行在小爱音箱上，负责音频采集/播放、事件监控（唤醒词/指令/播放状态）、响应 RPC 调用。交叉编译为 ARMv7 (`armv7-unknown-linux-gnueabihf`)
- **Server 端**（各 `examples/` 实现）：运行在电脑/NAS 上，实现具体 AI 业务逻辑（如 MiGPT、小智 AI、Gemini 等）
- **通信协议** (`services/connect/data.rs`)：`AppMessage` 枚举定义四种消息类型：`Request`/`Response`（RPC）、`Event`（事件推送）、`Stream`（音频流）

### 通信协议要点

- Client 端注册 RPC 命令：`get_version`, `run_shell`, `start_play`, `stop_play`, `start_recording`, `stop_recording`
- Server 端通过 `Stream(tag="play")` 发送音频数据到 Client 播放
- Client 端通过 `Event` 上报唤醒词(kws)、语音识别(instruction)、播放状态(playing)等事件

## 常用命令

### Rust Client 编译

```bash
cd packages/client-rust
cross build --release --target armv7-unknown-linux-gnueabihf
# 产物: target/armv7-unknown-linux-gnueabihf/release/client
```

### 固件补丁工具 (TypeScript)

```bash
cd packages/client-patch
pnpm install
pnpm ota       # 下载固件
pnpm extract   # 解压固件
pnpm patch     # 应用补丁
pnpm squashfs  # 重新打包
pnpm build     # 构建完整固件
```

### MiGPT 示例 (Rust + TypeScript)

```bash
cd examples/migpt
pnpm install
pnpm dev       # 构建 Rust server binding 并启动
```

### Python 示例（xiaozhi/gemini）

```bash
cd examples/xiaozhi  # 或 examples/gemini
uv sync               # 安装依赖
python main.py        # 运行
```

## 关键目录

| 路径 | 说明 |
|------|------|
| `packages/client-rust/src/services/` | Client 核心服务：audio（播放/录音）、connect（通信）、monitor（唤醒词/指令/播放监控） |
| `packages/client-rust/src/bin/` | 入口：`client.rs`（主程序）、`monitor.rs`（监控服务） |
| `packages/client-patch/src/` | 固件补丁脚本（OTA 下载、解压、补丁、打包） |
| `packages/runtime/` | Docker 交叉编译运行环境（Ubuntu 20.04 + ARMv7 工具链） |
| `examples/migpt/` | MiGPT 集成，通过 Rust binding 共享通信模块 |
| `examples/xiaozhi/` | 小智 AI 集成（Python） |
| `examples/gemini/` | Gemini Live API 集成（Python） |
| `examples/kws/` | 自定义唤醒词 |
| `examples/stereo/` | 多音箱组立体声 |
| `docs/` | 刷机教程等文档 |

## 技术栈

- **Rust**：Client 端，tokio 异步运行时，tokio-tungstenite WebSocket，serde 序列化
- **TypeScript/Node.js**：Client-patch 工具和 MiGPT 示例，使用 pnpm 管理
- **Python**：xiaozhi/gemini 示例，使用 uv 管理依赖
- **Docker**：交叉编译环境和固件构建
- **CI**：GitHub Actions，监听 `packages/client-rust/` 变更自动编译 ARMv7 产物

## 注意事项

- Client 端 Release 编译开启 LTO、size 优化、panic abort、strip，针对嵌入式设备优化
- MiGPT 示例通过 `cargo-cp-artifact` + Neon 实现 Rust/Node.js 通信模块共享
- 通信模块是各 Server 端示例的共享基础设施，理解 `packages/client-rust/src/services/connect/` 是开发新 Server 端的关键
