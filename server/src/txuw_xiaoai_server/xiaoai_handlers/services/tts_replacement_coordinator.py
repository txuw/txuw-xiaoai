from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from ..ports import (
    ConnectionContext,
    LegacyAudioInterrupter,
    StreamingLlmClient,
    StreamingTtsEngine,
)
from ..sessions import DialogSessionState, DialogSessionStore


logger = logging.getLogger(__name__)


class TtsReplacementCoordinator:
    """协调旧小爱播报打断、LLM 回答与新 TTS 输出。"""

    def __init__(
        self,
        session_store: DialogSessionStore,
        *,
        engine_factory: Callable[[], StreamingTtsEngine],
        enabled: bool,
        llm_client: StreamingLlmClient | None = None,
        llm_enabled: bool = False,
    ) -> None:
        self._session_store = session_store
        self._engine_factory = engine_factory
        self._enabled = enabled
        self._llm_client = llm_client
        self._llm_enabled = llm_enabled and llm_client is not None

    async def close(self) -> None:
        if self._llm_client is None:
            return
        await self._llm_client.close()

    def is_server_owned(self, connection_id: str, dialog_id: str) -> bool:
        session = self._session_store.get(connection_id, dialog_id)
        return bool(session and session.server_owned)

    async def on_query(
        self,
        context: ConnectionContext,
        interrupter: LegacyAudioInterrupter,
        *,
        dialog_id: str,
        instruction_name: str,
        text: str,
    ) -> None:
        session = self._session_store.get_or_create(context.connection_id, dialog_id)
        session.query_text = text

        if not text:
            return
        if not self._llm_enabled:
            logger.info(
                "llm.stream.skipped",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "instructionName": instruction_name,
                    "source": "query",
                    "status": "disabled",
                    "summary": "llm proxy disabled",
                },
            )
            return

        await self._start_llm_dialog(
            context,
            interrupter,
            dialog_id=dialog_id,
            instruction_name=instruction_name,
            prompt=text,
            source="query",
        )

    async def on_recognize_result(
        self,
        context: ConnectionContext,
        interrupter: LegacyAudioInterrupter,
        *,
        dialog_id: str,
        instruction_name: str,
        text: str,
        is_final: bool,
    ) -> None:
        if not is_final or not text:
            return

        session = self._session_store.get_or_create(context.connection_id, dialog_id)
        session.final_asr_text = text

        if not self._llm_enabled:
            logger.info(
                "llm.stream.skipped",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "instructionName": instruction_name,
                    "source": "asr",
                    "status": "disabled",
                    "summary": "llm proxy disabled",
                },
            )
            return

        await self._start_llm_dialog(
            context,
            interrupter,
            dialog_id=dialog_id,
            instruction_name=instruction_name,
            prompt=text,
            source="asr",
        )

    async def on_text(
        self,
        context: ConnectionContext,
        interrupter: LegacyAudioInterrupter,
        *,
        dialog_id: str,
        instruction_name: str,
        text: str,
    ) -> None:
        if not self._enabled:
            if text:
                logger.info(
                    "tts.replacement.skipped",
                    extra={
                        "connectionId": context.connection_id,
                        "dialogId": dialog_id,
                        "instructionName": instruction_name,
                        "status": "disabled",
                        "summary": "tts replacement disabled; using legacy xiaoai voice",
                    },
                )
            return

        await self.prime_dialog(
            context,
            interrupter,
            dialog_id=dialog_id,
            instruction_name=instruction_name,
        )

        if not text:
            return

        session = self._session_store.get_or_create(context.connection_id, dialog_id)
        await self._ensure_tts_session_started(context, dialog_id, session)

        assert session.engine is not None
        await session.engine.push_text(text)
        logger.info(
            "tts.text.forwarded",
            extra={
                "connectionId": context.connection_id,
                "dialogId": dialog_id,
                "instructionName": instruction_name,
                "source": "legacy",
                "summary": f"text={_truncate_text(text)}",
            },
        )

    async def prime_dialog(
        self,
        context: ConnectionContext,
        interrupter: LegacyAudioInterrupter,
        *,
        dialog_id: str,
        instruction_name: str,
    ) -> None:
        if not (self._enabled or self._llm_enabled):
            return

        session = self._session_store.get_or_create(context.connection_id, dialog_id)
        if session.legacy_interrupted:
            return

        task = session.legacy_interrupt_task
        if task is None:
            session.legacy_interrupt_task = asyncio.create_task(
                self._run_interrupt_task(
                    context=context,
                    interrupter=interrupter,
                    dialog_id=dialog_id,
                    instruction_name=instruction_name,
                    session=session,
                )
            )
            logger.info(
                "tts.replacement.prime_requested",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "instructionName": instruction_name,
                    "status": "pending",
                    "summary": "legacy xiaoai playback interrupt requested",
                },
            )
            await asyncio.sleep(0)
            return

        if task.done():
            with contextlib.suppress(Exception):
                session.legacy_interrupted = task.result()
            session.legacy_interrupt_task = None

    async def complete(self, connection_id: str, dialog_id: str) -> None:
        session = self._session_store.get(connection_id, dialog_id)
        if session is None or session.sealed or session.engine is None:
            return
        await session.engine.complete()
        session.sealed = True

    async def cleanup_dialog(self, connection_id: str, dialog_id: str) -> None:
        session = self._session_store.remove(connection_id, dialog_id)
        if session is None:
            return
        await self._close_session(session)

    async def cleanup_connection(self, connection_id: str) -> None:
        sessions = self._session_store.pop_connection(connection_id)
        for session in sessions:
            await self._close_session(session)

    async def _start_llm_dialog(
        self,
        context: ConnectionContext,
        interrupter: LegacyAudioInterrupter,
        *,
        dialog_id: str,
        instruction_name: str,
        prompt: str,
        source: str,
    ) -> None:
        session = self._session_store.get_or_create(context.connection_id, dialog_id)
        if session.server_owned or session.llm_task is not None:
            return

        session.server_owned = True
        session.llm_full_text = ""
        logger.info(
            "dialog.server_owned",
            extra={
                "connectionId": context.connection_id,
                "dialogId": dialog_id,
                "instructionName": instruction_name,
                "source": source,
                "status": "ok",
                "summary": f"server owned dialog source={source}",
            },
        )

        await self.prime_dialog(
            context,
            interrupter,
            dialog_id=dialog_id,
            instruction_name=instruction_name,
        )
        await self._ensure_tts_session_started(context, dialog_id, session)

        task = asyncio.create_task(
            self._run_llm_stream(
                context=context,
                dialog_id=dialog_id,
                instruction_name=instruction_name,
                prompt=prompt,
                source=source,
            )
        )
        session.llm_task = task

    async def _ensure_tts_session_started(
        self,
        context: ConnectionContext,
        dialog_id: str,
        session: DialogSessionState,
    ) -> None:
        if not session.tts_started:
            engine = self._engine_factory()
            await engine.start(self._session_key(context.connection_id, dialog_id), context.audio_sink)
            session.engine = engine
            session.tts_started = True

        if not session.playback_started:
            await context.playback_port.ensure_started()
            session.playback_started = True

    async def _close_session(self, session: DialogSessionState) -> None:
        task = session.legacy_interrupt_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        llm_task = session.llm_task
        if llm_task is not None and not llm_task.done():
            llm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await llm_task

        if session.engine is not None:
            await session.engine.close()

    async def _run_interrupt_task(
        self,
        *,
        context: ConnectionContext,
        interrupter: LegacyAudioInterrupter,
        dialog_id: str,
        instruction_name: str,
        session: DialogSessionState,
    ) -> bool:
        try:
            interrupted = await interrupter.interrupt(context.connection_id, dialog_id)
        except Exception:
            logger.exception(
                "tts.replacement.prime_failed",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "instructionName": instruction_name,
                    "summary": "legacy xiaoai playback interrupt failed",
                },
            )
            raise

        session.legacy_interrupted = interrupted
        logger.info(
            "tts.replacement.primed",
            extra={
                "connectionId": context.connection_id,
                "dialogId": dialog_id,
                "instructionName": instruction_name,
                "status": "ok" if interrupted else "skipped",
                "summary": (
                    "legacy xiaoai playback interrupted"
                    if interrupted
                    else "legacy xiaoai playback interrupt skipped"
                ),
            },
        )
        return interrupted

    async def _run_llm_stream(
        self,
        *,
        context: ConnectionContext,
        dialog_id: str,
        instruction_name: str,
        prompt: str,
        source: str,
    ) -> None:
        current_task = asyncio.current_task()
        logger.info(
            "llm.stream.started",
            extra={
                "connectionId": context.connection_id,
                "dialogId": dialog_id,
                "instructionName": instruction_name,
                "source": source,
                "status": "started",
                "summary": f"prompt={_truncate_text(prompt)}",
            },
        )

        try:
            assert self._llm_client is not None
            session = self._session_store.get(context.connection_id, dialog_id)
            async for delta in self._llm_client.stream_text(prompt):
                if not delta or not delta.strip():
                    continue

                session = self._session_store.get(context.connection_id, dialog_id)
                if session is None or session.engine is None:
                    return

                session.llm_full_text += delta
                await session.engine.push_text(delta)
                summary = f"text={_truncate_text(delta)}"
                logger.info(
                    "llm.stream.delta",
                    extra={
                        "connectionId": context.connection_id,
                        "dialogId": dialog_id,
                        "instructionName": instruction_name,
                        "source": source,
                        "status": "streaming",
                        "summary": summary,
                    },
                )
                logger.info(
                    "tts.text.forwarded",
                    extra={
                        "connectionId": context.connection_id,
                        "dialogId": dialog_id,
                        "instructionName": instruction_name,
                        "source": source,
                        "summary": summary,
                    },
                )

            await self.complete(context.connection_id, dialog_id)
            session = self._session_store.get(context.connection_id, dialog_id)
            logger.info(
                "llm.stream.completed",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "fullText": _truncate_text(_get_llm_full_text(session), limit=500),
                    "instructionName": instruction_name,
                    "source": source,
                    "status": "completed",
                    "summary": "llm stream completed",
                },
            )
        except asyncio.CancelledError:
            session = self._session_store.get(context.connection_id, dialog_id)
            logger.info(
                "llm.stream.cancelled",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "fullText": _truncate_text(_get_llm_full_text(session), limit=500),
                    "instructionName": instruction_name,
                    "source": source,
                    "status": "cancelled",
                    "summary": "llm stream cancelled",
                },
            )
            raise
        except Exception:
            session = self._session_store.get(context.connection_id, dialog_id)
            logger.exception(
                "llm.stream.failed",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "fullText": _truncate_text(_get_llm_full_text(session), limit=500),
                    "instructionName": instruction_name,
                    "source": source,
                    "status": "error",
                    "summary": "llm stream failed",
                },
            )
            with contextlib.suppress(Exception):
                await self.complete(context.connection_id, dialog_id)
        finally:
            session = self._session_store.get(context.connection_id, dialog_id)
            if session is not None and session.llm_task is current_task:
                session.llm_task = None

    @staticmethod
    def _session_key(connection_id: str, dialog_id: str) -> str:
        return f"{connection_id}:{dialog_id}"


def _truncate_text(value: str, limit: int = 160) -> str:
    return value if len(value) <= limit else f"{value[:limit]}..."


def _get_llm_full_text(session: DialogSessionState | None) -> str:
    if session is None:
        return ""
    return session.llm_full_text
