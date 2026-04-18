from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .handlers.dialog_lifecycle import DialogLifecycleHandler
from .handlers.speech_synthesizer import SpeechSynthesizerHandler
from .ports import ConnectionContext, LegacyAudioInterrupter, StreamingTtsEngine
from .services.tts_replacement_coordinator import TtsReplacementCoordinator
from .sessions import DialogSessionStore


class XiaoAiApplication:
    """小爱播报拦截与 TTS 替换的业务入口。"""

    def __init__(
        self,
        *,
        engine_factory: Callable[[], StreamingTtsEngine],
        interrupter_factory: Callable[[ConnectionContext], LegacyAudioInterrupter],
        enabled: bool,
    ) -> None:
        session_store = DialogSessionStore()
        coordinator = TtsReplacementCoordinator(
            session_store,
            engine_factory=engine_factory,
            enabled=enabled,
        )
        self._coordinator = coordinator
        self._interrupter_factory = interrupter_factory
        self._speech_synthesizer_handlers: dict[str, SpeechSynthesizerHandler] = {}
        self._dialog_lifecycle_handlers: dict[str, DialogLifecycleHandler] = {}

    async def handle_inbound(self, message: Any, context: ConnectionContext) -> None:
        """在应用层直接完成分发，避免额外中间层增加理解成本。"""

        if getattr(message, "message_type", None) == "response":
            return

        if getattr(message, "event", None) == "playing":
            return

        if await self._speech_synthesizer_handler(context).handle(message, context):
            return

        await self._dialog_lifecycle_handler(context).handle(message, context)

    async def on_disconnect(self, context: ConnectionContext) -> None:
        self._speech_synthesizer_handlers.pop(context.connection_id, None)
        self._dialog_lifecycle_handlers.pop(context.connection_id, None)
        await self._coordinator.cleanup_connection(context.connection_id)

    def _speech_synthesizer_handler(self, context: ConnectionContext) -> SpeechSynthesizerHandler:
        handler = self._speech_synthesizer_handlers.get(context.connection_id)
        if handler is None:
            interrupter = self._interrupter_factory(context)
            handler = SpeechSynthesizerHandler(self._coordinator, interrupter)
            self._speech_synthesizer_handlers[context.connection_id] = handler
        return handler

    def _dialog_lifecycle_handler(self, context: ConnectionContext) -> DialogLifecycleHandler:
        handler = self._dialog_lifecycle_handlers.get(context.connection_id)
        if handler is None:
            interrupter = self._interrupter_factory(context)
            handler = DialogLifecycleHandler(self._coordinator, interrupter)
            self._dialog_lifecycle_handlers[context.connection_id] = handler
        return handler
