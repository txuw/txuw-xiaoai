from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from ..agent import AgentStreamService
from ..ports import ConnectionContext, LegacyAudioInterrupter, StreamingTtsEngine
from ..sessions import DialogSessionState, DialogSessionStore


logger = logging.getLogger(__name__)


class TtsReplacementCoordinator:
    """协调旧小爱打断、Agent 输出与新 TTS 播放。"""

    def __init__(
        self,
        session_store: DialogSessionStore,
        *,
        engine_factory: Callable[[], StreamingTtsEngine],
        enabled: bool,
        agent_service: AgentStreamService | None = None,
        agent_enabled: bool = False,
    ) -> None:
        self._session_store = session_store
        self._engine_factory = engine_factory
        self._enabled = enabled
        self._agent_service = agent_service
        self._agent_enabled = agent_enabled and agent_service is not None

    async def close(self) -> None:
        if self._agent_service is None:
            return
        await self._agent_service.close()

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
        if not self._agent_enabled:
            logger.info(
                "agent.stream.skipped",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "instructionName": instruction_name,
                    "source": "query",
                    "status": "disabled",
                    "summary": "agent output disabled",
                },
            )
            return

        await self._start_agent_dialog(
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

        if not self._agent_enabled:
            logger.info(
                "agent.stream.skipped",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "instructionName": instruction_name,
                    "source": "asr",
                    "status": "disabled",
                    "summary": "agent output disabled",
                },
            )
            return

        await self._start_agent_dialog(
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
        await self._push_session_text(
            context=context,
            dialog_id=dialog_id,
            session=session,
            instruction_name=instruction_name,
            source="legacy",
            text=text,
        )

    async def speak_text(
        self,
        context: ConnectionContext,
        interrupter: LegacyAudioInterrupter,
        *,
        dialog_id: str,
        instruction_name: str,
        source: str,
        text: str,
    ) -> None:
        if not text or not text.strip():
            return

        if not (self._enabled or self._agent_enabled):
            logger.info(
                "tts.text.skipped",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "instructionName": instruction_name,
                    "source": source,
                    "status": "disabled",
                    "summary": "server proactive tts disabled",
                },
            )
            return

        session = self._session_store.get_or_create(context.connection_id, dialog_id)
        if session.sealed:
            logger.info(
                "tts.text.skipped",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "instructionName": instruction_name,
                    "source": source,
                    "status": "sealed",
                    "summary": "dialog already sealed",
                },
            )
            return

        await self.prime_dialog(
            context,
            interrupter,
            dialog_id=dialog_id,
            instruction_name=instruction_name,
        )

        forwarded = await self._push_session_text(
            context=context,
            dialog_id=dialog_id,
            session=session,
            instruction_name=instruction_name,
            source=source,
            text=text,
            mark_server_owned_after_push=True,
        )
        if not forwarded:
            return

        # v1 的主动播报主要用于在同一 dialog 中插入 Tool 前置提示，
        # 播报后通常还会继续有 Agent 文本进入，所以这里不自动 complete 当前 TTS 会话。

    async def prime_dialog(
        self,
        context: ConnectionContext,
        interrupter: LegacyAudioInterrupter,
        *,
        dialog_id: str,
        instruction_name: str,
    ) -> None:
        if not (self._enabled or self._agent_enabled):
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
        if session is None or session.engine is None:
            return
        async with session.tts_write_lock:
            if session.sealed or session.engine is None:
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

    async def _start_agent_dialog(
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
        if session.server_owned or session.agent_task is not None:
            return

        session.agent_full_text = ""
        self._mark_server_owned(
            context=context,
            dialog_id=dialog_id,
            session=session,
            instruction_name=instruction_name,
            source=source,
        )

        await self.prime_dialog(
            context,
            interrupter,
            dialog_id=dialog_id,
            instruction_name=instruction_name,
        )
        await self._ensure_tts_session_started(context, dialog_id, session)

        task = asyncio.create_task(
            self._run_agent_stream(
                context=context,
                dialog_id=dialog_id,
                instruction_name=instruction_name,
                prompt=prompt,
                source=source,
            )
        )
        session.agent_task = task

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

        agent_task = session.agent_task
        if agent_task is not None and not agent_task.done():
            # 先取消 Agent 任务，再关闭 TTS，会更容易保证不会有晚到的文本继续写入已关闭的音频会话。
            agent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await agent_task

        if session.engine is not None:
            async with session.tts_write_lock:
                session.sealed = True
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

    async def _run_agent_stream(
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
            "agent.stream.started",
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
            assert self._agent_service is not None
            async for delta in self._agent_service.stream_text(prompt):
                if not delta or not delta.strip():
                    continue

                session = self._session_store.get(context.connection_id, dialog_id)
                if session is None:
                    return

                session.agent_full_text += delta
                forwarded = await self._push_session_text(
                    context=context,
                    dialog_id=dialog_id,
                    session=session,
                    instruction_name=instruction_name,
                    source=source,
                    text=delta,
                )
                if not forwarded:
                    return
                summary = f"text={_truncate_text(delta)}"
                logger.info(
                    "agent.stream.delta",
                    extra={
                        "connectionId": context.connection_id,
                        "dialogId": dialog_id,
                        "instructionName": instruction_name,
                        "source": source,
                        "status": "streaming",
                        "summary": summary,
                    },
                )
            await self.complete(context.connection_id, dialog_id)
            session = self._session_store.get(context.connection_id, dialog_id)
            logger.info(
                "agent.stream.completed",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "fullText": _truncate_text(_get_agent_full_text(session), limit=500),
                    "instructionName": instruction_name,
                    "source": source,
                    "status": "completed",
                    "summary": "agent stream completed",
                },
            )
        except asyncio.CancelledError:
            session = self._session_store.get(context.connection_id, dialog_id)
            logger.info(
                "agent.stream.cancelled",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "fullText": _truncate_text(_get_agent_full_text(session), limit=500),
                    "instructionName": instruction_name,
                    "source": source,
                    "status": "cancelled",
                    "summary": "agent stream cancelled",
                },
            )
            raise
        except Exception:
            session = self._session_store.get(context.connection_id, dialog_id)
            logger.exception(
                "agent.stream.failed",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "fullText": _truncate_text(_get_agent_full_text(session), limit=500),
                    "instructionName": instruction_name,
                    "source": source,
                    "status": "error",
                    "summary": "agent stream failed",
                },
            )
            with contextlib.suppress(Exception):
                await self.complete(context.connection_id, dialog_id)
        finally:
            session = self._session_store.get(context.connection_id, dialog_id)
            if session is not None and session.agent_task is current_task:
                session.agent_task = None

    def _mark_server_owned(
        self,
        *,
        context: ConnectionContext,
        dialog_id: str,
        session: DialogSessionState,
        instruction_name: str,
        source: str,
    ) -> None:
        if session.server_owned:
            return

        session.server_owned = True
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

    async def _push_session_text(
        self,
        *,
        context: ConnectionContext,
        dialog_id: str,
        session: DialogSessionState,
        instruction_name: str,
        source: str,
        text: str,
        mark_server_owned_after_push: bool = False,
    ) -> bool:
        async with session.tts_write_lock:
            if session.sealed:
                logger.info(
                    "tts.text.skipped",
                    extra={
                        "connectionId": context.connection_id,
                        "dialogId": dialog_id,
                        "instructionName": instruction_name,
                        "source": source,
                        "status": "sealed",
                        "summary": "dialog already sealed",
                    },
                )
                return False

            await self._ensure_tts_session_started(context, dialog_id, session)
            assert session.engine is not None

            # 同一 dialog 的文本可能来自 Agent、旧链路转发和 Tool 前置提示，
            # 必须统一串行化 push_text，才能避免底层流式 TTS 会话乱序写入。
            await session.engine.push_text(text)

            if mark_server_owned_after_push:
                # 服务端一旦成功把文本写进当前 dialog，后续就必须屏蔽设备侧旧播报，
                # 否则旧链路 SpeakStream 和服务端主动提示会同时落到同一音频输出。
                self._mark_server_owned(
                    context=context,
                    dialog_id=dialog_id,
                    session=session,
                    instruction_name=instruction_name,
                    source=source,
                )

            logger.info(
                "tts.text.forwarded",
                extra={
                    "connectionId": context.connection_id,
                    "dialogId": dialog_id,
                    "instructionName": instruction_name,
                    "source": source,
                    "summary": f"text={_truncate_text(text)}",
                },
            )
            return True

    @staticmethod
    def _session_key(connection_id: str, dialog_id: str) -> str:
        return f"{connection_id}:{dialog_id}"


def _truncate_text(value: str, limit: int = 160) -> str:
    return value if len(value) <= limit else f"{value[:limit]}..."


def _get_agent_full_text(session: DialogSessionState | None) -> str:
    if session is None:
        return ""
    return session.agent_full_text
