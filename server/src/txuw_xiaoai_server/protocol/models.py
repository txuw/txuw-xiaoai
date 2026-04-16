from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RequestBody(BaseModel):
    id: str
    command: str
    payload: Any | None = None


class RequestMessage(BaseModel):
    Request: RequestBody


class ResponseBody(BaseModel):
    id: str
    code: int | None = None
    msg: str | None = None
    data: Any | None = None


class ResponseMessage(BaseModel):
    Response: ResponseBody


class GenericEventData(BaseModel):
    model_config = ConfigDict(extra="allow")


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


class GenericPayload(BaseModel):
    model_config = ConfigDict(extra="allow")


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


class InstructionEnvelope(BaseModel):
    header: InstructionHeader
    payload: dict[str, Any] = Field(default_factory=dict)


class DecodedInstruction(BaseModel):
    raw: str
    envelope: InstructionEnvelope
    payload_model: InstructionPayload

    @property
    def name(self) -> str:
        return self.envelope.header.name

    @classmethod
    def from_raw(cls, raw: str) -> "DecodedInstruction":
        envelope = InstructionEnvelope.model_validate_json(raw)
        payload_model = _decode_instruction_payload(envelope.header.name, envelope.payload)
        return cls(raw=raw, envelope=envelope, payload_model=payload_model)


def _decode_instruction_payload(name: str, payload: dict[str, Any]) -> InstructionPayload:
    if name in {"StartStream", "FinishStream", "FinishSpeakStream", "Finish"}:
        return EmptyPayload.model_validate(payload)
    if name == "RecognizeResult":
        return RecognizeResultPayload.model_validate(payload)
    if name == "StopCapture":
        return StopCapturePayload.model_validate(payload)
    if name in {"Speak", "SpeakStream"}:
        return SpeakPayload.model_validate(payload)
    if name == "Play":
        return PlayPayload.model_validate(payload)
    if name == "SetProperty":
        return SetPropertyPayload.model_validate(payload)
    if name == "InstructionControl":
        return InstructionControlPayload.model_validate(payload)
    return GenericPayload.model_validate(payload)


class InstructionEventData(BaseModel):
    kind: Literal["NewFile", "NewLine"]
    line: str | None = None
    decoded_instruction: DecodedInstruction | None = None

    @model_validator(mode="before")
    @classmethod
    def from_raw(cls, value: Any) -> dict[str, Any]:
        if value == "NewFile":
            return {"kind": "NewFile", "line": None}
        if isinstance(value, dict):
            if "NewLine" in value:
                return {"kind": "NewLine", "line": value["NewLine"]}
            if "kind" in value:
                return value
            # 空字典或未识别格式，按 NewFile 兜底
            return {"kind": "NewFile", "line": None}
        raise TypeError("invalid instruction event payload")

    @model_validator(mode="after")
    def decode_instruction(self) -> "InstructionEventData":
        if self.kind == "NewLine" and self.line:
            self.decoded_instruction = DecodedInstruction.from_raw(self.line)
        return self


class EventBody(BaseModel):
    id: str
    event: str
    data: Any | None = None


class ClientEventMessage(BaseModel):
    id: str
    event: str
    data: Any | None

    @classmethod
    def from_body(cls, body: EventBody) -> "ClientEventMessage":
        if body.event == "instruction":
            data = InstructionEventData.model_validate(body.data)
        elif body.event == "playing":
            data = PlayingEventData.model_validate(body.data)
        elif body.event == "kws":
            data = KwsEventData.model_validate(body.data)
        elif body.data is None:
            data = None
        else:
            data = GenericEventData.model_validate(body.data)
        return cls(id=body.id, event=body.event, data=data)


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
