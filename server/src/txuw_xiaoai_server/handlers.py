from __future__ import annotations

import logging

from .protocol import ClientEventMessage, RequestMessage, ResponseMessage, StreamFrame


logger = logging.getLogger(__name__)


async def handle_text_message(
    message: RequestMessage | ResponseMessage | ClientEventMessage,
    connection_id: str,
) -> None:
    if isinstance(message, ClientEventMessage):
        logger.info(
            "received client event",
            extra={
                "connection_id": connection_id,
                "event": message.event,
                "message_id": message.id,
            },
        )
        return

    if isinstance(message, RequestMessage):
        logger.info(
            "received request",
            extra={
                "connection_id": connection_id,
                "command": message.Request.command,
                "message_id": message.Request.id,
            },
        )
        return

    logger.info(
        "received response",
        extra={
            "connection_id": connection_id,
            "message_id": message.Response.id,
            "code": message.Response.code,
        },
    )


async def handle_stream_frame(frame: StreamFrame, connection_id: str) -> None:
    logger.info(
        "received stream frame",
        extra={
            "connection_id": connection_id,
            "tag": frame.tag,
            "message_id": frame.id,
            "byte_length": len(frame.bytes),
        },
    )
