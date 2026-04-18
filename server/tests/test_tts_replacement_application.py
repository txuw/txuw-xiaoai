from __future__ import annotations

import asyncio
import json

from txuw_xiaoai_server.protocol import InboundResponse, ResponseBody, parse_text_message
from txuw_xiaoai_server.transport import ClientSessionTransport
from txuw_xiaoai_server.xiaoai_handlers import ConnectionContext, XiaoAiApplication


class FakeTransport:
    """测试用传输层桩对象。"""

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
    """测试用 TTS 引擎。"""

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
    """测试用旧播报中断器。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def interrupt(self, connection_id: str, dialog_id: str) -> bool:
        self.calls.append((connection_id, dialog_id))
        return True


class FakeWebSocket:
    """捕获 transport 发出的请求与音频帧。"""

    def __init__(self) -> None:
        self.text_messages: list[str] = []
        self.binary_messages: list[bytes] = []

    async def send_text(self, data: str) -> None:
        self.text_messages.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.binary_messages.append(data)


def _instruction_message(name: str, text: str, dialog_id: str = "dialog-1"):
    payload: dict[str, object] = {}
    if name in {"Speak", "SpeakStream"}:
        payload = {"text": text}
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
                                    "namespace": "SpeechSynthesizer",
                                },
                                "payload": payload,
                            }
                        )
                    },
                }
            }
        )
    )


async def _run_application_flow() -> tuple[FakeTransport, FakeEngine, FakeInterrupter]:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    application = XiaoAiApplication(
        engine_factory=lambda: engine,
        interrupter_factory=lambda _context: interrupter,
        enabled=True,
    )
    context = ConnectionContext(
        connection_id="conn-1",
        playback_port=transport,
        audio_sink=transport,
    )

    await application.handle_inbound(_instruction_message("SpeakStream", "你好，"), context)
    await application.handle_inbound(_instruction_message("SpeakStream", "主人。"), context)
    await application.handle_inbound(_instruction_message("FinishSpeakStream", ""), context)
    await application.handle_inbound(_instruction_message("Finish", ""), context)

    return transport, engine, interrupter


def test_tts_replacement_stream_flow() -> None:
    transport, engine, interrupter = asyncio.run(_run_application_flow())

    assert transport.ensure_started_calls == 1
    assert transport.audio_chunks == ["你好，".encode("utf-8"), "主人。".encode("utf-8")]
    assert engine.started is True
    assert engine.pushed_texts == ["你好，", "主人。"]
    assert engine.completed == 1
    assert engine.closed == 1
    assert interrupter.calls == [("conn-1", "dialog-1")]


def test_empty_speak_text_is_ignored() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    application = XiaoAiApplication(
        engine_factory=lambda: engine,
        interrupter_factory=lambda _context: interrupter,
        enabled=True,
    )
    context = ConnectionContext(
        connection_id="conn-2",
        playback_port=transport,
        audio_sink=transport,
    )

    asyncio.run(application.handle_inbound(_instruction_message("SpeakStream", "", "dialog-2"), context))

    assert transport.ensure_started_calls == 0
    assert engine.started is False
    assert interrupter.calls == [("conn-2", "dialog-2")]


def test_interrupt_happens_before_first_non_empty_tts_chunk() -> None:
    transport = FakeTransport()
    engine = FakeEngine()
    interrupter = FakeInterrupter()
    application = XiaoAiApplication(
        engine_factory=lambda: engine,
        interrupter_factory=lambda _context: interrupter,
        enabled=True,
    )
    context = ConnectionContext(
        connection_id="conn-4",
        playback_port=transport,
        audio_sink=transport,
    )

    asyncio.run(application.handle_inbound(_instruction_message("Speak", "", "dialog-4"), context))
    asyncio.run(application.handle_inbound(_instruction_message("SpeakStream", "新的播报", "dialog-4"), context))

    assert interrupter.calls == [("conn-4", "dialog-4")]
    assert transport.ensure_started_calls == 1
    assert engine.started is True
    assert engine.pushed_texts == ["新的播报"]


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
