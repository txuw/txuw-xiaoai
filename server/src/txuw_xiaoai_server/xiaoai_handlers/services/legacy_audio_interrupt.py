from __future__ import annotations

import logging

from ..ports import LegacyAudioInterrupter, PlaybackControlPort


logger = logging.getLogger(__name__)


class AbortLegacyXiaoaiInterrupter(LegacyAudioInterrupter):
    """通过远端 shell 中断旧小爱播报。"""

    def __init__(
        self,
        playback_port: PlaybackControlPort,
        *,
        enabled: bool = True,
        command: str = "/etc/init.d/mico_aivs_lab restart >/dev/null 2>&1",
    ) -> None:
        self._playback_port = playback_port
        self._enabled = enabled
        self._command = command

    async def interrupt(self, connection_id: str, dialog_id: str) -> bool:
        if not self._enabled:
            logger.info(
                "tts.legacy_interrupt.skipped",
                extra={
                    "connectionId": connection_id,
                    "dialogId": dialog_id,
                    "summary": "legacy interrupt disabled",
                },
            )
            return False
        await self._playback_port.run_shell(self._command)
        logger.info(
            "tts.legacy_interrupt.executed",
            extra={
                "connectionId": connection_id,
                "dialogId": dialog_id,
                "summary": "legacy interrupt command sent",
            },
        )
        return True
