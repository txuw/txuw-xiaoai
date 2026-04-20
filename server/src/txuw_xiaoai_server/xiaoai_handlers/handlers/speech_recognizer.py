from __future__ import annotations

from typing import Any

from ..ports import ConnectionContext, LegacyAudioInterrupter
from ..services.tts_replacement_coordinator import TtsReplacementCoordinator


class SpeechRecognizerHandler:
    """处理 ASR 结果，为 LLM 入口提供兜底文本。"""

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
        if header.namespace != "SpeechRecognizer" or header.name != "RecognizeResult":
            return False
        if not self._coordinator.is_takeover_dialog(context.connection_id, header.dialog_id):
            return False

        payload = envelope.payload_model
        results = getattr(payload, "results", [])
        final_text = _extract_recognized_text(results)
        await self._coordinator.on_recognize_result(
            context,
            self._interrupter,
            dialog_id=header.dialog_id,
            instruction_name=header.name,
            text=final_text,
            is_final=bool(getattr(payload, "is_final", False)),
        )
        return True


def _extract_recognized_text(results: list[Any]) -> str:
    """兼容协议对象和 degraded 字典结果，提取最后一条可用文本。"""

    for item in reversed(results):
        text = _result_field(item, "text")
        if isinstance(text, str) and text.strip():
            return text

        origin_text = _result_field(item, "origin_text")
        if isinstance(origin_text, str) and origin_text.strip():
            return origin_text
    return ""


def _result_field(item: Any, field_name: str) -> Any:
    """设备上报缺字段时会退化为 dict，这里统一兜底读取。"""

    if isinstance(item, dict):
        return item.get(field_name, "")
    return getattr(item, field_name, "")
