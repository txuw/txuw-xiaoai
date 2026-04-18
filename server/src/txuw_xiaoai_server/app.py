from __future__ import annotations

import asyncio
import contextlib
import logging
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette import status

from .protocol import InboundMessage, InboundStream, parse_stream_frame, parse_text_message
from .socket_logging import (
    build_ingress_binary_log_entry,
    build_ingress_text_log_entry,
    build_socket_log_entry,
)
from .config import settings
from .protocol.models import InboundResponse
from .transport import ClientSessionTransport
from .xiaoai_handlers import ConnectionContext, DashScopeStreamingTtsConfig, XiaoAiApplication
from .xiaoai_handlers.services.dashscope_streaming_tts import DashScopeStreamingTtsEngine
from .xiaoai_handlers.services.legacy_audio_interrupt import AbortLegacyXiaoaiInterrupter


logger = logging.getLogger(__name__)


def create_app(application: XiaoAiApplication | None = None) -> FastAPI:
    """创建服务入口，并把 websocket 主链路集中在同一处。"""

    app = FastAPI(title="txuw-xiaoai-server", version="0.1.0")
    app_application = application or _build_application()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        connection_id = str(uuid4())
        transport = ClientSessionTransport(
            websocket,
            connection_id,
            play_config={
                "pcm": "noop",
                "channels": settings.tts_channels,
                "bits_per_sample": settings.tts_bits_per_sample,
                "sample_rate": settings.tts_sample_rate,
                "period_size": settings.tts_period_size,
                "buffer_size": settings.tts_buffer_size,
            },
        )
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
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
        processor = asyncio.create_task(
            _process_inbound_messages(queue, connection_id, app_application, transport)
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

                # 文本帧承载事件/指令/响应，是主业务协议入口。
                if text := message.get("text"):
                    ingress_entry = build_ingress_text_log_entry(text, connection_id)
                    logger.info(ingress_entry.event_name, extra=ingress_entry.to_logger_extra())
                    parsed = parse_text_message(text)

                    # response 只用于 transport 内部请求配对，不进入业务分发。
                    if isinstance(parsed, InboundResponse):
                        transport.accept_response(parsed)
                    else:
                        await queue.put(("text", parsed))
                    continue

                # 二进制帧当前只承载音频流，解析后走同一条异步消费链路。
                if data := message.get("bytes"):
                    ingress_entry = build_ingress_binary_log_entry(
                        _truncate_binary_preview(data, 160),
                        len(data),
                        connection_id,
                    )
                    logger.info(ingress_entry.event_name, extra=ingress_entry.to_logger_extra())
                    frame = parse_stream_frame(data)
                    await queue.put(("binary", frame))
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
        finally:
            processor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await processor

            # 断连时先清理业务会话，再终止挂起 RPC，最后回收播放资源。
            context = ConnectionContext(
                connection_id=connection_id,
                playback_port=transport,
                audio_sink=transport,
            )
            await app_application.on_disconnect(context)
            await transport.fail_pending("websocket disconnected")
            with contextlib.suppress(Exception):
                await transport.stop()

    return app


def _truncate_text(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[:limit]}..."


def _truncate_binary_preview(data: bytes, limit: int) -> str:
    text = data.decode("utf-8", errors="replace")
    return _truncate_text(text, limit)


async def _process_inbound_messages(
    queue: asyncio.Queue[tuple[str, object]],
    connection_id: str,
    application: XiaoAiApplication,
    transport: ClientSessionTransport,
) -> None:
    """串行消费已解析消息，保证日志与业务处理顺序稳定。"""

    while True:
        frame_type, payload = await queue.get()
        try:
            await _dispatch_inbound_payload(
                payload=payload,
                frame_type=frame_type,
                connection_id=connection_id,
                application=application,
                transport=transport,
            )
        finally:
            queue.task_done()


async def _dispatch_inbound_payload(
    *,
    payload: InboundMessage | InboundStream,
    frame_type: str,
    connection_id: str,
    application: XiaoAiApplication,
    transport: ClientSessionTransport,
) -> None:
    """记录解析后的结构化日志，然后把消息交给业务应用层。"""

    entry = build_socket_log_entry(payload, connection_id, frame_type=frame_type)
    logger.info(entry.event_name, extra=entry.to_logger_extra())

    context = ConnectionContext(
        connection_id=connection_id,
        playback_port=transport,
        audio_sink=transport,
    )
    await application.handle_inbound(payload, context)


def _build_application() -> XiaoAiApplication:
    enabled = settings.tts_intercept_enabled and bool(settings.dashscope_api_key)
    logger.info(
        "tts.replacement.bootstrap",
        extra={
            "ttsInterceptEnabled": settings.tts_intercept_enabled,
            "dashscopeApiKeyConfigured": bool(settings.dashscope_api_key),
            "status": "enabled" if enabled else "disabled",
            "summary": (
                "tts replacement enabled"
                if enabled
                else "tts replacement disabled: check TTS_INTERCEPT_ENABLED and DASHSCOPE_API_KEY"
            ),
        },
    )
    tts_config = DashScopeStreamingTtsConfig(
        api_key=settings.dashscope_api_key,
        model=settings.dashscope_tts_model,
        voice=settings.dashscope_tts_voice,
    )

    return XiaoAiApplication(
        engine_factory=lambda: DashScopeStreamingTtsEngine(tts_config),
        interrupter_factory=lambda context: AbortLegacyXiaoaiInterrupter(
            context.playback_port,
            enabled=settings.legacy_interrupt_enabled,
            command=settings.legacy_interrupt_command,
        ),
        enabled=enabled,
    )
