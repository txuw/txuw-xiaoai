from __future__ import annotations

from typing import Any

from ..ports import ConnectionContext, LegacyAudioInterrupter
from ..services.tts_replacement_coordinator import TtsReplacementCoordinator


class SpeechSynthesizerHandler:
    """处理旧小爱播报相关指令，并切换到新 TTS 链路。"""

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
        if header.namespace != "SpeechSynthesizer":
            return False

        if header.name in {"Speak", "SpeakStream"}:
            payload = envelope.payload_model
            text = getattr(payload, "text", "")

            # 旧小爱一进入播报阶段就先尝试打断，优先保证只保留新声音。
            await self._coordinator.prime_dialog(
                context,
                self._interrupter,
                dialog_id=header.dialog_id,
                instruction_name=header.name,
            )
            await self._coordinator.on_text(
                context,
                self._interrupter,
                dialog_id=header.dialog_id,
                instruction_name=header.name,
                text=text,
            )
            return True

        if header.name == "FinishSpeakStream":
            await self._coordinator.complete(context.connection_id, header.dialog_id)
            return True

        return False
