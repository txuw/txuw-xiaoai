from __future__ import annotations

import asyncio
import inspect
import re
from types import SimpleNamespace

from agents import RunContextWrapper
from agents.tool import resolve_function_tool_failure_error_function

from txuw_xiaoai_server.xiaoai_handlers.agent import (
    AgentRunContext,
    AgentToolsetFactory,
    REGION_TOOL_NAME,
    RegionLookupError,
    RegionLookupService,
    AmapIpRegionProvider,
)


class FakeResponse:
    def __init__(self, payload: dict[str, object], *, raise_error: Exception | None = None) -> None:
        self._payload = payload
        self._raise_error = raise_error

    def raise_for_status(self) -> None:
        if self._raise_error is not None:
            raise self._raise_error

    def json(self) -> dict[str, object]:
        return self._payload


class FakeAsyncClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.close_calls = 0

    async def get(self, url: str, *, params: dict[str, str]) -> FakeResponse:
        self.calls.append({"url": url, "params": dict(params)})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def aclose(self) -> None:
        self.close_calls += 1


def _success_payload(
    *,
    province: str = "北京市",
    city: str | object = "北京市",
    adcode: str = "110000",
    rectangle: str = "116.0119343,39.66127144;116.7829835,40.2164962",
) -> dict[str, object]:
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "province": province,
        "city": city,
        "adcode": adcode,
        "rectangle": rectangle,
    }


def _build_tool_and_context(
    client: FakeAsyncClient,
) -> tuple[object, AgentRunContext, list[str], RegionLookupService]:
    provider = AmapIpRegionProvider(client, key="test-map-key")
    region_service = RegionLookupService(provider, cache_ttl_seconds=300.0)
    spoken: list[str] = []
    announce_once_keys: set[str] = set()

    async def speak_progress(text: str) -> None:
        announce_key = "region_lookup" if text == "正在进行地区获取。" else f"progress:{text}"
        if announce_key in announce_once_keys:
            return
        announce_once_keys.add(announce_key)
        spoken.append(text)

    run_context = AgentRunContext(
        connection_id="conn-region",
        dialog_id="dialog-region",
        instruction_name="Query",
        source="query",
        locale="zh-CN",
        speak_progress=speak_progress,
        announce_once_keys=announce_once_keys,
        region_service=region_service,
    )
    factory = AgentToolsetFactory(
        amap_web_service_key="test-map-key",
        region_tool_enabled=True,
        region_tool_timeout_seconds=1.0,
    )
    toolset = factory.build_toolset(run_context)
    tool = next(tool for tool in toolset.tools if tool.name == REGION_TOOL_NAME)
    return tool, run_context, spoken, region_service


async def _invoke_tool(tool, run_context: AgentRunContext, payload: str):
    return await tool.on_invoke_tool(
        SimpleNamespace(tool_name=tool.name, context=run_context),
        payload,
    )


async def _call_error_formatter(tool, run_context: AgentRunContext, error: Exception) -> str:
    formatter = resolve_function_tool_failure_error_function(tool)
    assert formatter is not None
    result = formatter(RunContextWrapper(context=run_context), error)
    if inspect.isawaitable(result):
        return await result
    return result


async def _call_timeout_formatter(tool, run_context: AgentRunContext, error: Exception) -> str:
    formatter = tool.timeout_error_function
    assert formatter is not None
    result = formatter(RunContextWrapper(context=run_context), error)
    if inspect.isawaitable(result):
        return await result
    return result


def test_region_tool_schema_only_contains_ip_and_disallows_additional_properties() -> None:
    tool, _, _, _ = _build_tool_and_context(FakeAsyncClient([FakeResponse(_success_payload())]))
    schema = tool.params_json_schema

    assert tool.name == REGION_TOOL_NAME
    assert re.fullmatch(r"^[a-zA-Z0-9_-]+$", tool.name) is not None
    assert schema["type"] == "object"
    assert list(schema["properties"].keys()) == ["ip"]
    assert schema["properties"]["ip"]["type"] == "string"
    assert schema["additionalProperties"] is False


def test_region_tool_uses_request_ip_when_ip_is_omitted() -> None:
    client = FakeAsyncClient([FakeResponse(_success_payload())])
    tool, run_context, spoken, _ = _build_tool_and_context(client)

    result = asyncio.run(_invoke_tool(tool, run_context, "{}"))

    assert spoken == ["正在进行地区获取。"]
    assert client.calls == [
        {
            "url": "https://restapi.amap.com/v3/ip",
            "params": {"key": "test-map-key"},
        }
    ]
    assert result == {
        "provider": "amap_ip",
        "province": "北京市",
        "city": "北京市",
        "adcode": "110000",
        "rectangle": "116.0119343,39.66127144;116.7829835,40.2164962",
    }


def test_region_tool_forwards_explicit_ipv4_parameter() -> None:
    client = FakeAsyncClient([FakeResponse(_success_payload(province="广东省", city="深圳市", adcode="440300"))])
    tool, run_context, _, _ = _build_tool_and_context(client)

    result = asyncio.run(_invoke_tool(tool, run_context, '{"ip":"1.1.1.1"}'))

    assert client.calls[0]["params"] == {
        "key": "test-map-key",
        "ip": "1.1.1.1",
    }
    assert result["provider"] == "amap_ip"
    assert result["province"] == "广东省"
    assert result["city"] == "深圳市"


def test_region_tool_normalizes_amap_response() -> None:
    client = FakeAsyncClient([FakeResponse(_success_payload())])
    tool, run_context, _, _ = _build_tool_and_context(client)

    result = asyncio.run(_invoke_tool(tool, run_context, "{}"))

    assert result == {
        "provider": "amap_ip",
        "province": "北京市",
        "city": "北京市",
        "adcode": "110000",
        "rectangle": "116.0119343,39.66127144;116.7829835,40.2164962",
    }


def test_region_tool_handles_local_network_and_empty_city() -> None:
    client = FakeAsyncClient(
        [
            FakeResponse(
                _success_payload(
                    province="局域网",
                    city=[],
                    adcode="",
                    rectangle="",
                )
            )
        ]
    )
    tool, run_context, _, _ = _build_tool_and_context(client)

    result = asyncio.run(_invoke_tool(tool, run_context, "{}"))

    assert result == {
        "provider": "amap_ip",
        "province": "局域网",
        "city": None,
        "adcode": None,
        "rectangle": None,
    }


def test_region_tool_progress_is_announced_once_and_cache_avoids_duplicate_requests() -> None:
    client = FakeAsyncClient([FakeResponse(_success_payload())])
    tool, run_context, spoken, _ = _build_tool_and_context(client)

    async def scenario() -> None:
        await _invoke_tool(tool, run_context, "{}")
        await _invoke_tool(tool, run_context, "{}")

    asyncio.run(scenario())

    assert spoken == ["正在进行地区获取。"]
    assert len(client.calls) == 1


def test_region_tool_rejects_ipv6_as_model_visible_error() -> None:
    client = FakeAsyncClient([FakeResponse(_success_payload())])
    tool, run_context, _, _ = _build_tool_and_context(client)

    result = asyncio.run(_invoke_tool(tool, run_context, '{"ip":"240e:1234::1"}'))

    assert "高德 IP 定位仅支持国内 IPv4" in result
    assert client.calls == []


def test_region_tool_invalid_ip_uses_custom_failure_formatter() -> None:
    client = FakeAsyncClient([FakeResponse(_success_payload())])
    tool, run_context, _, _ = _build_tool_and_context(client)

    result = asyncio.run(_invoke_tool(tool, run_context, '{"ip":"bad-ip"}'))
    assert "当前暂时无法确定地区" in result

    message = asyncio.run(_call_error_formatter(tool, run_context, ValueError("bad-ip")))

    assert "当前暂时无法确定地区" in message
    assert "bad-ip" in message


def test_region_tool_provider_error_uses_custom_failure_formatter() -> None:
    client = FakeAsyncClient(
        [
            FakeResponse(
                {
                    "status": "0",
                    "info": "INVALID_USER_KEY",
                    "infocode": "10001",
                }
            )
        ]
    )
    tool, run_context, _, _ = _build_tool_and_context(client)

    result = asyncio.run(_invoke_tool(tool, run_context, "{}"))
    assert "INVALID_USER_KEY" in result
    assert "10001" in result

    message = asyncio.run(
        _call_error_formatter(tool, run_context, RegionLookupError("INVALID_USER_KEY infocode=10001"))
    )

    assert "INVALID_USER_KEY" in message
    assert "当前暂时无法确定地区" in message


def test_region_tool_timeout_uses_custom_timeout_formatter() -> None:
    tool, run_context, _, _ = _build_tool_and_context(FakeAsyncClient([FakeResponse(_success_payload())]))

    message = asyncio.run(_call_timeout_formatter(tool, run_context, TimeoutError("timeout")))

    assert "地区获取超时" in message


def test_region_lookup_service_close_closes_underlying_http_client() -> None:
    client = FakeAsyncClient([FakeResponse(_success_payload())])
    provider = AmapIpRegionProvider(client, key="test-map-key")
    service = RegionLookupService(provider, cache_ttl_seconds=300.0)

    asyncio.run(service.close())

    assert client.close_calls == 1
