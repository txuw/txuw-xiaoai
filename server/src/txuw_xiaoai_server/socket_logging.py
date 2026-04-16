from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .protocol import (
    InboundMessage,
    InboundRequest,
    InboundResponse,
    InstructionEventMessage,
    InstructionNewLine,
    KwsEventMessage,
    PlayingEventMessage,
    RecordStreamMessage,
    UnknownEventMessage,
    UnknownStreamMessage,
)


RAW_PREVIEW_LIMIT = 160


class SocketLogEntry(BaseModel):
    event_name: str
    connectionId: str
    direction: str = "inbound"
    frameType: str
    messageType: str
    status: str
    summary: str
    messageId: str = "-"
    event: str = "-"
    instructionName: str = "-"
    payloadKind: str = "-"
    tag: str = "-"
    command: str = "-"
    code: str = "-"
    errorType: str = "-"
    payloadError: str = "-"
    rawPreview: str = "-"
    byteLength: int | str = "-"

    def to_logger_extra(self) -> dict[str, Any]:
        return self.model_dump(exclude={"event_name"}, exclude_none=True)


def build_socket_log_entry(
    message: InboundMessage,
    connection_id: str,
    *,
    frame_type: str,
    raw_preview: str | None = None,
) -> SocketLogEntry:
    preview = _truncate_preview(raw_preview)

    if isinstance(message, InboundRequest):
        return SocketLogEntry(
            event_name="socket.message.request",
            connectionId=connection_id,
            frameType=frame_type,
            messageType="request",
            status="ok",
            summary=f"command={message.body.command}",
            messageId=message.body.id,
            command=message.body.command,
        )

    if isinstance(message, InboundResponse):
        code = "-" if message.body.code is None else str(message.body.code)
        return SocketLogEntry(
            event_name="socket.message.response",
            connectionId=connection_id,
            frameType=frame_type,
            messageType="response",
            status="ok",
            summary=f"code={code}",
            messageId=message.body.id,
            code=code,
        )

    if isinstance(message, InstructionEventMessage):
        return _build_instruction_entry(message, connection_id, frame_type, preview)

    if isinstance(message, PlayingEventMessage):
        status = "degraded" if message.payload_error else "ok"
        state = "-" if message.data is None else message.data.state.value
        return SocketLogEntry(
            event_name="socket.message.event",
            connectionId=connection_id,
            frameType=frame_type,
            messageType="event",
            status=status,
            summary=f"state={state}",
            messageId=message.id,
            event=message.event,
            payloadKind="PlayingEventData",
            errorType=_error_type(message.payload_error),
            payloadError=_error_message(message.payload_error),
        )

    if isinstance(message, KwsEventMessage):
        status = "degraded" if message.payload_error else "ok"
        summary = "Started"
        if message.data and message.data.kind == "Keyword":
            summary = f"keyword={message.data.keyword}"
        return SocketLogEntry(
            event_name="socket.message.event",
            connectionId=connection_id,
            frameType=frame_type,
            messageType="event",
            status=status,
            summary=summary,
            messageId=message.id,
            event=message.event,
            payloadKind="KwsEventData",
            errorType=_error_type(message.payload_error),
            payloadError=_error_message(message.payload_error),
        )

    if isinstance(message, UnknownEventMessage):
        return SocketLogEntry(
            event_name="socket.message.event",
            connectionId=connection_id,
            frameType=frame_type,
            messageType="event",
            status="ok",
            summary="unknown event",
            messageId=message.id,
            event=message.event,
            payloadKind="UnknownEventMessage",
        )

    if isinstance(message, RecordStreamMessage):
        return SocketLogEntry(
            event_name="socket.message.stream",
            connectionId=connection_id,
            frameType=frame_type,
            messageType="stream",
            status="ok",
            summary=f"bytes={len(message.frame.bytes)}",
            messageId=message.frame.id,
            tag=message.frame.tag,
            byteLength=len(message.frame.bytes),
        )

    if isinstance(message, UnknownStreamMessage):
        return SocketLogEntry(
            event_name="socket.message.stream",
            connectionId=connection_id,
            frameType=frame_type,
            messageType="stream",
            status="ok",
            summary=f"unknown stream bytes={len(message.frame.bytes)}",
            messageId=message.frame.id,
            tag=message.tag,
            payloadKind="UnknownStreamMessage",
            byteLength=len(message.frame.bytes),
        )

    raise TypeError(f"unsupported message type: {type(message).__name__}")


def _build_instruction_entry(
    message: InstructionEventMessage,
    connection_id: str,
    frame_type: str,
    raw_preview: str,
) -> SocketLogEntry:
    instruction_name = "-"
    payload_kind = "-"
    summary = "instruction"
    payload_error = message.payload_error

    if isinstance(message.data, InstructionNewLine):
        summary = "instruction line"
        if message.data.decoded_envelope is not None:
            decoded = message.data.decoded_envelope
            instruction_name = decoded.header.name
            payload_kind = decoded.payload_kind
            payload_error = decoded.payload_error or payload_error
            summary = _instruction_summary(decoded)
        elif message.data.payload_error is not None:
            payload_error = message.data.payload_error
    else:
        summary = "instruction new file"

    status = "degraded" if payload_error else "ok"
    return SocketLogEntry(
        event_name="socket.message.event",
        connectionId=connection_id,
        frameType=frame_type,
        messageType="event",
        status=status,
        summary=summary,
        messageId=message.id,
        event=message.event,
        instructionName=instruction_name,
        payloadKind=payload_kind,
        errorType=_error_type(payload_error),
        payloadError=_error_message(payload_error),
    )


def _instruction_summary(decoded) -> str:
    name = decoded.header.name
    payload = decoded.payload_model

    if decoded.payload_error is not None:
        return f"{name} degraded"

    if name == "RecognizeResult" and hasattr(payload, "results"):
        top_text = "-"
        if payload.results:
            first_item = payload.results[0]
            top_text = first_item.text if hasattr(first_item, "text") else str(first_item)
        return f"text={_truncate_preview(top_text)} is_final={payload.is_final} results={len(payload.results)}"
    if name in {"Speak", "SpeakStream"} and hasattr(payload, "text"):
        return f"text={_truncate_preview(payload.text)}"
    if name == "Play" and hasattr(payload, "audio_items"):
        return f"audio_items={len(payload.audio_items)} behavior={payload.play_behavior}"
    if name == "StopCapture" and hasattr(payload, "stop_time"):
        return f"stop_time={payload.stop_time}"
    if name == "SetProperty" and hasattr(payload, "name"):
        return f"name={payload.name} value={_truncate_preview(payload.value)}"
    if name == "InstructionControl" and hasattr(payload, "behavior"):
        return f"behavior={payload.behavior}"
    return name


def _error_type(error) -> str:
    return "-" if error is None else error.error_type


def _error_message(error) -> str:
    return "-" if error is None else _truncate_preview(error.message)


def _truncate_preview(value: str | None) -> str:
    if not value:
        return "-"
    return value if len(value) <= RAW_PREVIEW_LIMIT else f"{value[:RAW_PREVIEW_LIMIT]}..."


class IngressLogEntry(BaseModel):
    event_name: str
    connectionId: str
    direction: str = "inbound"
    frameType: str
    status: str = "raw"
    summary: str
    rawPayload: str = "-"
    byteLength: int | str = "-"

    def to_logger_extra(self) -> dict[str, Any]:
        return self.model_dump(exclude={"event_name"}, exclude_none=True)


def build_ingress_text_log_entry(raw_payload: str, connection_id: str) -> IngressLogEntry:
    return IngressLogEntry(
        event_name="socket.ingress.raw",
        connectionId=connection_id,
        frameType="text",
        summary="raw text frame",
        rawPayload=raw_payload,
    )


def build_ingress_binary_log_entry(
    payload_preview: str,
    byte_length: int,
    connection_id: str,
) -> IngressLogEntry:
    return IngressLogEntry(
        event_name="socket.ingress.raw",
        connectionId=connection_id,
        frameType="binary",
        summary="raw binary frame",
        rawPayload=_truncate_preview(payload_preview),
        byteLength=byte_length,
    )
