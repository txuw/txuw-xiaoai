from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

import httpx

from txuw_xiaoai_server.config import KwsTakeoverRule, settings
from txuw_xiaoai_server.protocol import KwsEventMessage, PlayingEventMessage
from txuw_xiaoai_server.xiaoai_handlers.memory import MemoryProvider

from .agent import AgentStreamService
from .handlers.dialog_lifecycle import DialogLifecycleHandler
from .handlers.query import QueryHandler
from .handlers.speech_recognizer import SpeechRecognizerHandler
from .handlers.speech_synthesizer import SpeechSynthesizerHandler
from .ports import ConnectionContext, LegacyAudioInterrupter, StreamingTtsEngine
from .services.tts_replacement_coordinator import TtsReplacementCoordinator
from .sessions import DialogSessionStore


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingKwsDecision:
    """记录连接级别、尚未绑定到真实对话的 KWS 决策。"""

    keyword: str
    takeover: bool
    created_at: float


class XiaoAiApplication:
    """小爱播报拦截、KWS 接管与 Agent 输出的业务入口。"""

    def __init__(
        self,
        *,
        engine_factory: Callable[[], StreamingTtsEngine],
        interrupter_factory: Callable[[ConnectionContext], LegacyAudioInterrupter],
        enabled: bool,
        agent_service: AgentStreamService | None = None,
        agent_enabled: bool = False,
        memory_provider: MemoryProvider | None = None,
        kws_takeover_enabled: bool = False,
        kws_takeover_rules: list[KwsTakeoverRule] | None = None,
        kws_native_wake_command: str = """ubus call pnshelper event_notify '{"src":1,"event":0}'""",
        kws_pending_timeout_seconds: float = 8.0,
        kws_welcome_idle_timeout_seconds: float = 6.0,
        kws_audio_cache_dir: str = ".cache/kws_audio",
    ) -> None:
        session_store = DialogSessionStore()
        coordinator = TtsReplacementCoordinator(
            session_store,
            engine_factory=engine_factory,
            enabled=enabled,
            agent_service=agent_service,
            agent_enabled=agent_enabled,
            memory_provider=memory_provider,
            kws_takeover_enabled=kws_takeover_enabled,
        )
        self._coordinator = coordinator
        self._interrupter_factory = interrupter_factory
        self._memory_provider = memory_provider
        self._query_handlers: dict[str, QueryHandler] = {}
        self._speech_recognizer_handlers: dict[str, SpeechRecognizerHandler] = {}
        self._speech_synthesizer_handlers: dict[str, SpeechSynthesizerHandler] = {}
        self._dialog_lifecycle_handlers: dict[str, DialogLifecycleHandler] = {}
        self._kws_takeover_enabled = kws_takeover_enabled
        self._kws_takeover_rules = {
            rule.keyword: rule for rule in (kws_takeover_rules or [])
        }
        self._kws_native_wake_command = kws_native_wake_command
        self._kws_pending_timeout_seconds = kws_pending_timeout_seconds
        self._kws_welcome_idle_timeout_seconds = kws_welcome_idle_timeout_seconds
        self._kws_audio_cache_dir = Path(kws_audio_cache_dir)
        self._cached_welcome_audio_paths: dict[str, Path] = {}
        # KWS 先到、真实 dialog 后到，所以需要连接级的待绑定决策。
        self._pending_kws_decisions: dict[str, PendingKwsDecision] = {}
        # 欢迎音频播完后仍然依赖设备侧的 playing=Idle 事件来兜底收尾。
        self._playback_idle_events: dict[str, asyncio.Event] = {}
        # 每个连接只保留最新一条欢迎音频任务，保证“后来的 KWS 决策优先”。
        self._welcome_audio_tasks: dict[str, asyncio.Task[None]] = {}

    async def startup(self) -> None:
        await self._preload_welcome_audios()
        if self._memory_provider is None:
            return
        await self._memory_provider.startup()

    async def handle_inbound(self, message: Any, context: ConnectionContext) -> None:
        """应用层直接分发消息，避免额外中间层增加理解成本。"""

        if getattr(message, "message_type", None) == "response":
            return

        event_name = getattr(message, "event", None)
        if event_name == "playing":
            self._update_playback_state(message, context.connection_id)
            return

        if event_name == "kws":
            await self._handle_kws_event(message, context)
            return

        # 第一次看到真实 dialog_id 时，把最近一次 KWS 决策绑定进去。
        self._bind_pending_kws_decision(message, context.connection_id)

        if await self._query_handler(context).handle(message, context):
            return

        if await self._speech_recognizer_handler(context).handle(message, context):
            return

        if await self._speech_synthesizer_handler(context).handle(message, context):
            return

        await self._dialog_lifecycle_handler(context).handle(message, context)

    async def on_disconnect(self, context: ConnectionContext) -> None:
        self._query_handlers.pop(context.connection_id, None)
        self._speech_recognizer_handlers.pop(context.connection_id, None)
        self._speech_synthesizer_handlers.pop(context.connection_id, None)
        self._dialog_lifecycle_handlers.pop(context.connection_id, None)
        self._pending_kws_decisions.pop(context.connection_id, None)
        idle_event = self._playback_idle_events.pop(context.connection_id, None)
        if idle_event is not None:
            # 连接断开时主动释放等待，避免欢迎音频协程卡到超时。
            idle_event.set()
        await self._cancel_welcome_audio_task(context.connection_id)
        await self._coordinator.cleanup_connection(context.connection_id)

    async def speak_text(
        self,
        context: ConnectionContext,
        *,
        dialog_id: str,
        text: str,
        instruction_name: str = "ServerSpeak",
        source: str = "proactive",
    ) -> None:
        interrupter = self._interrupter_factory(context)
        await self._coordinator.speak_text(
            context,
            interrupter,
            dialog_id=dialog_id,
            instruction_name=instruction_name,
            source=source,
            text=text,
        )

    async def close(self) -> None:
        try:
            await self._cancel_all_welcome_audio_tasks()
            await self._coordinator.close()
        finally:
            if self._memory_provider is not None:
                await self._memory_provider.shutdown()

    def _query_handler(self, context: ConnectionContext) -> QueryHandler:
        handler = self._query_handlers.get(context.connection_id)
        if handler is None:
            interrupter = self._interrupter_factory(context)
            handler = QueryHandler(self._coordinator, interrupter)
            self._query_handlers[context.connection_id] = handler
        return handler

    def _speech_recognizer_handler(self, context: ConnectionContext) -> SpeechRecognizerHandler:
        handler = self._speech_recognizer_handlers.get(context.connection_id)
        if handler is None:
            interrupter = self._interrupter_factory(context)
            handler = SpeechRecognizerHandler(self._coordinator, interrupter)
            self._speech_recognizer_handlers[context.connection_id] = handler
        return handler

    def _speech_synthesizer_handler(self, context: ConnectionContext) -> SpeechSynthesizerHandler:
        handler = self._speech_synthesizer_handlers.get(context.connection_id)
        if handler is None:
            interrupter = self._interrupter_factory(context)
            handler = SpeechSynthesizerHandler(self._coordinator, interrupter)
            self._speech_synthesizer_handlers[context.connection_id] = handler
        return handler

    def _dialog_lifecycle_handler(self, context: ConnectionContext) -> DialogLifecycleHandler:
        handler = self._dialog_lifecycle_handlers.get(context.connection_id)
        if handler is None:
            interrupter = self._interrupter_factory(context)
            handler = DialogLifecycleHandler(self._coordinator, interrupter)
            self._dialog_lifecycle_handlers[context.connection_id] = handler
        return handler

    async def _handle_kws_event(self, message: Any, context: ConnectionContext) -> None:
        if not self._kws_takeover_enabled or not isinstance(message, KwsEventMessage):
            return

        if message.data is None or message.data.kind != "Keyword":
            return

        keyword = (message.data.keyword or "").strip()
        if not keyword:
            return

        rule = self._kws_takeover_rules.get(keyword)
        takeover = rule is not None

        # 先写入 pending 决策，再触发原生唤醒，这样设备很快回传真实 dialog 时不会丢绑定窗口。
        self._pending_kws_decisions[context.connection_id] = PendingKwsDecision(
            keyword=keyword,
            takeover=takeover,
            created_at=monotonic(),
        )
        await self._cancel_welcome_audio_task(context.connection_id)

        try:
            await self._run_native_wake_command(context, keyword, takeover)
        except Exception:
            logger.exception(
                "kws.native_wake.failed",
                extra={
                    "connectionId": context.connection_id,
                    "keyword": keyword,
                    "takeover": takeover,
                    "summary": "native wake command failed",
                },
            )

        if rule is not None:
            # 原生唤醒放前面保证响应及时，欢迎音频放后台避免堵住后续真实对话入口。
            self._start_welcome_audio_task(rule, message.id, context)

    def _start_welcome_audio_task(
        self,
        rule: KwsTakeoverRule,
        event_id: str,
        context: ConnectionContext,
    ) -> None:
        connection_id = context.connection_id
        task = asyncio.create_task(self._run_welcome_audio_task(rule, event_id, context))
        self._welcome_audio_tasks[connection_id] = task
        task.add_done_callback(
            lambda finished_task, connection_id=connection_id: self._finalize_welcome_audio_task(
                connection_id,
                finished_task,
            )
        )

    async def _run_welcome_audio_task(
        self,
        rule: KwsTakeoverRule,
        event_id: str,
        context: ConnectionContext,
    ) -> None:
        try:
            await self._play_welcome_audio(rule, event_id, context)
        except asyncio.CancelledError:
            logger.info(
                "kws.welcome_audio.cancelled",
                extra={
                    "connectionId": context.connection_id,
                    "keyword": rule.keyword,
                    "summary": "welcome audio task cancelled",
                },
            )
            raise
        except Exception:
            logger.exception(
                "kws.welcome_audio.failed",
                extra={
                    "connectionId": context.connection_id,
                    "keyword": rule.keyword,
                    "summary": "welcome audio playback failed",
                },
            )

    def _finalize_welcome_audio_task(
        self,
        connection_id: str,
        task: asyncio.Task[None],
    ) -> None:
        if self._welcome_audio_tasks.get(connection_id) is task:
            self._welcome_audio_tasks.pop(connection_id, None)

        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()

    async def _cancel_welcome_audio_task(self, connection_id: str) -> None:
        task = self._welcome_audio_tasks.pop(connection_id, None)
        if task is None:
            return

        if task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                task.exception()
            return

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _cancel_all_welcome_audio_tasks(self) -> None:
        for connection_id in list(self._welcome_audio_tasks):
            await self._cancel_welcome_audio_task(connection_id)

    async def _play_welcome_audio(
        self,
        rule: KwsTakeoverRule,
        event_id: str,
        context: ConnectionContext,
    ) -> None:
        pcm_bytes = self._load_cached_welcome_audio(rule.keyword)
        dialog_id = f"kws-welcome:{event_id}"
        idle_event = self._playback_idle_events.setdefault(context.connection_id, asyncio.Event())
        # 新一轮欢迎音频开始前先清空 Idle，避免复用到上一轮的完成信号。
        idle_event.clear()

        interrupter = self._interrupter_factory(context)
        try:
            await self._coordinator.play_audio_bytes(
                context,
                interrupter,
                dialog_id=dialog_id,
                instruction_name="KwsWelcomeAudio",
                source="kws_welcome",
                pcm_bytes=pcm_bytes,
                interrupt_legacy=False,
            )
            await self._wait_for_playback_idle(context.connection_id, rule.keyword)
        finally:
            await self._coordinator.cleanup_dialog(context.connection_id, dialog_id)

    async def _wait_for_playback_idle(self, connection_id: str, keyword: str) -> None:
        idle_event = self._playback_idle_events.setdefault(connection_id, asyncio.Event())
        try:
            await asyncio.wait_for(
                idle_event.wait(),
                timeout=self._kws_welcome_idle_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "kws.welcome_audio.idle_timeout",
                extra={
                    "connectionId": connection_id,
                    "keyword": keyword,
                    "timeoutSeconds": self._kws_welcome_idle_timeout_seconds,
                    "summary": "welcome audio idle timeout",
                },
            )

    async def _run_native_wake_command(
        self,
        context: ConnectionContext,
        keyword: str,
        takeover: bool,
    ) -> None:
        await context.playback_port.run_shell(self._kws_native_wake_command)
        logger.info(
            "kws.native_wake.executed",
            extra={
                "connectionId": context.connection_id,
                "keyword": keyword,
                "takeover": takeover,
                "summary": "native wake command sent",
            },
        )

    def _update_playback_state(self, message: Any, connection_id: str) -> None:
        idle_event = self._playback_idle_events.setdefault(connection_id, asyncio.Event())
        if not isinstance(message, PlayingEventMessage) or message.data is None:
            idle_event.clear()
            return

        if message.data.state.value == "Idle":
            idle_event.set()
            return

        idle_event.clear()

    def _bind_pending_kws_decision(self, message: Any, connection_id: str) -> None:
        if not self._kws_takeover_enabled:
            return

        dialog_id = self._extract_dialog_id(message)
        if dialog_id is None:
            return

        pending = self._consume_pending_kws_decision(connection_id)
        keyword = "" if pending is None else pending.keyword
        takeover = False if pending is None else pending.takeover
        bound = self._coordinator.bind_wake_policy(
            connection_id,
            dialog_id,
            keyword=keyword,
            takeover=takeover,
        )
        if not bound:
            return

        if pending is None:
            # 小爱原生唤醒词通常不会经过自定义 KWS；这种情况下明确绑定为原生链路，
            # 避免后续最终 ASR 被误认为应该进入 Server 接管流程。
            logger.info(
                "kws.dialog.bound",
                extra={
                    "connectionId": connection_id,
                    "dialogId": dialog_id,
                    "keyword": "",
                    "takeover": False,
                    "status": "native",
                    "summary": "no pending kws decision; keep native dialog",
                },
            )
            return

        logger.info(
            "kws.dialog.bound",
            extra={
                "connectionId": connection_id,
                "dialogId": dialog_id,
                "keyword": pending.keyword,
                "takeover": pending.takeover,
                "status": "takeover" if pending.takeover else "native",
                "summary": "pending kws decision bound to dialog",
            },
        )

    def _consume_pending_kws_decision(self, connection_id: str) -> PendingKwsDecision | None:
        pending = self._pending_kws_decisions.pop(connection_id, None)
        if pending is None:
            return None

        age = monotonic() - pending.created_at
        if age <= self._kws_pending_timeout_seconds:
            return pending

        logger.info(
            "kws.pending.expired",
            extra={
                "connectionId": connection_id,
                "keyword": pending.keyword,
                "ageSeconds": round(age, 3),
                "timeoutSeconds": self._kws_pending_timeout_seconds,
                "summary": "pending kws decision expired",
            },
        )
        return None

    def _extract_dialog_id(self, message: Any) -> str | None:
        envelope = getattr(getattr(message, "data", None), "decoded_envelope", None)
        if envelope is None:
            return None

        dialog_id = getattr(envelope.header, "dialog_id", "")
        if isinstance(dialog_id, str) and dialog_id.strip():
            return dialog_id.strip()
        return None

    async def _preload_welcome_audios(self) -> None:
        if not self._kws_takeover_enabled or not self._kws_takeover_rules:
            return

        self._kws_audio_cache_dir.mkdir(parents=True, exist_ok=True)
        cached_by_url: dict[str, Path] = {}
        for rule in self._kws_takeover_rules.values():
            cache_path = cached_by_url.get(rule.welcome_audio_url)
            if cache_path is None:
                cache_path = self._cache_path_for_url(rule.welcome_audio_url)
                await self._download_and_validate_welcome_audio(rule, cache_path)
                cached_by_url[rule.welcome_audio_url] = cache_path

            self._cached_welcome_audio_paths[rule.keyword] = cache_path

    async def _download_and_validate_welcome_audio(
        self,
        rule: KwsTakeoverRule,
        cache_path: Path,
    ) -> None:
        try:
            audio_bytes = await self._download_welcome_audio_bytes(rule.welcome_audio_url)
            cache_path.write_bytes(audio_bytes)
            self._read_wav_pcm_bytes(cache_path, rule.keyword)
        except Exception as exc:
            cache_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"failed to prepare welcome audio for keyword={rule.keyword}"
            ) from exc

        logger.info(
            "kws.welcome_audio.cached",
            extra={
                "keyword": rule.keyword,
                "cachePath": str(cache_path),
                "summary": "welcome audio cached",
            },
        )

    async def _download_welcome_audio_bytes(self, url: str) -> bytes:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content

    def _load_cached_welcome_audio(self, keyword: str) -> bytes:
        cache_path = self._cached_welcome_audio_paths.get(keyword)
        if cache_path is None:
            raise RuntimeError(f"missing cached welcome audio for keyword={keyword}")
        return self._read_wav_pcm_bytes(cache_path, keyword)

    def _read_wav_pcm_bytes(self, path: Path, keyword: str) -> bytes:
        try:
            with wave.open(str(path), "rb") as wav_file:
                self._validate_wav_header(wav_file, keyword)
                pcm_bytes = wav_file.readframes(wav_file.getnframes())
        except Exception as exc:
            raise RuntimeError(
                f"invalid welcome audio file for keyword={keyword}"
            ) from exc

        if not pcm_bytes:
            raise RuntimeError(f"welcome audio is empty for keyword={keyword}")
        return pcm_bytes

    def _validate_wav_header(self, wav_file: wave.Wave_read, keyword: str) -> None:
        if wav_file.getcomptype() != "NONE":
            raise RuntimeError(f"welcome audio must be PCM for keyword={keyword}")
        if wav_file.getnchannels() != 1:
            raise RuntimeError(f"welcome audio must be mono for keyword={keyword}")
        if wav_file.getsampwidth() != 2:
            raise RuntimeError(f"welcome audio bit depth mismatch for keyword={keyword}")
        if wav_file.getframerate() != settings.tts_sample_rate:
            raise RuntimeError(f"welcome audio sample rate mismatch for keyword={keyword}")

    def _cache_path_for_url(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._kws_audio_cache_dir / f"{digest}.wav"
