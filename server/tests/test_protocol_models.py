from __future__ import annotations

import json

import pytest

from txuw_xiaoai_server.protocol import (
    ClientEventMessage,
    InstructionEventData,
    KwsEventData,
    PlayingEventData,
    PlayingState,
    parse_stream_frame,
    parse_text_message,
)


def test_parse_playing_event() -> None:
    message = parse_text_message(
        json.dumps({"Event": {"id": "1", "event": "playing", "data": "Playing"}})
    )
    assert isinstance(message, ClientEventMessage)
    assert isinstance(message.data, PlayingEventData)
    assert message.data.state is PlayingState.PLAYING


def test_parse_kws_keyword_event() -> None:
    message = parse_text_message(
        json.dumps({"Event": {"id": "2", "event": "kws", "data": {"Keyword": "小浮浮"}}})
    )
    assert isinstance(message, ClientEventMessage)
    assert isinstance(message.data, KwsEventData)
    assert message.data.kind == "Keyword"
    assert message.data.keyword == "小浮浮"


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
            "results": [{"confidence": 0.8, "text": "今天天气怎么样"}],
        },
    }
    message = parse_text_message(
        json.dumps(
            {
                "Event": {
                    "id": "3",
                    "event": "instruction",
                    "data": {"NewLine": json.dumps(instruction, ensure_ascii=False)},
                }
            },
            ensure_ascii=False,
        )
    )
    assert isinstance(message, ClientEventMessage)
    assert isinstance(message.data, InstructionEventData)
    assert message.data.kind == "NewLine"
    assert message.data.decoded_instruction is not None
    assert message.data.decoded_instruction.name == "RecognizeResult"


def test_parse_stream_frame_from_binary_json() -> None:
    frame = parse_stream_frame(
        json.dumps({"id": "stream-1", "tag": "record", "bytes": [1, 2, 3], "data": None}).encode()
    )
    assert frame.tag == "record"
    assert frame.bytes == b"\x01\x02\x03"


def test_invalid_outer_message_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_text_message(json.dumps({"Unknown": {"id": "oops"}}))
