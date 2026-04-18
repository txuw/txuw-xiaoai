from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from txuw_xiaoai_server.app import create_app
from txuw_xiaoai_server.xiaoai_handlers import XiaoAiApplication

from .dataset_utils import load_all_datasets


@pytest.mark.parametrize("dataset", load_all_datasets(), ids=lambda item: item.name)
def test_dataset_replay_over_websocket(dataset, caplog) -> None:
    application = XiaoAiApplication(
        engine_factory=lambda: None,  # type: ignore[arg-type]
        interrupter_factory=lambda _context: None,  # type: ignore[arg-type]
        enabled=False,
    )
    client = TestClient(create_app(application))

    with caplog.at_level(logging.INFO):
        with client.websocket_connect("/ws") as websocket:
            for raw_payload in dataset.frames:
                websocket.send_text(raw_payload)

    ingress_records = [record for record in caplog.records if record.msg == "socket.ingress.raw"]
    parsed_records = [
        record
        for record in caplog.records
        if record.msg.startswith("socket.message.")
    ]

    assert len(ingress_records) == len(dataset.frames)
    assert len(parsed_records) == len(dataset.frames)

    degraded_count = sum(1 for record in parsed_records if getattr(record, "status", "-") == "degraded")
    assert degraded_count == dataset.expected.degraded_count
