from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from ..ports import ConnectionContext, LegacyAudioInterrupter, StreamingTtsEngine
from ..sessions import DialogSessionState, DialogSessionStore


logger = logging.getLogger(__name__)


class TtsReplacementCoordinator:
    """协调旧小爱播报打断与新 TTS 替换。"""

    def __init__(
        self,
        session_store: DialogSessionStore,
        *,
        engine_factory: Callable[[], StreamingTtsEngine],
        enabled: bool,
    ) -> None:
        self._session_store = session_store
        self._engine_factory = engine_factory
        self._enabled = enabled

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
        if not session.tts_started:
            engine = self._engine_factory()
            await engine.start(self._session_key(context.connection_id, dialog_id), context.audio_sink)
            session.engine = engine
            session.tts_started = True
            await context.playback_port.ensure_started()

        assert session.engine is not None
        await session.engine.push_text(text)
        logger.info(
            "tts.text.forwarded",
            extra={
                "connectionId": context.connection_id,
                "dialogId": dialog_id,
                "instructionName": instruction_name,
                "summary": f"text={text}",
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
        """尽早异步触发旧小爱打断，不阻塞后续 TTS 文本处理。"""

        if not self._enabled:
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
            # 主动让出一次调度，让中断任务尽快把远端 run_shell 请求发出去，
            # 但不等待其最终响应，避免阻塞后续 SpeakStream 文本处理。
            await asyncio.sleep(0)
            return

        if task.done():
            with contextlib.suppress(Exception):
                session.legacy_interrupted = task.result()
            session.legacy_interrupt_task = None

    async def complete(self, connection_id: str, dialog_id: str) -> None:
        """声明文本输入结束，让实时 TTS 完成收尾。"""

        session = self._session_store.get(connection_id, dialog_id)
        if session is None or session.sealed or session.engine is None:
            return
        await session.engine.complete()
        session.sealed = True

    async def cleanup_dialog(self, connection_id: str, dialog_id: str) -> None:
        """清理单个对话会话，并关闭可能残留的 TTS 引擎。"""

        session = self._session_store.remove(connection_id, dialog_id)
        if session is None:
            return
        await self._close_session(session)

    async def cleanup_connection(self, connection_id: str) -> None:
        """连接断开时回收该连接下的全部 TTS 会话。"""

        sessions = self._session_store.pop_connection(connection_id)
        for session in sessions:
            await self._close_session(session)

    async def _close_session(self, session: DialogSessionState) -> None:
        task = session.legacy_interrupt_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

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

    @staticmethod
    def _session_key(connection_id: str, dialog_id: str) -> str:
        return f"{connection_id}:{dialog_id}"
