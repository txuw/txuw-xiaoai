from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class RequestBody(BaseModel):
    id: str
    command: str
    payload: Any | None = None


class InboundRequest(BaseModel):
    message_type: Literal["request"] = "request"
    body: RequestBody


class ResponseBody(BaseModel):
    id: str
    code: int | None = None
    msg: str | None = None
    data: Any | None = None


class InboundResponse(BaseModel):
    message_type: Literal["response"] = "response"
    body: ResponseBody


class GenericPayload(BaseModel):
    model_config = ConfigDict(extra="allow")


class InstructionPayloadError(BaseModel):
    model_name: str
    error_type: str
    message: str


class InstructionHeader(BaseModel):
    dialog_id: str
    id: str
    name: str
    namespace: str


class Emotion(BaseModel):
    category: str
    level: str


class RecognizeResultItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    confidence: float = 0.0
    text: str = ""
    asr_binary_offset: int | None = None
    begin_offset: int | None = None
    end_offset: int | None = None
    is_nlp_request: bool | None = None
    is_stop: bool | None = None
    origin_text: str | None = None


class AudioCp(BaseModel):
    id: str
    name: str


class AudioItemId(BaseModel):
    audio_id: str
    cp: AudioCp


class AudioLog(BaseModel):
    eid: str
    refer: str


class AudioStreamMeta(BaseModel):
    authentication: bool
    duration_in_ms: int
    offset_in_ms: int
    url: str


class AudioItem(BaseModel):
    item_id: AudioItemId
    log: AudioLog
    stream: AudioStreamMeta


class EmptyPayload(BaseModel):
    pass


class RecognizeResultPayload(BaseModel):
    is_final: bool
    is_vad_begin: bool
    results: list[RecognizeResultItem] = Field(default_factory=list)


class StopCapturePayload(BaseModel):
    stop_time: int


class SpeakPayload(BaseModel):
    text: str
    emotion: Emotion | None = None


class PlayPayload(BaseModel):
    audio_items: list[AudioItem]
    audio_type: str
    loadmore_token: str
    needs_loadmore: bool
    origin_id: str
    play_behavior: str


class SetPropertyPayload(BaseModel):
    name: str
    value: str


class InstructionControlPayload(BaseModel):
    behavior: str


InstructionPayload = (
    EmptyPayload
    | RecognizeResultPayload
    | StopCapturePayload
    | SpeakPayload
    | PlayPayload
    | SetPropertyPayload
    | InstructionControlPayload
    | GenericPayload
)


class InstructionEnvelopeDecoded(BaseModel):
    raw: str
    header: InstructionHeader
    payload_kind: str
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    payload_model: InstructionPayload
    payload_error: InstructionPayloadError | None = None


class InstructionNewFile(BaseModel):
    kind: Literal["NewFile"] = "NewFile"


class InstructionNewLine(BaseModel):
    kind: Literal["NewLine"] = "NewLine"
    raw_line: str
    decoded_envelope: InstructionEnvelopeDecoded | None = None
    payload_error: InstructionPayloadError | None = None


InstructionEventData = InstructionNewFile | InstructionNewLine


class PlayingState(str, Enum):
    PLAYING = "Playing"
    PAUSED = "Paused"
    IDLE = "Idle"


class PlayingEventData(BaseModel):
    state: PlayingState

    @model_validator(mode="before")
    @classmethod
    def from_raw(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            return {"state": value}
        if isinstance(value, dict) and "state" in value:
            return value
        raise TypeError("invalid playing event payload")


class KwsEventData(BaseModel):
    kind: Literal["Started", "Keyword"]
    keyword: str | None = None

    @model_validator(mode="before")
    @classmethod
    def from_raw(cls, value: Any) -> dict[str, Any]:
        if value == "Started":
            return {"kind": "Started", "keyword": None}
        if isinstance(value, dict) and "Keyword" in value:
            return {"kind": "Keyword", "keyword": value["Keyword"]}
        if isinstance(value, dict) and "kind" in value:
            return value
        raise TypeError("invalid kws event payload")


class InstructionEventMessage(BaseModel):
    message_type: Literal["event"] = "event"
    event: Literal["instruction"] = "instruction"
    id: str
    data: InstructionEventData
    raw_data: Any | None = None
    payload_error: InstructionPayloadError | None = None


class PlayingEventMessage(BaseModel):
    message_type: Literal["event"] = "event"
    event: Literal["playing"] = "playing"
    id: str
    data: PlayingEventData | None = None
    raw_data: Any | None = None
    payload_error: InstructionPayloadError | None = None


class KwsEventMessage(BaseModel):
    message_type: Literal["event"] = "event"
    event: Literal["kws"] = "kws"
    id: str
    data: KwsEventData | None = None
    raw_data: Any | None = None
    payload_error: InstructionPayloadError | None = None


class UnknownEventMessage(BaseModel):
    message_type: Literal["event"] = "event"
    event: str
    id: str
    raw_data: Any | None = None


class EventBody(BaseModel):
    id: str
    event: str
    data: Any | None = None


InboundEvent = (
    InstructionEventMessage | PlayingEventMessage | KwsEventMessage | UnknownEventMessage
)


class StreamFrame(BaseModel):
    id: str
    tag: str
    bytes: bytes
    data: Any | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_bytes(cls, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("bytes"), list):
            payload = dict(value)
            payload["bytes"] = bytes(payload["bytes"])
            return payload
        return value

    def to_pretty_dict(self) -> dict[str, Any]:
        result = self.model_dump()
        result["bytes"] = list(self.bytes)
        return json.loads(json.dumps(result))


class RecordStreamMessage(BaseModel):
    message_type: Literal["stream"] = "stream"
    tag: Literal["record"] = "record"
    frame: StreamFrame


class UnknownStreamMessage(BaseModel):
    message_type: Literal["stream"] = "stream"
    tag: str
    frame: StreamFrame


InboundStream = RecordStreamMessage | UnknownStreamMessage
InboundMessage = InboundRequest | InboundResponse | InboundEvent | InboundStream


_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "RecognizeResult": RecognizeResultPayload,
    "StopCapture": StopCapturePayload,
    "Speak": SpeakPayload,
    "SpeakStream": SpeakPayload,
    "Play": PlayPayload,
    "SetProperty": SetPropertyPayload,
    "InstructionControl": InstructionControlPayload,
}
_EMPTY_PAYLOAD_NAMES = {"StartStream", "FinishStream", "FinishSpeakStream", "Finish"}


def decode_instruction_event_data(value: Any) -> tuple[InstructionEventData, InstructionPayloadError | None]:
    if value == "NewFile":
        return InstructionNewFile(), None

    if isinstance(value, dict) and "NewLine" in value:
        raw_line = value["NewLine"]
        try:
            decoded = decode_instruction_line(raw_line)
            return InstructionNewLine(raw_line=raw_line, decoded_envelope=decoded), decoded.payload_error
        except (ValidationError, ValueError, TypeError) as exc:
            payload_error = InstructionPayloadError(
                model_name="InstructionEnvelopeDecoded",
                error_type=type(exc).__name__,
                message=str(exc),
            )
            return InstructionNewLine(raw_line=raw_line, payload_error=payload_error), payload_error

    raise TypeError("invalid instruction event payload")


def decode_instruction_line(raw: str) -> InstructionEnvelopeDecoded:
    envelope = _InstructionEnvelope.model_validate_json(raw)
    payload_kind, payload_model, payload_error = _decode_instruction_payload(
        envelope.header.name,
        envelope.payload,
    )
    return InstructionEnvelopeDecoded(
        raw=raw,
        header=envelope.header,
        payload_kind=payload_kind,
        raw_payload=envelope.payload,
        payload_model=payload_model,
        payload_error=payload_error,
    )


class _InstructionEnvelope(BaseModel):
    header: InstructionHeader
    payload: dict[str, Any] = Field(default_factory=dict)


def _decode_instruction_payload(
    name: str,
    payload: dict[str, Any],
) -> tuple[str, InstructionPayload, InstructionPayloadError | None]:
    if name in _EMPTY_PAYLOAD_NAMES:
        return "EmptyPayload", EmptyPayload.model_validate(payload), None

    model = _PAYLOAD_MODELS.get(name)
    if model is None:
        return "GenericPayload", GenericPayload.model_validate(payload), None

    try:
        return model.__name__, model.model_validate(payload), None
    except (ValidationError, ValueError, TypeError) as exc:
        error = InstructionPayloadError(
            model_name=model.__name__,
            error_type=type(exc).__name__,
            message=str(exc),
        )
        return model.__name__, GenericPayload.model_validate(payload), error
