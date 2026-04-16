from __future__ import annotations

import json
import logging

from pydantic import TypeAdapter

from .models import (
    EventBody,
    InboundMessage,
    InboundRequest,
    InboundResponse,
    InboundStream,
    InstructionPayloadError,
    InstructionEventMessage,
    KwsEventData,
    KwsEventMessage,
    PlayingEventData,
    PlayingEventMessage,
    RecordStreamMessage,
    RequestBody,
    ResponseBody,
    StreamFrame,
    UnknownEventMessage,
    UnknownStreamMessage,
    decode_instruction_event_data,
)


logger = logging.getLogger(__name__)

_REQUEST_ADAPTER = TypeAdapter(RequestBody)
_RESPONSE_ADAPTER = TypeAdapter(ResponseBody)


def parse_text_message(raw: str) -> InboundMessage:
    logger.debug("Parsing text message", extra={"rawPreview": _truncate_text(raw, 200)})
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("message must be an object")
    if "Request" in payload:
        return InboundRequest(body=_REQUEST_ADAPTER.validate_python(payload["Request"]))
    if "Response" in payload:
        return InboundResponse(body=_RESPONSE_ADAPTER.validate_python(payload["Response"]))
    if "Event" in payload:
        body = EventBody.model_validate(payload["Event"])
        return _parse_event_body(body)
    raise ValueError("unknown message type")


def parse_stream_frame(data: bytes) -> InboundStream:
    frame = StreamFrame.model_validate_json(data)
    if frame.tag == "record":
        return RecordStreamMessage(frame=frame)
    return UnknownStreamMessage(tag=frame.tag, frame=frame)


def _parse_event_body(body: EventBody):
    if body.event == "instruction":
        data, payload_error = decode_instruction_event_data(body.data)
        return InstructionEventMessage(
            id=body.id,
            data=data,
            raw_data=body.data,
            payload_error=payload_error,
        )
    if body.event == "playing":
        return _parse_known_event(
            body,
            PlayingEventData,
            lambda data, error: PlayingEventMessage(
                id=body.id,
                data=data,
                raw_data=body.data,
                payload_error=error,
            ),
        )
    if body.event == "kws":
        return _parse_known_event(
            body,
            KwsEventData,
            lambda data, error: KwsEventMessage(
                id=body.id,
                data=data,
                raw_data=body.data,
                payload_error=error,
            ),
        )
    return UnknownEventMessage(id=body.id, event=body.event, raw_data=body.data)


def _parse_known_event(body: EventBody, model, builder):
    try:
        return builder(model.model_validate(body.data), None)
    except (ValueError, TypeError) as exc:
        error = InstructionPayloadError(
            model_name=model.__name__,
            error_type=type(exc).__name__,
            message=str(exc),
        )
        return builder(None, error)


def _truncate_text(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[:limit]}..."
