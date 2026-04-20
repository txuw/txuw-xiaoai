from __future__ import annotations

from typing import Any

from ..ports import ConnectionContext, LegacyAudioInterrupter
from ..services.tts_replacement_coordinator import TtsReplacementCoordinator


class QueryHandler:
    """处理问答文本入口。"""

    def __init__(
        self,
        coordinator: TtsReplacementCoordinator,
        interrupter: LegacyAudioInterrupter,
    ) -> None:
        self._coordinator = coordinator
        self._interrupter = interrupter

    async def handle(self, message: Any, context: ConnectionContext) -> bool:
        envelope = getattr(getattr(message, "data", None), "decoded_envelope", None)
        if envelope is None:
            return False

        header = envelope.header
        if header.name != "Query" or header.namespace != "Template":
            return False
        if not self._coordinator.is_takeover_dialog(context.connection_id, header.dialog_id):
            return False

        text = ""
        if isinstance(envelope.raw_payload, dict):
            raw_text = envelope.raw_payload.get("text", "")
            if isinstance(raw_text, str):
                text = raw_text

        await self._coordinator.on_query(
            context,
            self._interrupter,
            dialog_id=header.dialog_id,
            instruction_name=header.name,
            text=text,
        )
        return True
