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

        # 在 NLP 应答开始阶段就预抢占旧小爱，避免等到 SpeakStream 才打断导致原声抢跑。
        if header.name in {"StartStream", "Query"}:
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

        return header.name in {"StartAnswer", "FinishAnswer", "FinishStream"}
