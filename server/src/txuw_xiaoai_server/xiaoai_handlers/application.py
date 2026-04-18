from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .agent import AgentStreamService
from .handlers.dialog_lifecycle import DialogLifecycleHandler
from .handlers.query import QueryHandler
from .handlers.speech_recognizer import SpeechRecognizerHandler
from .handlers.speech_synthesizer import SpeechSynthesizerHandler
from .ports import ConnectionContext, LegacyAudioInterrupter, StreamingTtsEngine
from .services.tts_replacement_coordinator import TtsReplacementCoordinator
from .sessions import DialogSessionStore


class XiaoAiApplication:
    """小爱播报拦截与 Agent 输出接管的业务入口。"""

    def __init__(
        self,
        *,
        engine_factory: Callable[[], StreamingTtsEngine],
        interrupter_factory: Callable[[ConnectionContext], LegacyAudioInterrupter],
        enabled: bool,
        agent_service: AgentStreamService | None = None,
        agent_enabled: bool = False,
    ) -> None:
        session_store = DialogSessionStore()
        coordinator = TtsReplacementCoordinator(
            session_store,
            engine_factory=engine_factory,
            enabled=enabled,
            agent_service=agent_service,
            agent_enabled=agent_enabled,
        )
        self._coordinator = coordinator
        self._interrupter_factory = interrupter_factory
        self._query_handlers: dict[str, QueryHandler] = {}
        self._speech_recognizer_handlers: dict[str, SpeechRecognizerHandler] = {}
        self._speech_synthesizer_handlers: dict[str, SpeechSynthesizerHandler] = {}
        self._dialog_lifecycle_handlers: dict[str, DialogLifecycleHandler] = {}

    async def handle_inbound(self, message: Any, context: ConnectionContext) -> None:
        """应用层直接分发消息，避免额外中间层增加理解成本。"""

        if getattr(message, "message_type", None) == "response":
            return

        if getattr(message, "event", None) == "playing":
            return

        if await self._query_handler(context).handle(message, context):
            return

        if await self._speech_recognizer_handler(context).handle(message, context):
            return

        if await self._speech_synthesizer_handler(context).handle(message, context):
            return

        await self._dialog_lifecycle_handler(context).handle(message, context)

    async def on_disconnect(self, context: ConnectionContext) -> None:
        self._query_handlers.pop(context.connection_id, None)
        self._speech_recognizer_handlers.pop(context.connection_id, None)
        self._speech_synthesizer_handlers.pop(context.connection_id, None)
        self._dialog_lifecycle_handlers.pop(context.connection_id, None)
        await self._coordinator.cleanup_connection(context.connection_id)

    async def speak_text(
        self,
        context: ConnectionContext,
        *,
        dialog_id: str,
        text: str,
        instruction_name: str = "ServerSpeak",
        source: str = "proactive",
    ) -> None:
        interrupter = self._interrupter_factory(context)
        await self._coordinator.speak_text(
            context,
            interrupter,
            dialog_id=dialog_id,
            instruction_name=instruction_name,
            source=source,
            text=text,
        )

    async def close(self) -> None:
        await self._coordinator.close()

    def _query_handler(self, context: ConnectionContext) -> QueryHandler:
        handler = self._query_handlers.get(context.connection_id)
        if handler is None:
            interrupter = self._interrupter_factory(context)
            handler = QueryHandler(self._coordinator, interrupter)
            self._query_handlers[context.connection_id] = handler
        return handler

    def _speech_recognizer_handler(self, context: ConnectionContext) -> SpeechRecognizerHandler:
        handler = self._speech_recognizer_handlers.get(context.connection_id)
        if handler is None:
            interrupter = self._interrupter_factory(context)
            handler = SpeechRecognizerHandler(self._coordinator, interrupter)
            self._speech_recognizer_handlers[context.connection_id] = handler
        return handler

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
