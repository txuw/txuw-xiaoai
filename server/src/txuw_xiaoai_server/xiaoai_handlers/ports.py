from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class AudioChunkSink(Protocol):
    """音频分片输出端口。"""

    async def write(self, data: bytes) -> None:
        """输出一段可播放音频数据。"""


class PlaybackControlPort(Protocol):
    """远端播放控制端口。"""

    async def ensure_started(self) -> None:
        """确保远端播放链路已启动。"""

    async def stop(self) -> None:
        """停止远端播放链路。"""

    async def run_shell(self, script: str) -> str:
        """在远端执行脚本并返回原始文本响应。"""


class StreamingTtsEngine(Protocol):
    """流式 TTS 引擎接口。"""

    async def start(self, session_id: str, sink: AudioChunkSink) -> None:
        """启动一次流式合成会话。"""

    async def push_text(self, text: str) -> None:
        """输入一段文本分片。"""

    async def complete(self) -> None:
        """声明文本输入完成。"""

    async def close(self) -> None:
        """关闭并释放当前会话资源。"""


class LegacyAudioInterrupter(Protocol):
    """旧小爱播报中断策略接口。"""

    async def interrupt(self, connection_id: str, dialog_id: str) -> bool:
        """中断旧小爱播报链路。"""


@dataclass(slots=True)
class ConnectionContext:
    """单连接业务上下文。"""

    connection_id: str
    playback_port: PlaybackControlPort
    audio_sink: AudioChunkSink
