from __future__ import annotations

import json

from pydantic import TypeAdapter

from .models import (
    ClientEventMessage,
    EventBody,
    RequestMessage,
    ResponseMessage,
    StreamFrame,
)


_REQUEST_ADAPTER = TypeAdapter(RequestMessage)
_RESPONSE_ADAPTER = TypeAdapter(ResponseMessage)


def parse_text_message(raw: str) -> RequestMessage | ResponseMessage | ClientEventMessage:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("message must be an object")
    if "Request" in payload:
        return _REQUEST_ADAPTER.validate_python(payload)
    if "Response" in payload:
        return _RESPONSE_ADAPTER.validate_python(payload)
    if "Event" in payload:
        body = EventBody.model_validate(payload["Event"])
        return ClientEventMessage.from_body(body)
    raise ValueError("unknown message type")


def parse_stream_frame(data: bytes) -> StreamFrame:
    return StreamFrame.model_validate_json(data)
