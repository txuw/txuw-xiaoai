from __future__ import annotations

import json
import logging

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from txuw_xiaoai_server.app import create_app
from txuw_xiaoai_server.xiaoai_handlers import XiaoAiApplication


def _create_test_client() -> TestClient:
    application = XiaoAiApplication(
        engine_factory=lambda: None,  # type: ignore[arg-type]
        interrupter_factory=lambda _context: None,  # type: ignore[arg-type]
        enabled=False,
    )
    return TestClient(create_app(application))


def test_healthz() -> None:
    client = _create_test_client()
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_websocket_accepts_valid_text_message() -> None:
    client = _create_test_client()
    with client.websocket_connect("/ws") as websocket:
        websocket.send_text(
            json.dumps({"Event": {"id": "1", "event": "playing", "data": "Idle"}})
        )


def test_websocket_accepts_valid_binary_stream() -> None:
    client = _create_test_client()
    with client.websocket_connect("/ws") as websocket:
        websocket.send_bytes(
            json.dumps({"id": "2", "tag": "record", "bytes": [1, 2, 3], "data": None}).encode()
        )


def test_websocket_closes_on_invalid_payload() -> None:
    client = _create_test_client()
    with client.websocket_connect("/ws") as websocket:
        websocket.send_text(json.dumps({"Unknown": {"id": "bad"}}))
        try:
            websocket.receive_text()
        except WebSocketDisconnect as exc:
            assert exc.code == 1007
        else:
            raise AssertionError("websocket should close on invalid payload")


def test_logs_structured_event_summary(caplog) -> None:
    client = _create_test_client()
    with caplog.at_level(logging.INFO):
        with client.websocket_connect("/ws") as websocket:
            websocket.send_text(
                json.dumps({"Event": {"id": "1", "event": "playing", "data": "Idle"}})
            )

    ingress_records = [record for record in caplog.records if record.msg == "socket.ingress.raw"]
    assert ingress_records
    assert ingress_records[-1].frameType == "text"

    event_records = [record for record in caplog.records if record.msg == "socket.message.event"]
    assert event_records
    record = event_records[-1]
    assert record.connectionId != "-"
    assert record.messageType == "event"
    assert record.event == "playing"
    assert record.status == "ok"
    assert record.summary == "state=Idle"
    assert getattr(record, "rawPayload", "-") == "-"
    assert getattr(record, "rawPreview", "-") == "-"


def test_logs_degraded_instruction_summary(caplog) -> None:
    client = _create_test_client()
    instruction = {
        "header": {
            "dialog_id": "dialog-1",
            "id": "msg-1",
            "name": "RecognizeResult",
            "namespace": "SpeechRecognizer",
        },
        "payload": {"results": [{"confidence": 0.8, "text": "hello"}]},
    }
    with caplog.at_level(logging.INFO):
        with client.websocket_connect("/ws") as websocket:
            websocket.send_text(
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

    event_records = [record for record in caplog.records if record.msg == "socket.message.event"]
    assert event_records
    record = event_records[-1]
    assert record.event == "instruction"
    assert record.instructionName == "RecognizeResult"
    assert record.payloadKind == "RecognizeResultPayload"
    assert record.status == "degraded"
    assert "Field required" in record.payloadError


def test_logs_full_raw_ingress_text_line(caplog) -> None:
    client = _create_test_client()
    raw_message = json.dumps({"Event": {"id": "1", "event": "playing", "data": "Idle"}})
    with caplog.at_level(logging.INFO):
        with client.websocket_connect("/ws") as websocket:
            websocket.send_text(raw_message)

    ingress_records = [record for record in caplog.records if record.msg == "socket.ingress.raw"]
    assert ingress_records
    record = ingress_records[-1]
    assert record.frameType == "text"
    assert record.rawPayload == raw_message
    assert record.summary == "raw text frame"


def test_logs_binary_ingress_with_length_and_preview(caplog) -> None:
    client = _create_test_client()
    raw_message = json.dumps({"id": "2", "tag": "record", "bytes": [1, 2, 3], "data": None}).encode()
    with caplog.at_level(logging.INFO):
        with client.websocket_connect("/ws") as websocket:
            websocket.send_bytes(raw_message)

    ingress_records = [record for record in caplog.records if record.msg == "socket.ingress.raw"]
    assert ingress_records
    record = ingress_records[-1]
    assert record.frameType == "binary"
    assert record.byteLength == len(raw_message)
    assert "record" in record.rawPayload
