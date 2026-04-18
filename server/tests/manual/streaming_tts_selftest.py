from __future__ import annotations

import asyncio
from datetime import datetime

import pyaudio

from txuw_xiaoai_server.config import settings
from txuw_xiaoai_server.xiaoai_handlers.services.dashscope_streaming_tts import (
    DashScopeStreamingTtsConfig,
    DashScopeStreamingTtsEngine,
)


def timestamp() -> str:
    """返回便于观察的时间戳。"""

    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S.%f]")


class LocalPyAudioSink:
    """把实时 TTS 输出写到本地扬声器，用于手动自测。"""

    def __init__(self) -> None:
        self._player = pyaudio.PyAudio()
        self._stream = self._player.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=settings.tts_sample_rate,
            output=True,
        )

    async def write(self, data: bytes) -> None:
        print(f"{timestamp()} 音频分片长度: {len(data)}")
        self._stream.write(data)

    async def close(self) -> None:
        self._stream.stop_stream()
        self._stream.close()
        self._player.terminate()


async def main() -> None:
    """真实调用 DashScope 做流式 TTS 手动自测。"""

    if not settings.dashscope_api_key:
        raise RuntimeError("请先在 server/.env 中配置 DASHSCOPE_API_KEY")

    sink = LocalPyAudioSink()
    engine = DashScopeStreamingTtsEngine(
        DashScopeStreamingTtsConfig(
            api_key=settings.dashscope_api_key,
            model=settings.dashscope_tts_model,
            voice=settings.dashscope_tts_voice,
        )
    )
    try:
        await engine.start("manual-selftest", sink)
        for text in [
            "流式文本语音合成自测开始。",
            "这条链路已经迁移到 tests 目录，",
            "方便你直接在测试目录下做真实自测。",
            "如果你能连续听到这些语音，",
            "说明新的实时合成链路工作正常。",
        ]:
            await engine.push_text(text)
            await asyncio.sleep(0.1)
        await engine.complete()
        await asyncio.sleep(1.0)
    finally:
        await engine.close()
        await sink.close()


if __name__ == "__main__":
    asyncio.run(main())
