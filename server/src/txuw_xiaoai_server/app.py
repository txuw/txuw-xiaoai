from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from starlette import status

from .handlers import handle_stream_frame, handle_text_message
from .protocol import parse_stream_frame, parse_text_message


logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="txuw-xiaoai-server", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        connection_id = str(uuid4())
        await websocket.accept()
        logger.info(
            "WebSocket connected",
            extra={"connectionId": connection_id, "status": "connected"},
        )

        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break

                if text := message.get("text"):
                    logger.debug(
                        "Received text frame",
                        extra={
                            "connectionId": connection_id,
                            "textPreview": text[:500],
                        },
                    )
                    parsed = parse_text_message(text)
                    await handle_text_message(parsed, connection_id)
                    continue

                if data := message.get("bytes"):
                    logger.debug(
                        "Received binary frame",
                        extra={
                            "connectionId": connection_id,
                            "byteLength": len(data),
                        },
                    )
                    frame = parse_stream_frame(data)
                    await handle_stream_frame(frame, connection_id)
        except WebSocketDisconnect:
            logger.info(
                "WebSocket disconnected",
                extra={"connectionId": connection_id, "status": "disconnected"},
            )
        except (ValidationError, ValueError, TypeError) as exc:
            logger.warning(
                "Invalid WebSocket payload",
                extra={
                    "connectionId": connection_id,
                    "errorType": type(exc).__name__,
                    "status": "error",
                },
                exc_info=True,
            )
            await websocket.close(code=status.WS_1007_INVALID_FRAME_PAYLOAD_DATA)

    return app
