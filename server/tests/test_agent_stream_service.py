from __future__ import annotations

import asyncio
from types import SimpleNamespace

from txuw_xiaoai_server.xiaoai_handlers.agent import REGION_TOOL_NAME
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


class FakeRegionService:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def _event(event_type: str, delta: str | None = None):
    return SimpleNamespace(
        type="raw_response_event",
        data=SimpleNamespace(type=event_type, delta=delta),
    )


def _run_context(region_service: FakeRegionService) -> agent_module.AgentRunContext:
    async def speak_progress(_text: str) -> None:
        return None

    return agent_module.AgentRunContext(
        connection_id="conn-1",
        dialog_id="dialog-1",
        instruction_name="Query",
        source="query",
        locale="zh-CN",
        speak_progress=speak_progress,
        region_service=region_service,
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

    def fake_run_streamed(starting_agent, prompt, context=None, run_config=None):
        captured["agent"] = starting_agent
        captured["prompt"] = prompt
        captured["context"] = context
        captured["run_config"] = run_config
        return fake_result

    monkeypatch.setattr(agent_module.Runner, "run_streamed", fake_run_streamed)
    region_service = FakeRegionService()
    run_context = _run_context(region_service)

    service = agent_module.AgentStreamService(
        agent_module.AgentStreamConfig(
            api_key="test-key",
            base_url="http://example.com/v1",
            model="gpt-test",
            system_prompt="test prompt",
        ),
        region_service=region_service,
    )

    async def scenario() -> list[str]:
        return [chunk async for chunk in service.stream_text("weather", run_context)]

    chunks = asyncio.run(scenario())

    assert chunks == ["hello", "world"]
    assert captured["prompt"] == "weather"
    assert captured["context"] is run_context
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
    region_service = FakeRegionService()
    run_context = _run_context(region_service)

    service = agent_module.AgentStreamService(
        agent_module.AgentStreamConfig(
            api_key="test-key",
            model="gpt-test",
        ),
        region_service=region_service,
    )

    async def scenario() -> list[str]:
        return [chunk async for chunk in service.stream_text("weather", run_context)]

    chunks = asyncio.run(scenario())

    assert chunks == ["kept"]


def test_agent_stream_service_close_closes_underlying_client() -> None:
    region_service = FakeRegionService()
    service = agent_module.AgentStreamService(
        agent_module.AgentStreamConfig(
            api_key="test-key",
            model="gpt-test",
        ),
        region_service=region_service,
    )
    fake_client = FakeClosableClient()
    service._client = fake_client

    asyncio.run(service.close())

    assert fake_client.close_calls == 1
    assert region_service.close_calls == 1


def test_agent_stream_service_registers_region_tool_when_key_present(monkeypatch) -> None:
    captured: dict[str, object] = {}
    fake_result = FakeStreamResult([])

    def fake_run_streamed(starting_agent, prompt, context=None, run_config=None):
        captured["agent"] = starting_agent
        captured["prompt"] = prompt
        captured["context"] = context
        captured["run_config"] = run_config
        return fake_result

    monkeypatch.setattr(agent_module.Runner, "run_streamed", fake_run_streamed)

    region_service = FakeRegionService()
    run_context = _run_context(region_service)
    service = agent_module.AgentStreamService(
        agent_module.AgentStreamConfig(
            api_key="test-key",
            model="gpt-test",
            system_prompt="基础提示词",
        ),
        region_service=region_service,
        toolset_factory=agent_module.AgentToolsetFactory(
            amap_web_service_key="test-map-key",
            region_tool_enabled=True,
            region_tool_timeout_seconds=1.0,
        ),
    )

    async def scenario() -> None:
        _ = [chunk async for chunk in service.stream_text("weather", run_context)]

    asyncio.run(scenario())

    tool_names = [tool.name for tool in captured["agent"].tools]
    assert tool_names == [REGION_TOOL_NAME]
    assert "询问当前地区" in captured["agent"].instructions
    assert captured["agent"].mcp_config["convert_schemas_to_strict"] is True


def test_agent_stream_service_does_not_register_region_tool_without_key(monkeypatch) -> None:
    captured: dict[str, object] = {}
    fake_result = FakeStreamResult([])

    def fake_run_streamed(starting_agent, prompt, context=None, run_config=None):
        captured["agent"] = starting_agent
        return fake_result

    monkeypatch.setattr(agent_module.Runner, "run_streamed", fake_run_streamed)

    region_service = FakeRegionService()
    run_context = _run_context(region_service)
    service = agent_module.AgentStreamService(
        agent_module.AgentStreamConfig(
            api_key="test-key",
            model="gpt-test",
            system_prompt="基础提示词",
        ),
        region_service=region_service,
        toolset_factory=agent_module.AgentToolsetFactory(
            amap_web_service_key="",
            region_tool_enabled=True,
            region_tool_timeout_seconds=1.0,
        ),
    )

    async def scenario() -> None:
        _ = [chunk async for chunk in service.stream_text("weather", run_context)]

    asyncio.run(scenario())

    assert captured["agent"].tools == []
