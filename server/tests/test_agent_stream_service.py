from __future__ import annotations

import asyncio
from types import SimpleNamespace

from txuw_xiaoai_server.xiaoai_handlers.agent import agent as agent_module


class FakeStreamResult:
    def __init__(self, events, run_loop_exception=None) -> None:
        self._events = list(events)
        self.run_loop_exception = run_loop_exception
        self.cancel_calls = 0

    async def stream_events(self):
        for event in self._events:
            await asyncio.sleep(0)
            yield event

    def cancel(self, mode: str = "immediate") -> None:
        self.cancel_calls += 1


class FakeClosableClient:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def _event(event_type: str, delta: str | None = None):
    return SimpleNamespace(
        type="raw_response_event",
        data=SimpleNamespace(type=event_type, delta=delta),
    )


def test_agent_stream_service_only_yields_output_text_deltas(monkeypatch) -> None:
    captured: dict[str, object] = {}
    fake_result = FakeStreamResult(
        [
            SimpleNamespace(type="agent_updated_stream_event", data=None),
            _event("response.output_text.delta", "hello"),
            _event("response.refusal.delta", "ignored"),
            _event("response.output_text.delta", "world"),
        ]
    )

    def fake_run_streamed(starting_agent, prompt, run_config=None):
        captured["agent"] = starting_agent
        captured["prompt"] = prompt
        captured["run_config"] = run_config
        return fake_result

    monkeypatch.setattr(agent_module.Runner, "run_streamed", fake_run_streamed)

    service = agent_module.AgentStreamService(
        agent_module.AgentStreamConfig(
            api_key="test-key",
            base_url="http://example.com/v1",
            model="gpt-test",
            system_prompt="test prompt",
        )
    )

    async def scenario() -> list[str]:
        return [chunk async for chunk in service.stream_text("weather")]

    chunks = asyncio.run(scenario())

    assert chunks == ["hello", "world"]
    assert captured["prompt"] == "weather"
    assert captured["run_config"] is not None
    assert captured["run_config"].tracing_disabled is True


def test_agent_stream_service_skips_empty_and_whitespace_deltas(monkeypatch) -> None:
    fake_result = FakeStreamResult(
        [
            _event("response.output_text.delta", ""),
            _event("response.output_text.delta", "   "),
            _event("response.output_text.delta", "\n"),
            _event("response.output_text.delta", "kept"),
        ]
    )

    monkeypatch.setattr(agent_module.Runner, "run_streamed", lambda *_args, **_kwargs: fake_result)

    service = agent_module.AgentStreamService(
        agent_module.AgentStreamConfig(
            api_key="test-key",
            model="gpt-test",
        )
    )

    async def scenario() -> list[str]:
        return [chunk async for chunk in service.stream_text("weather")]

    chunks = asyncio.run(scenario())

    assert chunks == ["kept"]


def test_agent_stream_service_close_closes_underlying_client() -> None:
    service = agent_module.AgentStreamService(
        agent_module.AgentStreamConfig(
            api_key="test-key",
            model="gpt-test",
        )
    )
    fake_client = FakeClosableClient()
    service._client = fake_client

    asyncio.run(service.close())

    assert fake_client.close_calls == 1
