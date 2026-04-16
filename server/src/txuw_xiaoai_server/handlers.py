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
            "Received client event",
            extra={
                "connectionId": connection_id,
                "event": message.event,
                "messageId": message.id,
            },
        )
        return

    if isinstance(message, RequestMessage):
        logger.info(
            "Received request",
            extra={
                "connectionId": connection_id,
                "messageId": message.Request.id,
                "command": message.Request.command,
            },
        )
        return

    logger.info(
        "Received response",
        extra={
            "connectionId": connection_id,
            "messageId": message.Response.id,
        },
    )


async def handle_stream_frame(frame: StreamFrame, connection_id: str) -> None:
    logger.info(
        "Received stream frame",
        extra={
            "connectionId": connection_id,
            "tag": frame.tag,
            "messageId": frame.id,
            "byteLength": len(frame.bytes),
        },
    )
