from __future__ import annotations

import asyncio
import io
import json
from types import MethodType
import wave

import pytest
from pydantic import ValidationError

from txuw_xiaoai_server.config import KwsTakeoverRule, settings
from txuw_xiaoai_server.protocol import parse_text_message
from txuw_xiaoai_server.xiaoai_handlers import ConnectionContext, XiaoAiApplication


WAKE_COMMAND = """ubus call pnshelper event_notify '{"src":1,"event":0}'"""


class FakeTransport:
    def __init__(self) -> None:
        self.ensure_started_calls = 0
        self.run_shell_calls: list[str] = []
        self.audio_chunks: list[bytes] = []

    async def ensure_started(self) -> None:
        self.ensure_started_calls += 1

    async def stop(self) -> None:
        return None

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

    def stream_text(self, prompt: str, run_context):
        async def generator():
            self.prompts.append(prompt)
            deltas = self._responses.pop(0) if self._responses else []
            for delta in deltas:
                await asyncio.sleep(0)
                yield delta

        return generator()

    async def close(self) -> None:
        return None

    @property
    def region_service(self):
        return object()


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
    payload: dict[str, object] | None = None,
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


def _query_message(text: str, dialog_id: str = "dialog-1"):
    return _instruction_message(
        "Query",
        namespace="Template",
        payload={"text": text},
        dialog_id=dialog_id,
    )


def _recognize_result_message(
    text: str,
    *,
    is_final: bool,
    dialog_id: str = "dialog-1",
    include_is_vad_begin: bool = True,
):
    payload: dict[str, object] = {
        "is_final": is_final,
        "results": [{"confidence": 0.0, "text": text}],
    }
    if include_is_vad_begin:
        payload["is_vad_begin"] = True

    return _instruction_message(
        "RecognizeResult",
        namespace="SpeechRecognizer",
        payload=payload,
        dialog_id=dialog_id,
    )


def _kws_message(keyword: str, event_id: str = "kws-id"):
    return parse_text_message(
        json.dumps(
            {
                "Event": {
                    "id": event_id,
                    "event": "kws",
                    "data": {"Keyword": keyword},
                }
            }
        )
    )


def _playing_message(state: str):
    return parse_text_message(
        json.dumps(
            {
                "Event": {
                    "id": f"playing-{state}",
                    "event": "playing",
                    "data": state,
                }
            }
        )
    )


async def _drain_loop(turns: int = 5) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


def _build_wav_bytes(
    *,
    sample_rate: int = settings.tts_sample_rate,
    channels: int = settings.tts_channels,
    sample_width: int = settings.tts_bits_per_sample // 8,
) -> tuple[bytes, bytes]:
    pcm_bytes = b"\x01\x02" * 64 * channels
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    return buffer.getvalue(), pcm_bytes


def _create_application(
    *,
    engine: FakeEngine,
    interrupter: FakeInterrupter,
    agent_service: FakeAgentService | None = None,
    agent_enabled: bool = False,
    kws_rules: list[KwsTakeoverRule] | None = None,
    kws_pending_timeout_seconds: float = 8.0,
    kws_audio_cache_dir: str = ".cache/kws-audio-test",
) -> XiaoAiApplication:
    return XiaoAiApplication(
        engine_factory=lambda: engine,
        interrupter_factory=lambda _context: interrupter,
        enabled=True,
        agent_service=agent_service,
        agent_enabled=agent_enabled,
        kws_takeover_enabled=True,
        kws_takeover_rules=kws_rules or [],
        kws_native_wake_command=WAKE_COMMAND,
        kws_pending_timeout_seconds=kws_pending_timeout_seconds,
        kws_audio_cache_dir=kws_audio_cache_dir,
    )


def test_kws_takeover_rule_rejects_non_http_url() -> None:
    with pytest.raises(ValidationError):
        KwsTakeoverRule(keyword="龙颜同学", welcome_audio_url="oss://bucket/audio.wav")


def test_startup_downloads_duplicate_welcome_audio_only_once(tmp_path) -> None:
    wav_bytes, _ = _build_wav_bytes()
    download_calls: list[str] = []

    async def fake_download(self, url: str) -> bytes:
        download_calls.append(url)
        return wav_bytes

    engine = FakeEngine()
    interrupter = FakeInterrupter()
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        kws_rules=[
            KwsTakeoverRule(
                keyword="龙颜同学",
                welcome_audio_url="https://example.com/audio/welcome.wav",
            ),
            KwsTakeoverRule(
                keyword="主人同学",
                welcome_audio_url="https://example.com/audio/welcome.wav",
            ),
        ],
        kws_audio_cache_dir=str(tmp_path),
    )
    application._download_welcome_audio_bytes = MethodType(fake_download, application)

    async def scenario() -> None:
        await application.startup()
        await application.close()

    asyncio.run(scenario())

    assert download_calls == ["https://example.com/audio/welcome.wav"]
    assert len(list(tmp_path.glob("*.wav"))) == 1


def test_startup_fails_when_welcome_audio_format_is_invalid(tmp_path) -> None:
    wav_bytes, _ = _build_wav_bytes(sample_rate=settings.tts_sample_rate + 1)

    async def fake_download(self, url: str) -> bytes:
        return wav_bytes

    engine = FakeEngine()
    interrupter = FakeInterrupter()
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        kws_rules=[
            KwsTakeoverRule(
                keyword="龙颜同学",
                welcome_audio_url="https://example.com/audio/welcome.wav",
            ),
        ],
        kws_audio_cache_dir=str(tmp_path),
    )
    application._download_welcome_audio_bytes = MethodType(fake_download, application)

    with pytest.raises(RuntimeError):
        asyncio.run(application.startup())


def test_takeover_kws_starts_native_wake_before_welcome_audio_and_server_dialog(tmp_path) -> None:
    wav_bytes, pcm_bytes = _build_wav_bytes()
    agent_service = FakeAgentService([["server answer"]])

    async def fake_download(self, url: str) -> bytes:
        return wav_bytes

    engine = FakeEngine()
    interrupter = FakeInterrupter()
    transport = FakeTransport()
    context = _context("conn-takeover", transport)
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        agent_service=agent_service,
        agent_enabled=True,
        kws_rules=[
            KwsTakeoverRule(
                keyword="龙颜同学",
                welcome_audio_url="https://example.com/audio/welcome.wav",
            ),
        ],
        kws_audio_cache_dir=str(tmp_path),
    )
    application._download_welcome_audio_bytes = MethodType(fake_download, application)

    async def scenario() -> None:
        await application.startup()
        await application.handle_inbound(_kws_message("龙颜同学", "kws-takeover"), context)
        assert transport.run_shell_calls == [WAKE_COMMAND]
        assert transport.audio_chunks == []

        await _drain_loop(2)
        assert transport.audio_chunks == [pcm_bytes]
        await application.handle_inbound(_query_message("weather", "dialog-takeover"), context)
        await _drain_loop(4)
        await application.handle_inbound(_playing_message("Idle"), context)
        await _drain_loop(2)
        await application.close()

    asyncio.run(scenario())

    assert agent_service.prompts == ["weather"]
    assert engine.pushed_texts == ["server answer"]
    assert engine.completed == 1
    assert transport.ensure_started_calls == 2
    assert not any(dialog_id.startswith("kws-welcome:") for _, dialog_id in interrupter.calls)
    assert ("conn-takeover", "dialog-takeover") in interrupter.calls


def test_takeover_kws_final_asr_without_is_vad_begin_still_starts_server_dialog(tmp_path) -> None:
    wav_bytes, pcm_bytes = _build_wav_bytes()
    agent_service = FakeAgentService([["server answer"]])

    async def fake_download(self, url: str) -> bytes:
        return wav_bytes

    engine = FakeEngine()
    interrupter = FakeInterrupter()
    transport = FakeTransport()
    context = _context("conn-takeover-asr", transport)
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        agent_service=agent_service,
        agent_enabled=True,
        kws_rules=[
            KwsTakeoverRule(
                keyword="龙颜同学",
                welcome_audio_url="https://example.com/audio/welcome.wav",
            ),
        ],
        kws_audio_cache_dir=str(tmp_path),
    )
    application._download_welcome_audio_bytes = MethodType(fake_download, application)

    async def scenario() -> None:
        await application.startup()
        await application.handle_inbound(_kws_message("龙颜同学", "kws-takeover-asr"), context)
        assert transport.run_shell_calls == [WAKE_COMMAND]
        assert transport.audio_chunks == []

        await _drain_loop(2)
        assert transport.audio_chunks == [pcm_bytes]

        await application.handle_inbound(
            _recognize_result_message(
                "今天天气如何",
                is_final=True,
                dialog_id="dialog-takeover-asr",
                include_is_vad_begin=False,
            ),
            context,
        )
        await _drain_loop(4)
        await application.handle_inbound(_playing_message("Idle"), context)
        await _drain_loop(2)
        await application.close()

    asyncio.run(scenario())

    assert agent_service.prompts == ["今天天气如何"]
    assert engine.pushed_texts == ["server answer"]
    assert engine.completed == 1
    assert transport.ensure_started_calls == 2
    assert not any(dialog_id.startswith("kws-welcome:") for _, dialog_id in interrupter.calls)


def test_non_takeover_kws_keeps_native_dialog_flow(tmp_path) -> None:
    wav_bytes, _ = _build_wav_bytes()
    agent_service = FakeAgentService([["server answer"]])

    async def fake_download(self, url: str) -> bytes:
        return wav_bytes

    engine = FakeEngine()
    interrupter = FakeInterrupter()
    transport = FakeTransport()
    context = _context("conn-native", transport)
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        agent_service=agent_service,
        agent_enabled=True,
        kws_rules=[
            KwsTakeoverRule(
                keyword="龙颜同学",
                welcome_audio_url="https://example.com/audio/welcome.wav",
            ),
        ],
        kws_audio_cache_dir=str(tmp_path),
    )
    application._download_welcome_audio_bytes = MethodType(fake_download, application)

    async def scenario() -> None:
        await application.startup()
        await application.handle_inbound(_kws_message("小度小度", "kws-native"), context)
        await application.handle_inbound(_query_message("weather", "dialog-native"), context)
        await _drain_loop(3)
        await application.close()

    asyncio.run(scenario())

    assert transport.audio_chunks == []
    assert transport.run_shell_calls == [WAKE_COMMAND]
    assert agent_service.prompts == []
    assert engine.started is False


def test_dialog_without_kws_event_keeps_native_dialog_flow(tmp_path) -> None:
    wav_bytes, _ = _build_wav_bytes()
    agent_service = FakeAgentService([["server answer"]])

    async def fake_download(self, url: str) -> bytes:
        return wav_bytes

    engine = FakeEngine()
    interrupter = FakeInterrupter()
    transport = FakeTransport()
    context = _context("conn-no-kws", transport)
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        agent_service=agent_service,
        agent_enabled=True,
        kws_rules=[
            KwsTakeoverRule(
                keyword="榫欓鍚屽",
                welcome_audio_url="https://example.com/audio/welcome.wav",
            ),
        ],
        kws_audio_cache_dir=str(tmp_path),
    )
    application._download_welcome_audio_bytes = MethodType(fake_download, application)

    async def scenario() -> None:
        await application.startup()
        await application.handle_inbound(
            _recognize_result_message("打开电视", is_final=True, dialog_id="dialog-no-kws"),
            context,
        )
        await _drain_loop(3)
        await application.close()

    asyncio.run(scenario())

    assert transport.audio_chunks == []
    assert transport.run_shell_calls == []
    assert agent_service.prompts == []
    assert engine.started is False


def test_last_kws_decision_wins_before_dialog_binding(tmp_path) -> None:
    wav_bytes, _ = _build_wav_bytes()
    agent_service = FakeAgentService([["server answer"]])

    async def fake_download(self, url: str) -> bytes:
        return wav_bytes

    engine = FakeEngine()
    interrupter = FakeInterrupter()
    transport = FakeTransport()
    context = _context("conn-last-kws", transport)
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        agent_service=agent_service,
        agent_enabled=True,
        kws_rules=[
            KwsTakeoverRule(
                keyword="龙颜同学",
                welcome_audio_url="https://example.com/audio/welcome.wav",
            ),
        ],
        kws_audio_cache_dir=str(tmp_path),
    )
    application._download_welcome_audio_bytes = MethodType(fake_download, application)

    async def scenario() -> None:
        await application.startup()
        takeover_keyword = next(iter(application._kws_takeover_rules))
        await application.handle_inbound(_kws_message("native", "kws-native"), context)
        await application.handle_inbound(_kws_message(takeover_keyword, "kws-takeover"), context)
        assert transport.run_shell_calls == [WAKE_COMMAND, WAKE_COMMAND]
        assert transport.audio_chunks == []
        await _drain_loop(2)
        await application.handle_inbound(_query_message("weather", "dialog-last-kws"), context)
        await _drain_loop(4)
        await application.handle_inbound(_playing_message("Idle"), context)
        await _drain_loop(2)
        await application.close()

    asyncio.run(scenario())

    assert transport.run_shell_calls == [WAKE_COMMAND, WAKE_COMMAND]
    assert agent_service.prompts == ["weather"]


def test_pending_kws_decision_expires_after_timeout(tmp_path) -> None:
    wav_bytes, _ = _build_wav_bytes()
    agent_service = FakeAgentService([["server answer"]])

    async def fake_download(self, url: str) -> bytes:
        return wav_bytes

    engine = FakeEngine()
    interrupter = FakeInterrupter()
    transport = FakeTransport()
    context = _context("conn-expire", transport)
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        agent_service=agent_service,
        agent_enabled=True,
        kws_rules=[
            KwsTakeoverRule(
                keyword="龙颜同学",
                welcome_audio_url="https://example.com/audio/welcome.wav",
            ),
        ],
        kws_pending_timeout_seconds=0.01,
        kws_audio_cache_dir=str(tmp_path),
    )
    application._download_welcome_audio_bytes = MethodType(fake_download, application)

    async def scenario() -> None:
        await application.startup()
        takeover_keyword = next(iter(application._kws_takeover_rules))
        await application.handle_inbound(_kws_message(takeover_keyword, "kws-expire"), context)
        await asyncio.sleep(0.02)
        await application.handle_inbound(_query_message("weather", "dialog-expire"), context)
        await _drain_loop(3)
        await application.close()

    asyncio.run(scenario())

    assert agent_service.prompts == []
    assert engine.started is False


def test_disconnect_clears_pending_kws_decision(tmp_path) -> None:
    wav_bytes, _ = _build_wav_bytes()
    agent_service = FakeAgentService([["server answer"]])

    async def fake_download(self, url: str) -> bytes:
        return wav_bytes

    engine = FakeEngine()
    interrupter = FakeInterrupter()
    transport = FakeTransport()
    context = _context("conn-disconnect", transport)
    application = _create_application(
        engine=engine,
        interrupter=interrupter,
        agent_service=agent_service,
        agent_enabled=True,
        kws_rules=[
            KwsTakeoverRule(
                keyword="龙颜同学",
                welcome_audio_url="https://example.com/audio/welcome.wav",
            ),
        ],
        kws_audio_cache_dir=str(tmp_path),
    )
    application._download_welcome_audio_bytes = MethodType(fake_download, application)

    async def scenario() -> None:
        await application.startup()
        takeover_keyword = next(iter(application._kws_takeover_rules))
        await application.handle_inbound(_kws_message(takeover_keyword, "kws-disconnect"), context)
        await _drain_loop(2)
        await application.on_disconnect(context)
        await application.handle_inbound(_query_message("weather", "dialog-after-disconnect"), context)
        await _drain_loop(3)
        await application.close()

    asyncio.run(scenario())

    assert agent_service.prompts == []
