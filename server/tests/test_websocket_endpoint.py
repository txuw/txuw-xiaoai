from __future__ import annotations

import json

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from txuw_xiaoai_server.app import create_app


def test_healthz() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_websocket_accepts_valid_text_message() -> None:
    client = TestClient(create_app())
    with client.websocket_connect("/ws") as websocket:
        websocket.send_text(
            json.dumps({"Event": {"id": "1", "event": "playing", "data": "Idle"}})
        )


def test_websocket_accepts_valid_binary_stream() -> None:
    client = TestClient(create_app())
    with client.websocket_connect("/ws") as websocket:
        websocket.send_bytes(
            json.dumps({"id": "2", "tag": "record", "bytes": [1, 2, 3], "data": None}).encode()
        )


def test_websocket_closes_on_invalid_payload() -> None:
    client = TestClient(create_app())
    with client.websocket_connect("/ws") as websocket:
        websocket.send_text(json.dumps({"Event": {"id": "bad", "event": "playing", "data": 123}}))
        try:
            websocket.receive_text()
        except WebSocketDisconnect as exc:
            assert exc.code == 1007
        else:
            raise AssertionError("websocket should close on invalid payload")
