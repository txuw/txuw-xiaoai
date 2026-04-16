from __future__ import annotations

import logging

from .protocol import InboundMessage, InboundStream
from .socket_logging import build_socket_log_entry


logger = logging.getLogger(__name__)


async def handle_text_message(
    message: InboundMessage,
    connection_id: str,
) -> None:
    entry = build_socket_log_entry(
        message,
        connection_id,
        frame_type="text",
    )
    logger.info(entry.event_name, extra=entry.to_logger_extra())


async def handle_stream_frame(
    frame: InboundStream,
    connection_id: str,
) -> None:
    entry = build_socket_log_entry(
        frame,
        connection_id,
        frame_type="binary",
    )
    logger.info(entry.event_name, extra=entry.to_logger_extra())
