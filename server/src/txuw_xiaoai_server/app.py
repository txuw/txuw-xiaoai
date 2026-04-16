from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette import status

from .handlers import handle_stream_frame, handle_text_message
from .protocol import parse_stream_frame, parse_text_message
from .socket_logging import (
    build_ingress_binary_log_entry,
    build_ingress_text_log_entry,
)


logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="txuw-xiaoai-server", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        connection_id = str(uuid4())
        await websocket.accept()
        logger.info(
            "socket.connected",
            extra={
                "connectionId": connection_id,
                "direction": "inbound",
                "status": "connected",
                "summary": "websocket connected",
            },
        )

        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    logger.info(
                        "socket.disconnected",
                        extra={
                            "connectionId": connection_id,
                            "direction": "inbound",
                            "status": "disconnected",
                            "summary": "websocket disconnected",
                        },
                    )
                    break

                if text := message.get("text"):
                    ingress_entry = build_ingress_text_log_entry(text, connection_id)
                    logger.info(ingress_entry.event_name, extra=ingress_entry.to_logger_extra())
                    parsed = parse_text_message(text)
                    await handle_text_message(parsed, connection_id)
                    continue

                if data := message.get("bytes"):
                    ingress_entry = build_ingress_binary_log_entry(
                        _truncate_binary_preview(data, 160),
                        len(data),
                        connection_id,
                    )
                    logger.info(ingress_entry.event_name, extra=ingress_entry.to_logger_extra())
                    frame = parse_stream_frame(data)
                    await handle_stream_frame(frame, connection_id)
        except WebSocketDisconnect:
            logger.info(
                "socket.disconnected",
                extra={
                    "connectionId": connection_id,
                    "direction": "inbound",
                    "status": "disconnected",
                    "summary": "websocket disconnected",
                },
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "socket.closed_invalid",
                extra={
                    "connectionId": connection_id,
                    "direction": "inbound",
                    "errorType": type(exc).__name__,
                    "status": "error",
                    "summary": "invalid websocket payload",
                },
                exc_info=True,
            )
            await websocket.close(code=status.WS_1007_INVALID_FRAME_PAYLOAD_DATA)

    return app


def _truncate_text(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[:limit]}..."


def _truncate_binary_preview(data: bytes, limit: int) -> str:
    text = data.decode("utf-8", errors="replace")
    return _truncate_text(text, limit)
