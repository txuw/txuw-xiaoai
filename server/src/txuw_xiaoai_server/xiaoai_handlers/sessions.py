from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .ports import StreamingTtsEngine


@dataclass(slots=True)
class DialogSessionState:
    """单轮对话中的 TTS 接管状态。"""

    connection_id: str
    dialog_id: str
    legacy_interrupted: bool = False
    legacy_interrupt_task: asyncio.Task[bool] | None = None
    tts_started: bool = False
    playback_started: bool = False
    sealed: bool = False
    query_text: str = ""
    final_asr_text: str = ""
    server_owned: bool = False
    agent_task: asyncio.Task[None] | None = None
    agent_full_text: str = ""
    engine: StreamingTtsEngine | None = None
    # 同一 dialog 的文本可能同时来自 Agent 增量、旧链路转发和服务端主动播报，
    # 统一串行化写入可以避免底层流式 TTS 会话出现乱序或在 complete 后继续写入。
    tts_write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DialogSessionStore:
    """按连接和对话 ID 管理 TTS 会话。"""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], DialogSessionState] = {}

    def get_or_create(self, connection_id: str, dialog_id: str) -> DialogSessionState:
        key = (connection_id, dialog_id)
        session = self._sessions.get(key)
        if session is None:
            session = DialogSessionState(connection_id=connection_id, dialog_id=dialog_id)
            self._sessions[key] = session
        return session

    def get(self, connection_id: str, dialog_id: str) -> DialogSessionState | None:
        return self._sessions.get((connection_id, dialog_id))

    def remove(self, connection_id: str, dialog_id: str) -> DialogSessionState | None:
        return self._sessions.pop((connection_id, dialog_id), None)

    def pop_connection(self, connection_id: str) -> list[DialogSessionState]:
        removed: list[DialogSessionState] = []
        for key in list(self._sessions):
            if key[0] == connection_id:
                removed.append(self._sessions.pop(key))
        return removed
