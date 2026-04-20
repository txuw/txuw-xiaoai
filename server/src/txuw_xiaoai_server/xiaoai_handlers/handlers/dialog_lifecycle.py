from __future__ import annotations

from typing import Any

from ..ports import ConnectionContext, LegacyAudioInterrupter
from ..services.tts_replacement_coordinator import TtsReplacementCoordinator


class DialogLifecycleHandler:
    """处理对话生命周期相关指令。"""

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

        if header.name == "StartStream":
            if not self._coordinator.is_takeover_dialog(context.connection_id, header.dialog_id):
                return False
            await self._coordinator.prime_dialog(
                context,
                self._interrupter,
                dialog_id=header.dialog_id,
                instruction_name=header.name,
            )
            return True

        if header.name == "Finish":
            await self._coordinator.cleanup_dialog(context.connection_id, header.dialog_id)
            return True

        if not self._coordinator.is_takeover_dialog(context.connection_id, header.dialog_id):
            return False

        return header.name in {"StartAnswer", "FinishAnswer", "FinishStream"}
