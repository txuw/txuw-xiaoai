# WebSocket 交互协议说明

本文档基于当前仓库中的真实实现整理，主要来源：

- `packages/client-rust/src/services/connect/data.rs`
- `packages/client-rust/src/bin/client.rs`
- `server/src/txuw_xiaoai_server/protocol/models.py`
- `server/tests/datasets/*.json`

目标是快速回答 3 个问题：

1. 当前到底有哪些交互消息类型
2. 每类消息有哪些关键字段
3. 常见业务链路里会出现哪些事件序列

## 1. 顶层消息类型

WebSocket 入站消息分为 4 类。

### 1.1 Text: `Request`

```json
{
  "Request": {
    "id": "string",
    "command": "string",
    "payload": {}
  }
}
```

字段说明：

- `id`: 请求唯一 ID
- `command`: 请求命令名
- `payload`: 命令参数，可为空

当前服务端重点是接收 Client 主动上报事件，因此这类消息主要用于协议完整性保留。

### 1.2 Text: `Response`

```json
{
  "Response": {
    "id": "string",
    "code": 0,
    "msg": "success",
    "data": {}
  }
}
```

字段说明：

- `id`: 对应请求 ID
- `code`: 返回码，可为空
- `msg`: 返回消息，可为空
- `data`: 返回数据，可为空

### 1.3 Text: `Event`

```json
{
  "Event": {
    "id": "string",
    "event": "instruction | playing | kws | <unknown>",
    "data": {}
  }
}
```

字段说明：

- `id`: 事件唯一 ID
- `event`: 事件类型
- `data`: 事件负载，具体结构由 `event` 决定

### 1.4 Binary: `Stream`

二进制帧内部仍然是 JSON 序列化后的 `Stream`。

```json
{
  "id": "string",
  "tag": "record | play | <unknown>",
  "bytes": [1, 2, 3],
  "data": {}
}
```

字段说明：

- `id`: 流帧唯一 ID
- `tag`: 流类型
- `bytes`: 实际二进制内容
- `data`: 附加元数据，可为空

当前 Client 主动上报到服务端的已知流类型是 `record`。

## 2. 已知事件类型

当前 `client-rust` 主动发送 3 类事件：

- `instruction`
- `playing`
- `kws`

### 2.1 `instruction`

这是最复杂的一类，`data` 有两种形态。

#### 2.1.1 NewFile

```json
{
  "Event": {
    "id": "xxx",
    "event": "instruction",
    "data": "NewFile"
  }
}
```

含义：

- 代表底层指令日志文件切换/重建
- 常作为一轮新对话的起点

#### 2.1.2 NewLine

```json
{
  "Event": {
    "id": "xxx",
    "event": "instruction",
    "data": {
      "NewLine": "{\"header\":...,\"payload\":...}"
    }
  }
}
```

`NewLine` 内部是一个字符串化 JSON，结构如下：

```json
{
  "header": {
    "dialog_id": "string",
    "id": "string",
    "name": "string",
    "namespace": "string"
  },
  "payload": {}
}
```

`header` 字段说明：

- `dialog_id`: 一轮对话链路 ID，同一轮交互通常保持一致
- `id`: 当前指令消息 ID
- `name`: 指令类型名，决定 `payload` 的结构
- `namespace`: 指令命名空间

#### 2.1.3 已知 `instruction.header.name`

当前服务端已明确识别的类型如下。

##### `RecognizeResult`

语音识别增量/最终结果。

```json
{
  "payload": {
    "is_final": false,
    "is_vad_begin": true,
    "results": [
      {
        "confidence": 0.0,
        "text": "今天的天气",
        "asr_binary_offset": 4400,
        "begin_offset": 2540,
        "end_offset": 4400,
        "is_nlp_request": true,
        "is_stop": true,
        "origin_text": "今天的天气"
      }
    ]
  }
}
```

字段说明：

- `is_final`: 是否最终识别结果
- `is_vad_begin`: 是否语音段开始
- `results`: 识别结果数组
- `results[].text`: 当前最重要的识别文本
- `results[].is_nlp_request`: 是否可进入 NLP
- `results[].is_stop`: 是否触发结束
- `results[].origin_text`: 原始文本

##### `StopCapture`

停止录音/停止采集。

```json
{
  "payload": {
    "stop_time": 5024
  }
}
```

字段说明：

- `stop_time`: 停止时间点

##### `Speak`

完整播报文本。

```json
{
  "payload": {
    "text": "正在为你关闭电视。",
    "emotion": {
      "category": "string",
      "level": "string"
    }
  }
}
```

字段说明：

- `text`: 播报文本
- `emotion`: 情感信息，可为空

##### `SpeakStream`

流式播报文本分片。

结构与 `Speak` 相同，常用于分段输出 TTS 文本。

##### `Play`

播放音频资源。

```json
{
  "payload": {
    "audio_items": [],
    "audio_type": "string",
    "loadmore_token": "string",
    "needs_loadmore": false,
    "origin_id": "string",
    "play_behavior": "string"
  }
}
```

字段说明：

- `audio_items`: 音频资源列表
- `audio_type`: 音频类型
- `loadmore_token`: 分页/续取 token
- `needs_loadmore`: 是否需要继续加载
- `origin_id`: 原始来源 ID
- `play_behavior`: 播放行为

##### `SetProperty`

设置属性。

```json
{
  "payload": {
    "name": "string",
    "value": "string"
  }
}
```

##### `InstructionControl`

执行控制指令。

```json
{
  "payload": {
    "behavior": "INSERT_FRONT"
  }
}
```

字段说明：

- `behavior`: 控制行为，例如 `INSERT_FRONT`

##### 空 Payload 类型

这类 `payload` 通常是 `{}`：

- `StartStream`
- `FinishStream`
- `FinishSpeakStream`
- `Finish`

#### 2.1.4 当前已观察到但仍按通用类型处理

这些类型已经在事实日志里出现，但目前服务端按 `GenericPayload` 处理：

- `Query`
- `StartAnswer`
- `FinishAnswer`

它们仍会被识别出 `instructionName`，只是没有专门的强类型 payload 模型。

### 2.2 `playing`

表示播放状态变化。

```json
{
  "Event": {
    "id": "xxx",
    "event": "playing",
    "data": "Playing"
  }
}
```

已知状态值：

- `Playing`
- `Paused`
- `Idle`

### 2.3 `kws`

表示唤醒词相关事件。

#### `Started`

```json
{
  "Event": {
    "id": "xxx",
    "event": "kws",
    "data": "Started"
  }
}
```

#### `Keyword`

```json
{
  "Event": {
    "id": "xxx",
    "event": "kws",
    "data": {
      "Keyword": "小爱同学"
    }
  }
}
```

字段说明：

- `Keyword`: 实际唤醒词文本

## 3. 当前服务端重点识别的交互链路

基于 `server/tests/datasets/` 中的事实数据，当前沉淀了 3 条通用链路。

### 3.1 天气询问

典型事件顺序：

1. `instruction: NewFile`
2. 多个 `instruction: RecognizeResult`
3. `instruction: StopCapture`
4. `instruction: Query`
5. `instruction: SpeakStream`
6. `instruction: FinishStream`
7. `instruction: FinishSpeakStream`
8. `instruction: Finish`
9. `playing: Idle`

关注重点：

- ASR 增量是否完整
- `Query` 是否出现
- 播报链路是否正确闭环

### 3.2 开关电视

典型事件顺序：

1. `instruction: NewFile`
2. 多个 `instruction: RecognizeResult`
3. `instruction: StopCapture`
4. `instruction: StartAnswer`
5. `instruction: InstructionControl`
6. `instruction: Speak`
7. `playing: Playing`
8. `instruction: FinishAnswer`
9. `instruction: FinishSpeakStream`
10. `instruction: Finish`
11. `playing: Idle`

关注重点：

- `InstructionControl.behavior`
- 是否出现明确的 `Speak`
- `playing` 是否经历 `Playing -> Idle`

### 3.3 唤醒但无后续语音

典型事件顺序：

1. `instruction: NewFile`
2. `instruction: RecognizeResult` 空文本
3. `instruction: StopCapture` 但缺字段，可能 `degraded`
4. `instruction: RecognizeResult` 缺字段，可能 `degraded`
5. `instruction: Finish`

关注重点：

- 是否产生 `degraded` 而不是崩溃
- 是否能平稳结束当前对话

## 4. 日志与协议字段如何对应

当前服务端日志分两层。

### 4.1 原始事实日志

日志名：

- `socket.ingress.raw`

作用：

- 原样记录最外层收到的文本帧
- 用于回放、构建测试数据集、还原现场

常见字段：

- `connectionId`
- `frameType`
- `rawPayload`
- `summary=raw text frame`

### 4.2 解析后的结构化日志

日志名：

- `socket.message.request`
- `socket.message.response`
- `socket.message.event`
- `socket.message.stream`

作用：

- 展示协议转换后的核心事实
- 便于快速定位业务语义

常见字段：

- `messageType`
- `event`
- `instructionName`
- `payloadKind`
- `status`
- `summary`
- `payloadError`

## 5. 排障时推荐优先看什么

### 场景 1：确认收到什么原始消息

先看：

- `socket.ingress.raw.rawPayload`

### 场景 2：确认服务端识别成了什么类型

再看：

- `socket.message.event.event`
- `instructionName`
- `payloadKind`
- `summary`

### 场景 3：确认是否发生降级解析

重点看：

- `status=degraded`
- `payloadError`
- `errorType`

## 6. 当前通用数据集位置

可直接用于测试回放的数据集文件：

- `server/tests/datasets/weather_query.json`
- `server/tests/datasets/tv_control.json`
- `server/tests/datasets/wake_only.json`

如果后续新增交互场景，建议优先追加新的事实数据集，再补对应模型与测试，而不是直接手写新的模拟 JSON。
