from __future__ import annotations

import json

import pytest

from txuw_xiaoai_server.protocol import (
    InboundRequest,
    InstructionEventMessage,
    InstructionNewLine,
    RecordStreamMessage,
    UnknownEventMessage,
    parse_stream_frame,
    parse_text_message,
)


def test_parse_request_message() -> None:
    message = parse_text_message(
        json.dumps({"Request": {"id": "r1", "command": "get_version", "payload": None}})
    )
    assert isinstance(message, InboundRequest)
    assert message.body.command == "get_version"


def test_parse_instruction_event_decodes_payload() -> None:
    instruction = {
        "header": {
            "dialog_id": "dialog-1",
            "id": "msg-1",
            "name": "RecognizeResult",
            "namespace": "SpeechRecognizer",
        },
        "payload": {
            "is_final": True,
            "is_vad_begin": False,
            "results": [{"confidence": 0.8, "text": "hello"}],
        },
    }
    message = parse_text_message(
        json.dumps(
            {
                "Event": {
                    "id": "3",
                    "event": "instruction",
                    "data": {"NewLine": json.dumps(instruction)},
                }
            }
        )
    )
    assert isinstance(message, InstructionEventMessage)
    assert isinstance(message.data, InstructionNewLine)
    assert message.data.decoded_envelope is not None
    assert message.data.decoded_envelope.header.name == "RecognizeResult"
    assert message.data.decoded_envelope.payload_kind == "RecognizeResultPayload"
    assert message.payload_error is None


def test_recognize_result_allows_missing_is_vad_begin() -> None:
    instruction = {
        "header": {
            "dialog_id": "dialog-1",
            "id": "msg-1",
            "name": "RecognizeResult",
            "namespace": "SpeechRecognizer",
        },
        "payload": {
            "is_final": True,
            "results": [{"confidence": 0.8, "text": "hello"}],
        },
    }
    message = parse_text_message(
        json.dumps(
            {
                "Event": {
                    "id": "4",
                    "event": "instruction",
                    "data": {"NewLine": json.dumps(instruction)},
                }
            }
        )
    )
    assert isinstance(message, InstructionEventMessage)
    assert message.data.decoded_envelope is not None
    assert message.data.decoded_envelope.payload_kind == "RecognizeResultPayload"
    assert message.data.decoded_envelope.payload_error is None
    assert message.payload_error is None
    assert message.data.decoded_envelope.payload_model.is_vad_begin is None


def test_stop_capture_allows_missing_stop_time() -> None:
    instruction = {
        "header": {
            "dialog_id": "dialog-1",
            "id": "msg-2",
            "name": "StopCapture",
            "namespace": "SpeechRecognizer",
        },
        "payload": {},
    }
    message = parse_text_message(
        json.dumps(
            {
                "Event": {
                    "id": "5",
                    "event": "instruction",
                    "data": {"NewLine": json.dumps(instruction)},
                }
            }
        )
    )

    assert isinstance(message, InstructionEventMessage)
    assert message.data.decoded_envelope is not None
    assert message.data.decoded_envelope.payload_kind == "StopCapturePayload"
    assert message.data.decoded_envelope.payload_error is None
    assert message.payload_error is None
    assert message.data.decoded_envelope.payload_model.stop_time is None


def test_unknown_event_returns_unknown_message() -> None:
    message = parse_text_message(
        json.dumps({"Event": {"id": "6", "event": "custom", "data": {"x": 1}}})
    )
    assert isinstance(message, UnknownEventMessage)
    assert message.event == "custom"


def test_parse_stream_frame_from_binary_json() -> None:
    frame = parse_stream_frame(
        json.dumps({"id": "stream-1", "tag": "record", "bytes": [1, 2, 3], "data": None}).encode()
    )
    assert isinstance(frame, RecordStreamMessage)
    assert frame.frame.bytes == b"\x01\x02\x03"


def test_invalid_outer_message_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_text_message(json.dumps({"Unknown": {"id": "oops"}}))
