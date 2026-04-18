from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

import dashscope
from dashscope.audio.tts_v2 import AudioFormat, ResultCallback, SpeechSynthesizer

from ..ports import AudioChunkSink, StreamingTtsEngine


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DashScopeStreamingTtsConfig:
    """DashScope 流式 TTS 配置。"""

    api_key: str
    model: str = "cosyvoice-v3-flash"
    voice: str = "longyan_v3"


class _DashScopeCallback(ResultCallback):
    """把 DashScope PCM 回调桥接到异步 sink。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, sink: AudioChunkSink) -> None:
        self._loop = loop
        self._sink = sink

    def on_open(self) -> None:
        logger.info("tts.stream.open")

    def on_complete(self) -> None:
        logger.info("tts.stream.complete")

    def on_error(self, message: str) -> None:
        logger.warning("tts.stream.error", extra={"summary": message})

    def on_close(self) -> None:
        logger.info("tts.stream.close")

    def on_event(self, message: str) -> None:
        logger.debug("tts.stream.event", extra={"summary": message})

    def on_data(self, data: bytes) -> None:
        self._loop.call_soon_threadsafe(asyncio.create_task, self._sink.write(data))


class DashScopeStreamingTtsEngine(StreamingTtsEngine):
    """基于 DashScope 的流式 TTS 实现。"""

    def __init__(self, config: DashScopeStreamingTtsConfig) -> None:
        self._config = config
        self._synthesizer: SpeechSynthesizer | None = None
        self._started = False
        self._completed = False

    async def start(self, session_id: str, sink: AudioChunkSink) -> None:
        if not self._config.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is not configured")

        dashscope.api_key = self._config.api_key
        dashscope.base_websocket_api_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

        loop = asyncio.get_running_loop()
        callback = _DashScopeCallback(loop, sink)
        self._synthesizer = SpeechSynthesizer(
            model=self._config.model,
            voice=self._config.voice,
            format=AudioFormat.PCM_22050HZ_MONO_16BIT,
            callback=callback,
        )
        self._started = True
        self._completed = False
        logger.info(
            "tts.session.started",
            extra={"sessionId": session_id, "summary": "dashscope streaming tts started"},
        )

    async def push_text(self, text: str) -> None:
        if not self._started or self._synthesizer is None:
            raise RuntimeError("TTS engine is not started")
        if self._completed:
            raise RuntimeError("TTS engine already completed")
        self._synthesizer.streaming_call(text)

    async def complete(self) -> None:
        if not self._started or self._synthesizer is None or self._completed:
            return
        self._synthesizer.streaming_complete()
        self._completed = True

    async def close(self) -> None:
        if self._synthesizer is not None and not self._completed:
            with contextlib.suppress(Exception):
                self._synthesizer.streaming_complete()
            self._completed = True
        self._synthesizer = None
        self._started = False
