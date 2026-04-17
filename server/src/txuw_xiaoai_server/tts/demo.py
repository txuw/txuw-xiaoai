# coding=utf-8
"""
CosyVoice 双向流式语音合成 Demo

基于阿里云 DashScope SDK 实现双向流式 TTS，使用 longyan_v3 音色。
适用于 LLM 流式输出场景：分片发送文本，实时接收并播放音频。

依赖安装：
    pip install dashscope pyaudio

运行前在 server/.env 中配置：
    DASHSCOPE_API_KEY=sk-xxx

运行：
    python -m txuw_xiaoai_server.tts.demo
"""

import time

import dashscope
import pyaudio
from dashscope.audio.tts_v2 import AudioFormat, ResultCallback, SpeechSynthesizer

from datetime import datetime

from txuw_xiaoai_server.config import settings


def get_timestamp() -> str:
    now = datetime.now()
    return now.strftime("[%Y-%m-%d %H:%M:%S.%f]")


# API Key 从 .env 配置读取
dashscope.api_key = settings.dashscope_api_key
dashscope.base_websocket_api_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

# 模型与音色配置
model = "cosyvoice-v3-flash"
voice = "longyan_v3"


class Callback(ResultCallback):
    """双向流式语音合成回调，实时播放音频数据"""

    _player: pyaudio.PyAudio | None = None
    _stream: pyaudio.Stream | None = None

    def on_open(self) -> None:
        print("连接建立：" + get_timestamp())
        self._player = pyaudio.PyAudio()
        self._stream = self._player.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=22050,
            output=True,
        )

    def on_complete(self) -> None:
        print("语音合成完成：" + get_timestamp())

    def on_error(self, message: str) -> None:
        print(f"语音合成异常：{message}")

    def on_close(self) -> None:
        print("连接关闭：" + get_timestamp())
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
        if self._player:
            self._player.terminate()

    def on_event(self, message: str) -> None:
        pass

    def on_data(self, data: bytes) -> None:
        print(f"{get_timestamp()} 音频数据长度：{len(data)}")
        if self._stream:
            self._stream.write(data)


# 模拟流式文本片段（类似 LLM 逐 token 输出）
test_text = [
    "流式文本语音合成SDK，",
    "可以将输入的文本",
    "合成为语音二进制数据，",
    "相比于非流式语音合成，",
    "流式合成的优势在于实时性",
    "更强。用户在输入文本的同时",
    "可以听到接近同步的语音输出，",
    "极大地提升了交互体验，",
    "减少了用户等待时间。",
    "适用于调用大规模",
    "语言模型（LLM），以",
    "流式输入文本的方式",
    "进行语音合成的场景。",
]


def main() -> None:
    if not dashscope.api_key:
        raise RuntimeError("请在 .env 中配置 DASHSCOPE_API_KEY")

    # --- 第一轮：测量初始化耗时 ---
    callback1 = Callback()

    t0 = time.perf_counter()
    synthesizer = SpeechSynthesizer(
        model=model,
        voice=voice,
        format=AudioFormat.PCM_22050HZ_MONO_16BIT,
        callback=callback1,
    )
    init_duration = (time.perf_counter() - t0) * 1000
    print(f"[Metric] SpeechSynthesizer 初始化耗时: {init_duration:.1f}ms")

    # 流式发送文本片段
    for text in test_text:
        synthesizer.streaming_call(text)
        time.sleep(0.1)

    # 结束合成（必须调用，否则结尾文本可能丢失）
    synthesizer.streaming_complete()

    print(
        f"[Metric] requestId: {synthesizer.get_last_request_id()}, "
        f"首包延迟: {synthesizer.get_first_package_delay()}ms"
    )

    # --- 第二轮：复用同一 synthesizer 实例测试 ---
    # 注意：根据阿里云 SDK 文档，每次 call/streaming_call 前需重新初始化实例，
    # 但双向流式场景下，同一个实例可在 streaming_complete 后继续使用。
    # 以下验证第二轮初始化是否更快（WebSocket 连接复用）。
    print("\n--- 第二轮测试：验证连接复用 ---")

    callback2 = Callback()

    t1 = time.perf_counter()
    synthesizer2 = SpeechSynthesizer(
        model=model,
        voice=voice,
        format=AudioFormat.PCM_22050HZ_MONO_16BIT,
        callback=callback2,
    )
    init_duration2 = (time.perf_counter() - t1) * 1000
    print(f"[Metric] 第二轮初始化耗时: {init_duration2:.1f}ms")

    for text in test_text:
        synthesizer2.streaming_call(text)
        time.sleep(0.1)

    synthesizer2.streaming_complete()

    print(
        f"[Metric] requestId: {synthesizer2.get_last_request_id()}, "
        f"首包延迟: {synthesizer2.get_first_package_delay()}ms"
    )
    print(
        f"[Metric] 初始化耗时对比: 第一轮 {init_duration:.1f}ms vs 第二轮 {init_duration2:.1f}ms"
    )


if __name__ == "__main__":
    main()
