from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import httpx
from starlette import status

from .config import settings
from .protocol import InboundMessage, InboundStream, parse_stream_frame, parse_text_message
from .protocol.models import InboundResponse
from .socket_logging import (
    build_ingress_binary_log_entry,
    build_ingress_text_log_entry,
    build_socket_log_entry,
)
from .transport import ClientSessionTransport
from txuw_xiaoai_server.xiaoai_handlers.memory import MemoryProvider, MemoryProviderConfig
from .xiaoai_handlers import ConnectionContext, DashScopeStreamingTtsConfig, XiaoAiApplication
from .xiaoai_handlers.agent import (
    AmapIpRegionProvider,
    AgentStreamConfig,
    AgentStreamService,
    AgentToolsetFactory,
    RegionLookupService,
)
from .xiaoai_handlers.services.dashscope_streaming_tts import DashScopeStreamingTtsEngine
from .xiaoai_handlers.services.legacy_audio_interrupt import AbortLegacyXiaoaiInterrupter


logger = logging.getLogger(__name__)


def create_app(application: XiaoAiApplication | None = None) -> FastAPI:
    """创建服务入口，并把 WebSocket 主链路集中在同一处。"""

    app_application = application or _build_application()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            await app_application.startup()
            yield
        finally:
            with contextlib.suppress(Exception):
                await app_application.close()

    app = FastAPI(title="txuw-xiaoai-server", version="0.1.0", lifespan=lifespan)

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

                if text := message.get("text"):
                    ingress_entry = build_ingress_text_log_entry(text, connection_id)
                    logger.info(ingress_entry.event_name, extra=ingress_entry.to_logger_extra())
                    parsed = parse_text_message(text)

                    if isinstance(parsed, InboundResponse):
                        transport.accept_response(parsed)
                    else:
                        await queue.put(("text", parsed))
                    continue

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
    dashscope_configured = bool(settings.dashscope_api_key)
    legacy_enabled = settings.tts_intercept_enabled and dashscope_configured
    agent_enabled = (
        settings.llm_proxy_enabled
        and dashscope_configured
        and bool(settings.llm_api_key)
    )

    logger.info(
        "tts.replacement.bootstrap",
        extra={
            "ttsInterceptEnabled": settings.tts_intercept_enabled,
            "dashscopeApiKeyConfigured": dashscope_configured,
            "status": "enabled" if legacy_enabled else "disabled",
            "summary": (
                "tts replacement enabled"
                if legacy_enabled
                else "tts replacement disabled: check TTS_INTERCEPT_ENABLED and DASHSCOPE_API_KEY"
            ),
        },
    )
    logger.info(
        "agent.output.bootstrap",
        extra={
            "llmProxyEnabled": settings.llm_proxy_enabled,
            "llmApiKeyConfigured": bool(settings.llm_api_key),
            "llmBaseUrlConfigured": bool(settings.llm_base_url),
            "dashscopeApiKeyConfigured": dashscope_configured,
            "status": "enabled" if agent_enabled else "disabled",
            "summary": (
                "agent output enabled"
                if agent_enabled
                else "agent output disabled: check LLM_PROXY_ENABLED, LLM_API_KEY and DASHSCOPE_API_KEY"
            ),
        },
    )

    tts_config = DashScopeStreamingTtsConfig(
        api_key=settings.dashscope_api_key,
        model=settings.dashscope_tts_model,
        voice=settings.dashscope_tts_voice,
    )

    agent_service = None
    if agent_enabled:
        region_service = RegionLookupService(
            AmapIpRegionProvider(
                httpx.AsyncClient(),
                key=settings.amap_web_service_key,
            ),
            cache_ttl_seconds=settings.region_tool_cache_ttl_seconds,
        )
        agent_service = AgentStreamService(
            AgentStreamConfig(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                timeout_seconds=settings.llm_timeout_seconds,
                system_prompt=settings.llm_system_prompt,
            ),
            region_service=region_service,
            toolset_factory=AgentToolsetFactory(
                amap_web_service_key=settings.amap_web_service_key,
                region_tool_enabled=settings.region_tool_enabled,
                region_tool_timeout_seconds=settings.region_tool_timeout_seconds,
            ),
        )

    memory_provider = MemoryProvider(
        MemoryProviderConfig(
            enabled=settings.memory_enabled,
            user_id=settings.memory_user_id,
            llm_model=settings.memory_llm_model,
            embedding_model=settings.memory_embedding_model,
            milvus_url=settings.memory_milvus_url,
            milvus_token=settings.memory_milvus_token,
            milvus_db_name=settings.memory_milvus_db_name,
            milvus_collection_name=settings.memory_milvus_collection_name,
            recall_max_results=settings.memory_recall_max_results,
            recall_min_score=settings.memory_recall_min_score,
            commit_queue_maxsize=settings.memory_commit_queue_maxsize,
            commit_worker_count=settings.memory_commit_worker_count,
            timeout_seconds=settings.llm_timeout_seconds,
        ),
        llm_api_key=settings.llm_api_key,
        llm_base_url=settings.llm_base_url,
    )

    return XiaoAiApplication(
        engine_factory=lambda: DashScopeStreamingTtsEngine(tts_config),
        interrupter_factory=lambda context: AbortLegacyXiaoaiInterrupter(
            context.playback_port,
            enabled=settings.legacy_interrupt_enabled,
            command=settings.legacy_interrupt_command,
        ),
        enabled=legacy_enabled,
        agent_service=agent_service,
        agent_enabled=agent_enabled,
        memory_provider=memory_provider,
    )
