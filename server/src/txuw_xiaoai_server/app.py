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

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        connection_id = str(uuid4())
        await websocket.accept()
        logger.info("websocket connected", extra={"connection_id": connection_id})

        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break

                if text := message.get("text"):
                    parsed = parse_text_message(text)
                    await handle_text_message(parsed, connection_id)
                    continue

                if data := message.get("bytes"):
                    frame = parse_stream_frame(data)
                    await handle_stream_frame(frame, connection_id)
        except WebSocketDisconnect:
            logger.info("websocket disconnected", extra={"connection_id": connection_id})
        except (ValidationError, ValueError, TypeError) as exc:
            logger.warning(
                "invalid websocket payload",
                extra={"connection_id": connection_id, "error": str(exc)},
            )
            await websocket.close(code=status.WS_1007_INVALID_FRAME_PAYLOAD_DATA)

    return app
