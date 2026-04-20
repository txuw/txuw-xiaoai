from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from txuw_xiaoai_server.protocol import InboundResponse, ResponseBody, parse_text_message
from txuw_xiaoai_server.transport import ClientSessionTransport
from txuw_xiaoai_server.xiaoai_handlers import ConnectionContext, XiaoAiApplication
from txuw_xiaoai_server.xiaoai_handlers.memory import (
    MemoryRecallMetrics,
    MemorySearchResult,
)


class FakeTransport:
    def __init__(self) -> None:
        self.ensure_started_calls = 0
        self.stop_calls = 0
        self.run_shell_calls: list[str] = []
        self.audio_chunks: list[bytes] = []

    async def ensure_started(self) -> None:
        self.ensure_started_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1

    async def run_shell(self, script: str) -> str:
        self.run_shell_calls.append(script)
        return "{}"

    async def write(self, data: bytes) -> None:
        self.audio_chunks.append(data)


class FakeEngine:
    def __init__(self) -> None:
        self.started = False
        self.completed = 0
        self.closed = 0
        self.pushed_texts: list[str] = []
        self.sink = None

    async def start(self, session_id: str, sink) -> None:
        self.started = True
        self.sink = sink

    async def push_text(self, text: str) -> None:
        self.pushed_texts.append(text)
        await self.sink.write(text.encode("utf-8"))

    async def complete(self) -> None:
        self.completed += 1

    async def close(self) -> None:
        self.closed += 1


class FakeInterrupter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def interrupt(self, connection_id: str, dialog_id: str) -> bool:
        self.calls.append((connection_id, dialog_id))
        return True


class FakeAgentService:
    def __init__(self, responses: list[list[str]] | None = None) -> None:
        self._responses = list(responses or [])
        self.prompts: list[str] = []
        self.close_calls = 0

    def stream_text(self, prompt: str) -> AsyncIterator[str]:
        async def generator() -> AsyncIterator[str]:
            self.prompts.append(prompt)
            deltas = self._responses.pop(0) if self._responses else []
            for delta in deltas:
                await asyncio.sleep(0)
                yield delta

        return generator()

    async def close(self) -> None:
        self.close_calls += 1


class BlockingAgentService:
    def __init__(self, first_delta: str = "streaming") -> None:
        self.first_delta = first_delta
        self.prompts: list[str] = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    def stream_text(self, prompt: str) -> AsyncIterator[str]:
        async def generator() -> AsyncIterator[str]:
            self.prompts.append(prompt)
            self.started.set()
            yield self.first_delta
            try:
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

        return generator()

    async def close(self) -> None:
        return None


class FailingAgentService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or RuntimeError("agent failed")
        self.prompts: list[str] = []

    def stream_text(self, prompt: str) -> AsyncIterator[str]:
        async def generator() -> AsyncIterator[str]:
            self.prompts.append(prompt)
            raise self.error
            yield ""

        return generator()

    async def close(self) -> None:
        return None


class FakeMemoryCommitWorker:
    def __init__(self, *, raise_on_enqueue: Exception | None = None) -> None:
        self.raise_on_enqueue = raise_on_enqueue
        self.calls: list[dict[str, object]] = []

    async def enqueue(
        self,
        user_id: str,
        messages: list[dict[str, str]],
        idempotency_key: str,
    ) -> bool:
        if self.raise_on_enqueue is not None:
            raise self.raise_on_enqueue
        self.calls.append(
            {
                "user_id": user_id,
                "messages": messages,
                "idempotency_key": idempotency_key,
            }
        )
        return True


class FakeMemoryProvider:
    def __init__(
        self,
        *,
        enabled: bool = True,
        results: list[list[dict[str, object]]] | None = None,
        search_metrics: list[MemoryRecallMetrics] | None = None,
        search_error: Exception | None = None,
        commit_worker: FakeMemoryCommitWorker | None = None,
        user_id: str = "txuw",
    ) -> None:
        self._enabled = enabled
        self._results = list(results or [])
        self._search_metrics = list(search_metrics or [])
        self._search_error = search_error
        self.commit_worker = commit_worker
        self.user_id = user_id
        self.search_calls: list[dict[str, object]] = []
        self.startup_calls = 0
        self.shutdown_calls = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def startup(self) -> None:
        self.startup_calls += 1

    async def shutdown(self) -> None:
        self.shutdown_calls += 1

    async def search(self, query: str, *, user_id: str | None = None) -> list[dict[str, object]]:
        result = await self.search_with_metrics(query, user_id=user_id)
        return result.results

    async def search_with_metrics(
        self,
        query: str,
        *,
        user_id: str | None = None,
    ) -> MemorySearchResult:
        self.search_calls.append({"query": query, "user_id": user_id})
        if self._search_error is not None:
            raise self._search_error
        results = self._results.pop(0) if self._results else []
        metrics = (
            self._search_metrics.pop(0)
            if self._search_metrics
            else MemoryRecallMetrics(
                embedding_http_ms=11,
                milvus_search_ms=7,
                memory_recall_total_ms=23,
            )
        )
        return MemorySearchResult(results=results, metrics=metrics)


class FakeWebSocket:
    def __init__(self) -> None:
        self.text_messages: list[str] = []
        self.binary_messages: list[bytes] = []

    async def send_text(self, data: str) -> None:
        self.text_messages.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.binary_messages.append(data)


def _create_application(
    *,
    engine: FakeEngine,
    interrupter: FakeInterrupter,
    enabled: bool = True,
    agent_service: FakeAgentService | BlockingAgentService | FailingAgentService | None = None,
    agent_enabled: bool = False,
    memory_provider: FakeMemoryProvider | None = None,
) -> XiaoAiApplication:
    return XiaoAiApplication(
        engine_factory=lambda: engine,
        interrupter_factory=lambda _context: interrupter,
        enabled=enabled,
        agent_service=agent_service,
        agent_enabled=agent_enabled,
        memory_provider=memory_provider,
    )


def _context(connection_id: str, transport: FakeTransport) -> ConnectionContext:
    return ConnectionContext(
        connection_id=connection_id,
        playback_port=transport,
        audio_sink=transport,
    )


def _instruction_message(
    name: str,
    *,
    namespace: str,
    payload: dict[str, Any] | None = None,
    dialog_id: str = "dialog-1",
):
    return parse_text_message(
        json.dumps(
            {
                "Event": {
                    "id": f"{name}-id",
                    "event": "instruction",
                    "data": {
                        "NewLine": json.dumps(
                            {
                                "header": {
                                    "dialog_id": dialog_id,
                                    "id": f"{name}-inner-id",
                                    "name": name,
                                    "namespace": namespace,
                                },
                                "payload": payload or {},
                            }
                        )
                    },
                }
            }
        )
    )


def _speak_message(name: str, text: str, dialog_id: str = "dialog-1"):
    return _instruction_message(
        name,
        namespace="SpeechSynthesizer",
        payload={"text": text} if name in {"Speak", "SpeakStream"} else {},
        dialog_id=dialog_id,
    )


def _query_message(text: str, dialog_id: str = "dialog-1"):
    return _instruction_message(
        "Query",
        namespace="Template",
        payload={"text": text},
        dialog_id=dialog_id,
    )


def _recognize_result_message(text: str, *, is_final: bool, dialog_id: str = "dialog-1"):
    return _instruction_message(
        "RecognizeResult",
        namespace="SpeechRecognizer",
        payload={
            "is_final": is_final,
            "is_vad_begin": True,
            "results": [{"confidence": 0.0, "text": text}],
        },
        dialog_id=dialog_id,
    )


async def _drain_loop(turns: int = 5) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


async def _run_application_flow() -> tuple[FakeTransport, FakeEngine, FakeInterrupter]:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    application = _create_application(engine=engine, interrupter=interrupter)
    context = _context("conn-1", transport)

    await application.handle_inbound(_speak_message("SpeakStream", "hello"), context)
    await application.handle_inbound(_speak_message("SpeakStream", "world"), context)
    await application.handle_inbound(_speak_message("FinishSpeakStream", ""), context)
    await application.handle_inbound(
        _instruction_message("Finish", namespace="Dialog", dialog_id="dialog-1"),
        context,
    )

    return transport, engine, interrupter


def test_tts_replacement_stream_flow() -> None:
    transport, engine, interrupter = asyncio.run(_run_application_flow())

    assert transport.ensure_started_calls == 1
    assert transport.audio_chunks == [b"hello", b"world"]
    assert engine.started is True
    assert engine.pushed_texts == ["hello", "world"]
    assert engine.completed == 1
    assert engine.closed == 1
    assert interrupter.calls == [("conn-1", "dialog-1")]


def test_empty_speak_text_is_ignored() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    application = _create_application(engine=engine, interrupter=interrupter)
    context = _context("conn-2", transport)

    asyncio.run(application.handle_inbound(_speak_message("SpeakStream", "", "dialog-2"), context))

    assert transport.ensure_started_calls == 0
    assert engine.started is False
    assert interrupter.calls == [("conn-2", "dialog-2")]


def test_interrupt_happens_before_first_non_empty_tts_chunk() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    application = _create_application(engine=engine, interrupter=interrupter)
    context = _context("conn-4", transport)

    asyncio.run(application.handle_inbound(_speak_message("Speak", "", "dialog-4"), context))
    asyncio.run(application.handle_inbound(_speak_message("SpeakStream", "fresh", "dialog-4"), context))

    assert interrupter.calls == [("conn-4", "dialog-4")]
    assert transport.ensure_started_calls == 1
    assert engine.started is True
    assert engine.pushed_texts == ["fresh"]


def test_application_speak_text_starts_tts_for_new_dialog() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    application = _create_application(engine=engine, interrupter=interrupter)
    context = _context("conn-proactive", transport)

    async def scenario() -> None:
        await application.speak_text(
            context,
            dialog_id="dialog-proactive",
            text="正在查询天气",
            instruction_name="ToolCallStatus",
            source="tool",
        )
        await _drain_loop()

    asyncio.run(scenario())

    assert interrupter.calls == [("conn-proactive", "dialog-proactive")]
    assert transport.ensure_started_calls == 1
    assert engine.started is True
    assert engine.pushed_texts == ["正在查询天气"]


def test_application_speak_text_reuses_same_dialog_session() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    application = _create_application(engine=engine, interrupter=interrupter)
    context = _context("conn-proactive-reuse", transport)

    async def scenario() -> None:
        await application.speak_text(
            context,
            dialog_id="dialog-proactive-reuse",
            text="正在查询天气",
            instruction_name="ToolCallStatus",
            source="tool",
        )
        await _drain_loop()
        await application.speak_text(
            context,
            dialog_id="dialog-proactive-reuse",
            text="正在获取结果",
            instruction_name="ToolCallStatus",
            source="tool",
        )
        await _drain_loop()

    asyncio.run(scenario())

    assert interrupter.calls == [("conn-proactive-reuse", "dialog-proactive-reuse")]
    assert transport.ensure_started_calls == 1
    assert engine.pushed_texts == ["正在查询天气", "正在获取结果"]


def test_application_speak_text_ignores_blank_text() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    application = _create_application(engine=engine, interrupter=interrupter)
    context = _context("conn-proactive-blank", transport)

    asyncio.run(
        application.speak_text(
            context,
            dialog_id="dialog-proactive-blank",
            text="   ",
            instruction_name="ToolCallStatus",
            source="tool",
        )
    )

    assert interrupter.calls == []
    assert transport.ensure_started_calls == 0
    assert engine.started is False
    assert engine.pushed_texts == []


def test_application_speak_text_marks_dialog_as_server_owned() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    application = _create_application(engine=engine, interrupter=interrupter)
    context = _context("conn-proactive-owned", transport)

    async def scenario() -> None:
        await application.speak_text(
            context,
            dialog_id="dialog-proactive-owned",
            text="正在查询天气",
            instruction_name="ToolCallStatus",
            source="tool",
        )
        await _drain_loop()
        await application.handle_inbound(
            _speak_message("SpeakStream", "legacy text", "dialog-proactive-owned"),
            context,
        )
        await _drain_loop()

    asyncio.run(scenario())

    assert engine.pushed_texts == ["正在查询天气"]


def test_query_starts_agent_stream_and_forwards_text_to_tts() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    agent_service = FakeAgentService([["answer ", "from agent"]])
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
    )
    context = _context("conn-query", transport)

    async def scenario() -> None:
        await application.handle_inbound(_query_message("weather", "dialog-query"), context)
        await _drain_loop()

    asyncio.run(scenario())

    assert agent_service.prompts == ["weather"]
    assert interrupter.calls == [("conn-query", "dialog-query")]
    assert transport.ensure_started_calls == 1
    assert engine.started is True
    assert engine.pushed_texts == ["answer ", "from agent"]
    assert engine.completed == 1


def test_query_recall_injects_memories_into_agent_prompt() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    agent_service = FakeAgentService([["answer"]])
    memory_provider = FakeMemoryProvider(
        results=[
            [
                {"memory": "主人最近在关注天气", "score": 0.92},
            ]
        ],
        commit_worker=FakeMemoryCommitWorker(),
    )
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
        memory_provider=memory_provider,
    )
    context = _context("conn-memory-query", transport)

    async def scenario() -> None:
        await application.handle_inbound(_query_message("weather", "dialog-memory-query"), context)
        await _drain_loop()

    asyncio.run(scenario())

    assert len(memory_provider.search_calls) == 1
    assert memory_provider.search_calls[0] == {"query": "weather", "user_id": "txuw"}
    assert "[相关记忆]" in agent_service.prompts[0]
    assert "主人最近在关注天气" in agent_service.prompts[0]
    assert "[当前用户问题]\nweather" in agent_service.prompts[0]


def test_query_recall_failure_falls_back_to_original_prompt() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    agent_service = FakeAgentService([["answer"]])
    memory_provider = FakeMemoryProvider(
        search_error=RuntimeError("memory unavailable"),
        commit_worker=FakeMemoryCommitWorker(),
    )
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
        memory_provider=memory_provider,
    )
    context = _context("conn-memory-fallback", transport)

    async def scenario() -> None:
        await application.handle_inbound(_query_message("weather", "dialog-memory-fallback"), context)
        await _drain_loop()

    asyncio.run(scenario())

    assert agent_service.prompts == ["weather"]


def test_memory_recall_completed_log_contains_stage_timings(caplog) -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    agent_service = FakeAgentService([["answer"]])
    memory_provider = FakeMemoryProvider(
        results=[
            [
                {"memory": "主人最近在关注天气", "score": 0.92},
            ]
        ],
        search_metrics=[
            MemoryRecallMetrics(
                embedding_http_ms=91,
                milvus_search_ms=18,
                memory_recall_total_ms=123,
            )
        ],
        commit_worker=FakeMemoryCommitWorker(),
    )
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
        memory_provider=memory_provider,
    )
    context = _context("conn-memory-log", transport)

    async def scenario() -> None:
        await application.handle_inbound(_query_message("weather", "dialog-memory-log"), context)
        await _drain_loop()

    logger_name = "txuw_xiaoai_server.xiaoai_handlers.services.tts_replacement_coordinator"
    with caplog.at_level(logging.INFO, logger=logger_name):
        asyncio.run(scenario())

    completed_records = [
        record for record in caplog.records if record.msg == "memory.recall.completed"
    ]

    assert len(completed_records) == 1
    assert getattr(completed_records[0], "embedding_http_ms") == 91
    assert getattr(completed_records[0], "milvus_search_ms") == 18
    assert getattr(completed_records[0], "memory_recall_total_ms") == 123
    assert getattr(completed_records[0], "hitCount") == 1


def test_agent_completed_log_contains_aggregated_text(caplog) -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    agent_service = FakeAgentService([["answer ", "from agent"]])
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
    )
    context = _context("conn-log", transport)

    async def scenario() -> None:
        await application.handle_inbound(_query_message("weather", "dialog-log"), context)
        await _drain_loop()

    logger_name = "txuw_xiaoai_server.xiaoai_handlers.services.tts_replacement_coordinator"
    with caplog.at_level(logging.INFO, logger=logger_name):
        asyncio.run(scenario())

    completed_records = [
        record for record in caplog.records if record.msg == "agent.stream.completed"
    ]

    assert len(completed_records) == 1
    assert getattr(completed_records[0], "fullText") == "answer from agent"


def test_final_asr_can_start_agent_stream_without_query() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    agent_service = FakeAgentService([["fallback answer"]])
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
    )
    context = _context("conn-asr", transport)

    async def scenario() -> None:
        await application.handle_inbound(
            _recognize_result_message("recognized text", is_final=True, dialog_id="dialog-asr"),
            context,
        )
        await _drain_loop()

    asyncio.run(scenario())

    assert agent_service.prompts == ["recognized text"]
    assert transport.ensure_started_calls == 1
    assert engine.pushed_texts == ["fallback answer"]
    assert engine.completed == 1


def test_final_asr_uses_same_memory_recall_path() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    agent_service = FakeAgentService([["fallback answer"]])
    memory_provider = FakeMemoryProvider(
        results=[
            [
                {"memory": "主人喜欢简短回答", "score": 0.88},
            ]
        ],
        commit_worker=FakeMemoryCommitWorker(),
    )
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
        memory_provider=memory_provider,
    )
    context = _context("conn-memory-asr", transport)

    async def scenario() -> None:
        await application.handle_inbound(
            _recognize_result_message("recognized text", is_final=True, dialog_id="dialog-memory-asr"),
            context,
        )
        await _drain_loop()

    asyncio.run(scenario())

    assert len(memory_provider.search_calls) == 1
    assert memory_provider.search_calls[0] == {
        "query": "recognized text",
        "user_id": "txuw",
    }
    assert "主人喜欢简短回答" in agent_service.prompts[0]


def test_application_speak_text_can_append_while_agent_stream_is_running() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    agent_service = BlockingAgentService(first_delta="agent streaming")
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
    )
    context = _context("conn-tool", transport)

    async def scenario() -> bool:
        await application.handle_inbound(_query_message("weather", "dialog-tool"), context)
        await agent_service.started.wait()
        await _drain_loop(2)
        await application.speak_text(
            context,
            dialog_id="dialog-tool",
            text="正在查询天气",
            instruction_name="ToolCallStatus",
            source="tool",
        )
        await _drain_loop(2)
        cancelled_before_cleanup = agent_service.cancelled.is_set()
        await application.on_disconnect(context)
        return cancelled_before_cleanup

    cancelled_before_cleanup = asyncio.run(scenario())

    assert cancelled_before_cleanup is False
    assert engine.pushed_texts == ["agent streaming", "正在查询天气"]
    assert agent_service.cancelled.is_set()


def test_server_owned_dialog_ignores_legacy_speech_messages() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    agent_service = FakeAgentService([["proxy answer"]])
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
    )
    context = _context("conn-owned", transport)

    async def scenario() -> None:
        await application.handle_inbound(_query_message("weather", "dialog-owned"), context)
        await _drain_loop()
        await application.handle_inbound(_speak_message("SpeakStream", "legacy text", "dialog-owned"), context)
        await application.handle_inbound(_speak_message("FinishSpeakStream", "", "dialog-owned"), context)
        await _drain_loop()

    asyncio.run(scenario())

    assert engine.pushed_texts == ["proxy answer"]
    assert engine.completed == 1


def test_empty_or_whitespace_agent_deltas_are_not_forwarded() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    agent_service = FakeAgentService([["", "   ", "hello", "\n", "world"]])
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
    )
    context = _context("conn-delta", transport)

    async def scenario() -> None:
        await application.handle_inbound(_query_message("weather", "dialog-delta"), context)
        await _drain_loop()

    asyncio.run(scenario())

    assert engine.pushed_texts == ["hello", "world"]
    assert transport.audio_chunks == [b"hello", b"world"]


def test_application_speak_text_skips_sealed_dialog() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    agent_service = FakeAgentService([["agent answer"]])
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
    )
    context = _context("conn-sealed", transport)

    async def scenario() -> None:
        await application.handle_inbound(_query_message("weather", "dialog-sealed"), context)
        await _drain_loop()
        await application.speak_text(
            context,
            dialog_id="dialog-sealed",
            text="正在查询天气",
            instruction_name="ToolCallStatus",
            source="tool",
        )
        await _drain_loop()

    asyncio.run(scenario())

    assert interrupter.calls == [("conn-sealed", "dialog-sealed")]
    assert transport.ensure_started_calls == 1
    assert engine.pushed_texts == ["agent answer"]
    assert engine.completed == 1


def test_disconnect_cancels_agent_task_and_closes_engine() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    agent_service = BlockingAgentService()
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
    )
    context = _context("conn-close", transport)

    async def scenario() -> None:
        await application.handle_inbound(_query_message("weather", "dialog-close"), context)
        await agent_service.started.wait()
        await _drain_loop(2)
        await application.on_disconnect(context)

    asyncio.run(scenario())

    assert engine.started is True
    assert engine.closed == 1
    assert agent_service.cancelled.is_set()


def test_successful_agent_stream_enqueues_memory_commit() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    commit_worker = FakeMemoryCommitWorker()
    memory_provider = FakeMemoryProvider(commit_worker=commit_worker)
    agent_service = FakeAgentService([["answer ", "from agent"]])
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
        memory_provider=memory_provider,
    )
    context = _context("conn-commit", transport)

    async def scenario() -> None:
        await application.handle_inbound(_query_message("weather", "dialog-commit"), context)
        await _drain_loop()

    asyncio.run(scenario())

    assert commit_worker.calls == [
        {
            "user_id": "txuw",
            "messages": [
                {"role": "user", "content": "weather"},
                {"role": "assistant", "content": "answer from agent"},
            ],
            "idempotency_key": "conn-commit:dialog-commit:query",
        }
    ]


def test_empty_agent_output_does_not_enqueue_memory_commit() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    commit_worker = FakeMemoryCommitWorker()
    memory_provider = FakeMemoryProvider(commit_worker=commit_worker)
    agent_service = FakeAgentService([["", "   ", "\n"]])
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
        memory_provider=memory_provider,
    )
    context = _context("conn-empty-commit", transport)

    async def scenario() -> None:
        await application.handle_inbound(_query_message("weather", "dialog-empty-commit"), context)
        await _drain_loop()

    asyncio.run(scenario())

    assert commit_worker.calls == []


def test_failed_agent_stream_does_not_enqueue_memory_commit() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    commit_worker = FakeMemoryCommitWorker()
    memory_provider = FakeMemoryProvider(commit_worker=commit_worker)
    agent_service = FailingAgentService()
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=True,
        memory_provider=memory_provider,
    )
    context = _context("conn-failed-commit", transport)

    async def scenario() -> None:
        await application.handle_inbound(_query_message("weather", "dialog-failed-commit"), context)
        await _drain_loop()

    asyncio.run(scenario())

    assert commit_worker.calls == []


def test_application_startup_and_close_manage_memory_provider() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    memory_provider = FakeMemoryProvider(commit_worker=FakeMemoryCommitWorker())
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        memory_provider=memory_provider,
    )

    async def scenario() -> None:
        await application.startup()
        await application.close()

    asyncio.run(scenario())

    assert memory_provider.startup_calls == 1
    assert memory_provider.shutdown_calls == 1


def test_transport_ensure_started_sends_start_play_request() -> None:
    websocket = FakeWebSocket()
    transport = ClientSessionTransport(
        websocket,
        "conn-3",
        play_config={
            "pcm": "noop",
            "channels": 1,
            "bits_per_sample": 16,
            "sample_rate": 22050,
            "period_size": 330,
            "buffer_size": 1320,
        },
    )

    async def scenario() -> None:
        task = asyncio.create_task(transport.ensure_started())
        await asyncio.sleep(0)
        payload = json.loads(websocket.text_messages[-1])
        response = InboundResponse(
            body=ResponseBody(
                id=payload["Request"]["id"],
                code=0,
                msg="success",
                data=None,
            )
        )
        transport.accept_response(response)
        await task

    asyncio.run(scenario())

    payload = json.loads(websocket.text_messages[-1])
    assert payload["Request"]["command"] == "start_play"
    assert payload["Request"]["payload"]["sample_rate"] == 22050
